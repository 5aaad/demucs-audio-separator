import asyncio
import os
import random
import time
from pathlib import Path
from typing import Optional, Dict, Any
import logging
import subprocess
import yt_dlp
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from functools import partial

logger = logging.getLogger(__name__)

class DockerYouTubeDownloader:
    def __init__(self):
        self.max_attempts = 5
        self.base_delay = 2
        self.max_delay = 10
        
    def get_docker_optimized_headers(self) -> Dict[str, str]:
        """Get headers optimized for Docker environment"""
        return {
            'User-Agent': self.get_random_user_agent(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0',
        }
    
    def get_random_user_agent(self) -> str:
        """Get a random user agent string"""
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
            "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
        ]
        return random.choice(user_agents)
    
    def get_progressive_ydl_opts(self, temp_id: str, attempt: int) -> Dict[str, Any]:
        """Get progressively more aggressive options for each attempt"""
        output = Path("/tmp") / f"{temp_id}.%(ext)s"
        headers = self.get_docker_optimized_headers()
        
        base_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(output),
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "nocheckcertificate": True,
            "http_headers": headers,
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192"
            }]
        }
        
        # Progressive strategies
        if attempt == 1:
            # Standard approach with geo bypass
            base_opts.update({
                "geo_bypass": True,
                "geo_bypass_country": "US",
                "extractor_args": {
                    "youtube": {
                        "skip": ["dash", "hls"],
                        "player_skip": ["configs", "webpage"]
                    }
                }
            })
        elif attempt == 2:
            # Try different format and add proxy-like behavior
            base_opts.update({
                "format": "worstaudio/worst",
                "geo_bypass": True,
                "geo_bypass_country": "CA",
                "sleep_interval": 1,
                "max_sleep_interval": 3,
                "extractor_args": {
                    "youtube": {
                        "skip": ["dash"]
                    }
                }
            })
        elif attempt == 3:
            # Use legacy extraction methods
            base_opts.update({
                "format": "18/mp4",
                "youtube_include_dash_manifest": False,
                "extractor_args": {
                    "youtube": {
                        "skip": ["dash", "hls"],
                        "player_client": "web"
                    }
                }
            })
        elif attempt == 4:
            # Try mobile client
            base_opts.update({
                "format": "worst[ext=mp4]",
                "extractor_args": {
                    "youtube": {
                        "player_client": "android"
                    }
                }
            })
        else:
            # Final attempt - minimal options
            base_opts.update({
                "format": "mp4",
                "no_check_certificate": True,
                "prefer_insecure": True,
                "extractor_args": {
                    "youtube": {
                        "player_client": "web_embedded"
                    }
                }
            })
        
        return base_opts
    
    async def download_with_exponential_backoff(self, url: str, temp_id: str, attempt: int) -> Optional[Path]:
        """Download with exponential backoff and jitter"""
        if attempt > 1:
            # Exponential backoff with jitter
            delay = min(self.base_delay * (2 ** (attempt - 2)), self.max_delay)
            jitter = random.uniform(0.1, 0.5) * delay
            total_delay = delay + jitter
            
            logger.info(f"Waiting {total_delay:.1f} seconds before attempt {attempt}")
            await asyncio.sleep(total_delay)
        
        ydl_opts = self.get_progressive_ydl_opts(temp_id, attempt)
        
        def _download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        
        await run_in_threadpool(_download)
        
        # Check for downloaded file
        mp3_files = list(Path("/tmp").glob(f"{temp_id}*.mp3"))
        return mp3_files[0] if mp3_files else None
    
    async def download_youtube_audio(self, url: str, temp_id: str) -> Optional[Path]:
        """Main download function with comprehensive error handling"""
        logger.info(f"Starting YouTube download for: {url}")
        
        for attempt in range(1, self.max_attempts + 1):
            try:
                logger.info(f"YouTube download attempt {attempt}/{self.max_attempts}")
                
                result = await self.download_with_exponential_backoff(url, temp_id, attempt)
                
                if result and result.exists():
                    logger.info(f"Successfully downloaded on attempt {attempt}: {result}")
                    return result
                else:
                    logger.warning(f"No file found after attempt {attempt}")
                    
            except yt_dlp.utils.DownloadError as e:
                error_msg = str(e).lower()
                logger.error(f"yt-dlp error on attempt {attempt}: {e}")
                
                if "sign in to confirm" in error_msg or "not a bot" in error_msg:
                    logger.warning("Bot detection encountered")
                    if attempt < self.max_attempts:
                        continue
                    else:
                        raise HTTPException(
                            status_code=429, 
                            detail="YouTube bot detection triggered. Please try again later or use a different video."
                        )
                        
                elif "private video" in error_msg or "unavailable" in error_msg:
                    raise HTTPException(status_code=400, detail="Video is private or unavailable")
                    
                elif "copyright" in error_msg or "blocked" in error_msg:
                    raise HTTPException(status_code=400, detail="Video is blocked due to copyright restrictions")
                    
                elif "network" in error_msg or "timeout" in error_msg:
                    logger.warning("Network error, retrying...")
                    if attempt < self.max_attempts:
                        continue
                    else:
                        raise HTTPException(status_code=503, detail="Network timeout. Please try again later.")
                        
                elif attempt == self.max_attempts:
                    raise HTTPException(status_code=500, detail=f"Download failed after {self.max_attempts} attempts: {e}")
                    
            except subprocess.CalledProcessError as e:
                logger.error(f"FFmpeg error on attempt {attempt}: {e}")
                if attempt == self.max_attempts:
                    raise HTTPException(status_code=500, detail="Audio processing failed")
                    
            except Exception as e:
                logger.error(f"Unexpected error on attempt {attempt}: {e}")
                if attempt == self.max_attempts:
                    raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")
        
        raise HTTPException(status_code=500, detail="All download attempts failed")

