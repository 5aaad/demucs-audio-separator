# 🎧 Demucs Audio Microservice

FastAPI microservice for:

- Stem separation (Demucs)
- YouTube to MP3 (yt-dlp)
- Video to MP3 (ffmpeg)

## Endpoints

### `/process_audio`

Input: base64 audio, filename, mode (`two` or `four`)  
Returns: base64 of separated stems (MP3)

### `/extract_audio`

Input: base64 video file  
Returns: base64 audio file (MP3)

### `/youtube_audio`

Input: YouTube URL  
Returns: base64 MP3

## Deployment (RunPod Serverless)

1. Build using provided Dockerfile
2. Expose `main:app` using Gunicorn with Uvicorn worker
3. JSON-only interface for serverless compatibility

---
