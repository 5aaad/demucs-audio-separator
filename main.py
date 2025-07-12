from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from enum import Enum
from pathlib import Path
import shutil, subprocess, os, uuid, zipfile, soundfile as sf, logging, os
import traceback

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

class Mode(str, Enum):
    two = "two"
    four = "four"

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


@app.post("/process_audio")
async def process_audio(file: UploadFile = File(...), 
                        mode: str = Form("two"),
                        background_tasks: BackgroundTasks = None):
        
        allowed_exts = {".mp3", ".wav", ".flac", ".m4a", ".aac"}
        ext = Path(file.filename).suffix.lower()
        if ext not in allowed_exts:
            raise HTTPException(status_code=400, detail="Invalid file type. Supported formats: mp3, wav, flac, m4a, aac")
        
        track_id = f"{uuid.uuid4()}_{file.filename}"
        input_dir = Path("input")
        output_dir = Path("separated")
        input_dir.mkdir(exist_ok=True)
        output_dir.mkdir(exist_ok=True)
        
        input_path = input_dir / track_id
        
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        logger.info(f"Saved file to {input_path}")


        if mode == "two":
            demucs_command = [
                "python", "-m", "demucs",
                "--two-stems=vocals",
                input_path,
                "--out=separated"
            ]
        else:
            demucs_command = [
                "python", "-m", "demucs",
                input_path,
                "--out=separated"
            ]
            
        try:
            subprocess.run(demucs_command, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Demucs failed: {e.stderr.decode()}")
            raise HTTPException(status_code=500, detail="Stem separation failed")

        track_name = input_path.stem
        stem_folder = Path("separated") / "htdemucs" / track_name
        zip_path = output_dir / f"{track_name}_stems.zip"
        
        stems = {
            "vocals.wav": "vocals.mp3",
            "no_vocals.wav": "background.mp3"
        } if mode == Mode.two else {
            "vocals.wav": "vocals.mp3",
            "drums.wav": "drums.mp3",
            "bass.wav": "bass.mp3",
            "other.wav": "other.mp3"
        }
        
        mp3_paths = []
        
        for wav_file, mp3_file in stems.items():
            wav_path = stem_folder / wav_file
            mp3_path = stem_folder / mp3_file
            if wav_path.exists():
                try:
                    
                    # for Linux/Mac
                    # subprocess.run([
                    #     "/usr/bin/ffmpeg", "-y", "-i", str(wav_path),
                    #     "-codec:a", "libmp3lame", "-qscale:a", "5",
                    #     str(mp3_path)
                    # ], check=True, capture_output=True) 
                    
                    # for Windows
                    subprocess.run([
                        "ffmpeg", "-y", "-i", str(wav_path),
                        "-codec:a", "libmp3lame", "-qscale:a", "5",
                        str(mp3_path)
                    ], check=True, capture_output=True) 

                    mp3_paths.append((mp3_path, mp3_file))
                except subprocess.CalledProcessError as e:
                    logger.error(f"FFmpeg failed for {wav_file}: {e.stderr.decode()}")
                    continue
                
            

        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for full_path, arcname in mp3_paths:
                zipf.write(full_path, arcname=arcname)
        logger.info(f"Created ZIP at {zip_path}")
        
        background_tasks.add_task(
            cleanup_files,
            paths=[input_path, *[p[0] for p in mp3_paths], zip_path],
            dirs=[stem_folder]
        )

        return FileResponse(
            path=zip_path,
            media_type="application/zip",
            filename=zip_path.name
        )   