# Docker-specific helper functions
async def setup_docker_environment():
    """Setup Docker environment for better YouTube compatibility"""
    try:
        # Create necessary directories
        Path("/tmp").mkdir(parents=True, exist_ok=True)
        
        # Set up DNS (helps with some network issues)
        dns_setup = """
nameserver 8.8.8.8
nameserver 8.8.4.4
nameserver 1.1.1.1
"""
        try:
            with open("/etc/resolv.conf", "w") as f:
                f.write(dns_setup)
        except PermissionError:
            logger.warning("Could not update DNS settings (permission denied)")
        
        # Update yt-dlp if possible
        try:
            subprocess.run(["pip", "install", "--upgrade", "yt-dlp"], 
                         capture_output=True, timeout=60)
            logger.info("yt-dlp updated successfully")
        except Exception as e:
            logger.warning(f"Could not update yt-dlp: {e}")
            
    except Exception as e:
        logger.error(f"Docker environment setup failed: {e}")

# Initialize the downloader
youtube_downloader = DockerYouTubeDownloader()

# Modified download function for your existing code
async def download_youtube_audio(url: str, temp_id: str) -> Optional[Path]:
    """Drop-in replacement for your existing download_youtube_audio function"""
    return await youtube_downloader.download_youtube_audio(url, temp_id)

# Health check function for Docker
async def docker_health_check():
    """Check if yt-dlp is working in Docker environment"""
    try:
        test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # Rick Roll (usually stable)
        temp_id = "health_check"
        
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 10,
        }
        
        def _test():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(test_url, download=False)
                return info is not None
        
        result = await run_in_threadpool(_test)
        return {"youtube_access": result, "status": "healthy" if result else "degraded"}
        
    except Exception as e:
        return {"youtube_access": False, "status": "unhealthy", "error": str(e)}

# Startup function to call in your main app
async def startup_docker_youtube():
    """Call this during app startup"""
    await setup_docker_environment()
    health = await docker_health_check()
    logger.info(f"Docker YouTube setup complete: {health}")
    return health