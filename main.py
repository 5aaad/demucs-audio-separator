from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from enum import Enum
from pathlib import Path
import shutil, subprocess, os, uuid, asyncio, threading, zipfile, soundfile as sf, logging, yt_dlp
import traceback, base64
from pydantic import BaseModel

app = FastAPI(title="Demucs Audio Processor", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

active_downloads = {}

class Mode(str, Enum):
    two = "two"
    four = "four"

class RunPodInput(BaseModel):
    file_base64: str
    filename: str
    mode: str = "two"

class RunPodRequest(BaseModel):
    input: RunPodInput
    
class ExtractAudioInput(BaseModel):
    file_base64: str
    filename: str
    
class YouTubeInput(BaseModel):
    url: str

def cleanup_files(paths: list[Path], dirs: list[Path] = []):
    for path in paths:
        try:
            if path.exists():
                path.unlink()
                logger.info(f"Deleted file: {path}")
        except Exception as e:
            logger.warning(f"Failed to delete file {path}: {e}")
            logger.debug(traceback.format_exc())

    for directory in dirs:
        try:
            if directory.exists() and directory.is_dir():
                logger.info(f"Attempting to delete directory: {directory}")
                shutil.rmtree(directory)
                logger.info(f"Deleted directory: {directory}")
        except Exception as e:
            logger.warning(f"Failed to delete directory {directory}: {e}")
            logger.debug(traceback.format_exc())
            
@app.post("/extract_audio")
async def extract_audio(input: ExtractAudioInput):
    try:
        file_data = base64.b64decode(input.file_base64)
        filename = input.filename
        ext = Path(filename).suffix.lower()

        allowed_exts = {".mp4", ".mov", ".mkv", ".avi"}
        if ext not in allowed_exts:
            raise HTTPException(status_code=400, detail="Unsupported video format.")

        temp_id = uuid.uuid4().hex
        input_path = Path("input") / f"{temp_id}_{filename}"
        output_path = input_path.with_suffix(".mp3")

        with open(input_path, "wb") as f:
            f.write(file_data)

        subprocess.run([
            "ffmpeg", "-y", "-i", str(input_path),
            "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k",
            str(output_path)
        ], check=True)

        encoded_audio = base64.b64encode(output_path.read_bytes()).decode("utf-8")

        cleanup_files(paths=[input_path, output_path])

        return JSONResponse(content={
            "audio_base64": encoded_audio,
            "filename": output_path.name,
            "message": "Audio successfully extracted from video"
        })

    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"FFmpeg failed: {e.stderr}")
    except Exception as e:
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    
from fastapi.concurrency import run_in_threadpool

