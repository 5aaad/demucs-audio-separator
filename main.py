from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from enum import Enum
from pathlib import Path
import shutil, subprocess, os, uuid, zipfile, soundfile as sf, logging, yt_dlp
import requests, re
from pydantic import BaseModel
import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from urllib.parse import urlparse, unquote_plus
from typing import Dict
from dotenv import load_dotenv
from functools import partial

load_dotenv()

app = FastAPI(title="Demucs Audio Processor", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

def extract_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    filename = os.path.basename(parsed.path)
    return unquote_plus(filename) if '.' in filename else "audio_file"

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
        r = await run_in_threadpool(partial(requests.get, url, stream=True, timeout=300))
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

async def download_youtube_audio(url: str, temp_id: str) -> Path:
    output = Path("/tmp") / f"{temp_id}.%(ext)s"
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output),
        "quiet": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192"
        }]
    }
    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    await run_in_threadpool(_download)
    mp3_path = list(Path("/tmp").glob(f"{temp_id}*.mp3"))
    return mp3_path[0] if mp3_path else None

async def extract_audio(video: Path, temp_id: str) -> Path:
    out_path = Path("/tmp") / f"{temp_id}_extracted.mp3"
    await run_in_threadpool(partial(subprocess.run, [
        "ffmpeg", "-y", "-i", str(video), "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k", str(out_path)
    ], check=True))
    return out_path

async def run_demucs(audio_path: Path, mode: Mode, temp_id: str) -> Dict[str, Path]:
    output_dir = Path("/tmp/separated")
    cmd = ["python", "-m", "demucs", str(audio_path), "--out", str(output_dir)]
    if mode == Mode.two:
        cmd.insert(3, "--two-stems=vocals")
    await run_in_threadpool(partial(subprocess.run, cmd, check=True, capture_output=True))

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
            await run_in_threadpool(partial(subprocess.run, [
                "ffmpeg", "-y", "-i", str(src), "-codec:a", "libmp3lame", "-qscale:a", "5", str(dst)
            ], check=True))
            output[mp3.replace('.mp3', '')] = dst
    return output

def zip_stems(stems: Dict[str, Path], filename: str) -> Path:
    zip_path = Path("/tmp") / filename
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, path in stems.items():
            z.write(path, f"{name}.mp3")
    return zip_path

@app.post("/process_audio")
async def process(input_data: ProcessAudioInput):
    temp_id = uuid.uuid4().hex
    Path("/tmp").mkdir(parents=True, exist_ok=True)

    try:
        if input_data.upload_type == UploadType.youtubeURL:
            audio = await download_youtube_audio(input_data.url, temp_id)
        elif input_data.upload_type == UploadType.audio:
            audio = Path("/tmp") / f"{temp_id}_{extract_filename_from_url(input_data.url)}"
            await download_file(input_data.url, audio)
        elif input_data.upload_type == UploadType.video:
            video = Path("/tmp") / f"{temp_id}_{extract_filename_from_url(input_data.url)}"
            await download_file(input_data.url, video)
            audio = await extract_audio(video, temp_id)
        else:
            raise HTTPException(status_code=400, detail="Invalid upload type")

        stems = await run_demucs(audio, input_data.mode, temp_id)

        slug_name = slugify((input_data.filename or extract_filename_from_url(input_data.url)).rsplit('.', 1)[0])
        mode_label = "2stem" if input_data.mode == Mode.two else "4stem"
        zip_filename = f"{slug_name}-{mode_label}-{temp_id[:8]}.zip"

        zip_path = zip_stems(stems, zip_filename)

        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            filename=zip_filename
        )

    except Exception as e:
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health():
    return {"status": "ok"}
