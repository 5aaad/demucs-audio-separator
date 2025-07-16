from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from enum import Enum
from pathlib import Path
import shutil, subprocess, os, uuid, zipfile, soundfile as sf, logging, yt_dlp
import requests, re, time, random
from pydantic import BaseModel
import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from urllib.parse import urlparse, unquote_plus
from typing import Dict, Optional
from dotenv import load_dotenv
from functools import partial
import asyncio

# Import the Docker-optimized YouTube downloader
from docker_youtube_downloader import (
    download_youtube_audio, 
    startup_docker_youtube, 
    docker_health_check
)

load_dotenv()

app = FastAPI(title="Demucs Audio Processor", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Enhanced logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Check if running in Docker
IS_DOCKER = os.path.exists('/.dockerenv')
if IS_DOCKER:
    logger.info("Running in Docker environment")
else:
    logger.info("Running in local environment")

class UploadType(str, Enum):
    youtubeURL = "youtubeURL"
    audio = "audio"
    video = "video"

class Mode(str, Enum):
    two = "two"
    four = "four"

class ProcessAudioInput(BaseModel):
    upload_type: UploadType
    url: str
    mode: Mode = Mode.two
    filename: str = None

def slugify(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')

def is_s3_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.netloc.endswith('.amazonaws.com') or
        parsed.netloc.endswith('.s3.amazonaws.com') or
        's3' in parsed.netloc
    )

def extract_filename_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    parsed = urlparse(url)
    filename = os.path.basename(parsed.path)
    return unquote_plus(filename) if '.' in filename else None

async def download_file(url: str, dest: Path):
    if is_s3_url(url):
        s3 = boto3.client(
            's3',
            aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
            region_name=os.getenv('AWS_REGION')
        )
        bucket = os.getenv("S3_BUCKET_NAME")
        key = unquote_plus(urlparse(url).path.lstrip("/"))
        await run_in_threadpool(s3.download_file, bucket, key, str(dest))
    else:
        # Enhanced download with better error handling
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            r = await run_in_threadpool(
                partial(requests.get, url, stream=True, timeout=300, headers=headers)
            )
            r.raise_for_status()
            
            with open(dest, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to download file from {url}: {e}")
            raise HTTPException(status_code=400, detail=f"Failed to download file: {e}")

async def extract_audio(video: Path, temp_id: str) -> Path:
    out_path = Path("/tmp") / f"{temp_id}_extracted.mp3"
    try:
        await run_in_threadpool(partial(subprocess.run, [
            "ffmpeg", "-y", "-i", str(video), "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k", str(out_path)
        ], check=True, capture_output=True))
        return out_path
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg extraction failed: {e}")
        raise HTTPException(status_code=500, detail="Audio extraction failed")

async def run_demucs(audio_path: Path, mode: Mode, temp_id: str) -> Dict[str, Path]:
    output_dir = Path("/tmp/separated")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    cmd = ["python", "-m", "demucs", str(audio_path), "--out", str(output_dir)]
    if mode == Mode.two:
        cmd.insert(3, "--two-stems=vocals")
    
    try:
        await run_in_threadpool(partial(subprocess.run, cmd, check=True, capture_output=True))
    except subprocess.CalledProcessError as e:
        logger.error(f"Demucs processing failed: {e}")
        raise HTTPException(status_code=500, detail="Audio separation failed")

    stem_dir = output_dir / "htdemucs" / audio_path.stem
    stems = {
        "vocals.wav": "vocals.mp3",
        "no_vocals.wav": "background.mp3"
    } if mode == Mode.two else {
        "vocals.wav": "vocals.mp3",
        "drums.wav": "drums.mp3",
        "bass.wav": "bass.mp3",
        "other.wav": "other.mp3"
    }

    output = {}
    for wav, mp3 in stems.items():
        src = stem_dir / wav
        dst = stem_dir / mp3
        if src.exists():
            try:
                await run_in_threadpool(partial(subprocess.run, [
                    "ffmpeg", "-y", "-i", str(src), "-codec:a", "libmp3lame", "-qscale:a", "5", str(dst)
                ], check=True, capture_output=True))
                output[mp3.replace('.mp3', '')] = dst
            except subprocess.CalledProcessError as e:
                logger.error(f"MP3 conversion failed for {wav}: {e}")
                # Continue with other files
                continue
    
    if not output:
        raise HTTPException(status_code=500, detail="No audio stems were generated")
    
    return output

def zip_stems(stems: Dict[str, Path], filename: str) -> Path:
    zip_path = Path("/tmp") / filename
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, path in stems.items():
                if path.exists():
                    z.write(path, f"{name}.mp3")
        return zip_path
    except Exception as e:
        logger.error(f"Failed to create zip file: {e}")
        raise HTTPException(status_code=500, detail="Failed to create output archive")

async def cleanup_temp_files(temp_id: str):
    """Clean up temporary files"""
    try:
        temp_dir = Path("/tmp")
        
        # Clean up files with temp_id
        for file in temp_dir.glob(f"{temp_id}*"):
            if file.is_file():
                try:
                    file.unlink()
                except Exception as e:
                    logger.warning(f"Failed to delete {file}: {e}")
        
        # Clean up separated directory
        separated_dir = temp_dir / "separated"
        if separated_dir.exists():
            try:
                shutil.rmtree(separated_dir)
            except Exception as e:
                logger.warning(f"Failed to delete separated directory: {e}")
                
    except Exception as e:
        logger.warning(f"Failed to cleanup temp files: {e}")

@app.on_event("startup")
async def startup_event():
    """Initialize Docker environment on startup"""
    if IS_DOCKER:
        logger.info("Initializing Docker environment for YouTube downloads...")
        health = await startup_docker_youtube()
        logger.info(f"Docker YouTube initialization result: {health}")

@app.post("/process_audio")
async def process(input_data: ProcessAudioInput, background_tasks: BackgroundTasks):
    temp_id = uuid.uuid4().hex
    Path("/tmp").mkdir(parents=True, exist_ok=True)
    
    logger.info(f"Processing audio request: {input_data.upload_type} - {temp_id}")

    try:
        
        print("upload_type:", input_data.upload_type)
        print("url:", input_data.url)
        print("filename:", input_data.filename)
        
        if input_data.upload_type == UploadType.youtubeURL:
            logger.info(f"Downloading YouTube audio: {input_data.url}")
            audio = await download_youtube_audio(input_data.url, temp_id)
            if not audio:
                raise HTTPException(status_code=500, detail="Failed to download YouTube audio")
                
        elif input_data.upload_type == UploadType.audio:
            audio = Path("/tmp") / f"{temp_id}_{extract_filename_from_url(input_data.url)}"
            await download_file(input_data.url, audio)
            
        elif input_data.upload_type == UploadType.video:
            video = Path("/tmp") / f"{temp_id}_{extract_filename_from_url(input_data.url)}"
            await download_file(input_data.url, video)
            audio = await extract_audio(video, temp_id)
            
        else:
            raise HTTPException(status_code=400, detail="Invalid upload type")

        logger.info(f"Running Demucs separation in {input_data.mode} mode")
        stems = await run_demucs(audio, input_data.mode, temp_id)

        raw_filename = (
            input_data.filename 
            or extract_filename_from_url(input_data.url) 
            or f"audio_{uuid.uuid4().hex[:8]}.mp3"
        )
        if not raw_filename:
            raise HTTPException(status_code=400, detail="Filename could not be determined")

        slug_name = slugify(raw_filename.rsplit('.', 1)[0])
        mode_label = "2stem" if input_data.mode == Mode.two else "4stem"
        zip_filename = f"{slug_name}-{mode_label}-{temp_id[:8]}.zip"

        logger.info(f"Filename fallback resolution: input={input_data.filename}, fallback={extract_filename_from_url(input_data.url)}")

        zip_path = zip_stems(stems, zip_filename)

        # Schedule cleanup of temporary files
        background_tasks.add_task(cleanup_temp_files, temp_id)

        logger.info(f"Successfully processed audio: {zip_filename}")
        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename=zip_filename
        )

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Error processing audio: {e}")
        # Clean up on error
        await cleanup_temp_files(temp_id)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    """Enhanced health check"""
    try:
        basic_health = {"status": "ok", "docker": IS_DOCKER}
        
        if IS_DOCKER:
            youtube_health = await docker_health_check()
            basic_health.update(youtube_health)
            
        return basic_health
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.post("/test_youtube_url")
async def test_youtube_url(url: str):
    """Test if a YouTube URL can be downloaded"""
    try:
        temp_id = uuid.uuid4().hex
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 10,
        }
        
        def _test():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return {
                    "title": info.get("title", "Unknown"),
                    "duration": info.get("duration", 0),
                    "available": True
                }
        
        result = await run_in_threadpool(_test)
        return result
    except Exception as e:
        return {
            "available": False,
            "error": str(e)
        }

@app.get("/debug/docker")
async def debug_docker():
    """Debug endpoint for Docker environment"""
    return {
        "is_docker": IS_DOCKER,
        "tmp_dir_exists": Path("/tmp").exists(),
        "tmp_dir_writable": os.access("/tmp", os.W_OK),
        "environment_vars": {
            "YT_DLP_CACHE_DIR": os.getenv("YT_DLP_CACHE_DIR"),
            "TMPDIR": os.getenv("TMPDIR"),
        }
    }