@app.post("/youtube_audio")
async def download_youtube(input: YouTubeInput, request: Request):
    try:
        temp_id = uuid.uuid4().hex
        temp_dir = Path("input")
        temp_dir.mkdir(exist_ok=True)
        output_template = temp_dir / f"{temp_id}.%(ext)s"
        
        cancellation_event = threading.Event()
        active_downloads[temp_id] = cancellation_event
        
        if await request.is_disconnected():
            logger.info("Client disconnected before download started")
            raise HTTPException(status_code=499, detail="Client disconnected")

        def progress_hook(d):
            if cancellation_event.is_set():
                logger.info(f"Download {temp_id} cancelled via progress hook")
                raise yt_dlp.utils.DownloadError("Download cancelled")
            
            if d['status'] == 'downloading':
                logger.info(f"Download progress: {d.get('_percent_str', 'N/A')}")
            elif d['status'] == 'finished':
                logger.info(f"Download finished: {d['filename']}")
            elif d['status'] == 'error':
                logger.error(f"Download error: {d.get('error', 'Unknown error')}")

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(output_template),
            "quiet": False,  
            "no_warnings": False,
            "progress_hooks": [progress_hook],
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192"
            }],
            "socket_timeout": 30,
            "retries": 1,
            "fragment_retries": 1,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "postprocessor_args": [
                "-threads", "1",  
                "-y",  
                "-loglevel", "error",  
                "-nostdin" 
            ],
            "ignoreerrors": False,
            "abort_on_unavailable_fragment": True,
        }

        def _download_youtube():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    if cancellation_event.is_set():
                        logger.info(f"Download {temp_id} cancelled before start")
                        raise yt_dlp.utils.DownloadError("Download cancelled")
                    
                    info = ydl.extract_info(input.url, download=False)
                    logger.info(f"Video: {info.get('title', 'Unknown')} - Duration: {info.get('duration', 'Unknown')}s")
                    
                    if cancellation_event.is_set():
                        logger.info(f"Download {temp_id} cancelled after info extraction")
                        raise yt_dlp.utils.DownloadError("Download cancelled")
                    
                    ydl.download([input.url])
                    
                    if cancellation_event.is_set():
                        logger.info(f"Download {temp_id} cancelled after download")
                        raise yt_dlp.utils.DownloadError("Download cancelled")
                        
            except yt_dlp.utils.DownloadError as e:
                if "cancelled" in str(e).lower():
                    logger.info(f"Download {temp_id} cancelled: {e}")
                    raise
                else:
                    logger.error(f"yt-dlp error: {e}")
                    raise
            except Exception as e:
                logger.error(f"Download error: {e}")
                raise

        async def monitor_client_disconnection():
            while True:
                if await request.is_disconnected():
                    logger.info(f"Client disconnected for download {temp_id}")
                    cancellation_event.set()
                    return
                await asyncio.sleep(1)  

        download_task = asyncio.create_task(
            asyncio.wait_for(
                run_in_threadpool(_download_youtube), 
                timeout=300  
            )
        )
        
        monitor_task = asyncio.create_task(monitor_client_disconnection())
        
        try:
            done, pending = await asyncio.wait(
                [download_task, monitor_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            for task in pending:
                task.cancel()
            
            if cancellation_event.is_set():
                logger.info(f"Download {temp_id} was cancelled")
                raise HTTPException(status_code=499, detail="Download cancelled")
            
            if download_task in done:
                await download_task  
            else:
                cancellation_event.set()
                raise HTTPException(status_code=499, detail="Client disconnected")
                
        except asyncio.TimeoutError:
            logger.error(f"Download {temp_id} timed out")
            cancellation_event.set()
            raise HTTPException(status_code=504, detail="Download timeout")
        except asyncio.CancelledError:
            logger.info(f"Download {temp_id} was cancelled")
            cancellation_event.set()
            raise HTTPException(status_code=499, detail="Download cancelled")

        mp3_files = list(temp_dir.glob(f"{temp_id}*.mp3"))
        if not mp3_files:
            audio_files = (
                list(temp_dir.glob(f"{temp_id}*.webm")) +
                list(temp_dir.glob(f"{temp_id}*.m4a")) +
                list(temp_dir.glob(f"{temp_id}*.wav")) +
                list(temp_dir.glob(f"{temp_id}*.aac"))
            )
            
            if audio_files:
                logger.info(f"No MP3 found, but found audio file: {audio_files[0]}")
                output_file = audio_files[0]
            else:
                logger.error(f"No audio files found for download {temp_id}")
                all_files = list(temp_dir.glob(f"{temp_id}*"))
                logger.info(f"All files found: {[f.name for f in all_files]}")
                raise HTTPException(status_code=500, detail="No audio file found after download")
        else:
            output_file = mp3_files[0]

        if not output_file.exists():
            logger.error(f"Output file {output_file} does not exist")
            raise HTTPException(status_code=500, detail="Output file was not created")
            
        file_size = output_file.stat().st_size
        if file_size == 0:
            logger.error(f"Output file {output_file} is empty")
            raise HTTPException(status_code=500, detail="Output file is empty")

        logger.info(f"Using output file: {output_file} (size: {file_size} bytes)")

        encoded_audio = base64.b64encode(output_file.read_bytes()).decode("utf-8")
        
        cleanup_files(paths=[output_file])

        return JSONResponse(content={
            "audio_base64": encoded_audio,
            "filename": output_file.name,
            "file_size": file_size,
            "message": "YouTube audio successfully downloaded"
        })

    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as e:
        if "cancelled" in str(e).lower():
            logger.info(f"Download cancelled: {e}")
            raise HTTPException(status_code=499, detail="Download cancelled")
        else:
            logger.error(f"yt-dlp download error: {e}")
            clean_error = str(e).replace('\x1b[0;31m', '').replace('\x1b[0m', '').replace('ERROR:', '').strip()
            raise HTTPException(status_code=400, detail=f"Download failed: {clean_error}")
    except Exception as e:
        logger.error(f"Unexpected error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"YouTube download failed: {str(e)}")
    
    finally:
        try:
            files_to_clean = list(temp_dir.glob(f"{temp_id}*"))
            for file_path in files_to_clean:
                try:
                    if file_path.exists():
                        file_path.unlink()
                        logger.info(f"Cleaned up: {file_path}")
                except Exception as e:
                    logger.error(f"Error cleaning up {file_path}: {e}")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
        
        if temp_id in active_downloads:
            del active_downloads[temp_id]

@app.delete("/youtube_audio/cancel_all")
async def cancel_all_downloads():
    """Cancel all active downloads"""
    cancelled_count = 0
    for temp_id, cancellation_event in active_downloads.items():
        if not cancellation_event.is_set():
            cancellation_event.set()
            cancelled_count += 1
    
    return JSONResponse(content={
        "message": f"Cancelled {cancelled_count} active downloads"
    })


@app.post("/process_audio")
async def runpod_handler(request: RunPodRequest):
    try:
        input_data = request.input
        
        file_data = base64.b64decode(input_data.file_base64)
        filename = input_data.filename
        mode = input_data.mode
        
        allowed_exts = {".mp3", ".wav", ".flac", ".m4a", ".aac"}
        ext = Path(filename).suffix.lower()
        if ext not in allowed_exts:
            raise HTTPException(status_code=400, detail="Invalid file type. Supported formats: mp3, wav, flac, m4a, aac")
        
        track_id = f"{uuid.uuid4()}_{filename}"
        input_dir = Path("input")
        output_dir = Path("separated")
        input_dir.mkdir(exist_ok=True)
        output_dir.mkdir(exist_ok=True)
        
        input_path = input_dir / track_id
        
        with open(input_path, "wb") as f:
            f.write(file_data)
        logger.info(f"Saved file to {input_path}")

        if mode == "two":
            demucs_command = [
                "python", "-m", "demucs",
                "--two-stems=vocals",
                str(input_path),
                "--out=separated"
            ]
        else:
            demucs_command = [
                "python", "-m", "demucs",
                str(input_path),
                "--out=separated"
            ]
            
        try:
            result = subprocess.run(demucs_command, check=True, capture_output=True, text=True)
            logger.info(f"Demucs output: {result.stdout}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Demucs failed: {e.stderr}")
            raise HTTPException(status_code=500, detail=f"Stem separation failed: {e.stderr}")

        track_name = input_path.stem
        stem_folder = Path("separated") / "htdemucs" / track_name
        
        stems = {
            "vocals.wav": "vocals.mp3",
            "no_vocals.wav": "background.mp3"
        } if mode == "two" else {
            "vocals.wav": "vocals.mp3",
            "drums.wav": "drums.mp3",
            "bass.wav": "bass.mp3",
            "other.wav": "other.mp3"
        }
        
        output_files = {}
        
        for wav_file, mp3_file in stems.items():
            wav_path = stem_folder / wav_file
            mp3_path = stem_folder / mp3_file
            if wav_path.exists():
                try:
                    subprocess.run([
                        "ffmpeg", "-y", "-i", str(wav_path),
                        "-codec:a", "libmp3lame", "-qscale:a", "5",
                        str(mp3_path)
                    ], check=True, capture_output=True)
                    
                    with open(mp3_path, "rb") as f:
                        mp3_data = f.read()
                        output_files[mp3_file] = base64.b64encode(mp3_data).decode('utf-8')
                        
                except subprocess.CalledProcessError as e:
                    logger.error(f"FFmpeg failed for {wav_file}: {e.stderr.decode()}")
                    continue
        
        cleanup_files(
            paths=[input_path, *[stem_folder / mp3_file for mp3_file in stems.values()]],
            dirs=[stem_folder]
        )
        
        return JSONResponse(content={
            "output": output_files,
            "message": f"Successfully separated {len(output_files)} stems"
        })
        
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
