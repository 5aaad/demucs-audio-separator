# Dockerfile optimized for YouTube downloads
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    curl \
    ca-certificates \
    build-essential \
    gcc \
    git \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install latest yt-dlp
RUN pip install --no-cache-dir --upgrade yt-dlp

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p /tmp && chmod 777 /tmp

# Set environment variables for better Docker compatibility
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV YT_DLP_CACHE_DIR=/tmp/yt-dlp-cache
ENV TMPDIR=/tmp

# Create yt-dlp cache directory
RUN mkdir -p $YT_DLP_CACHE_DIR && chmod 777 $YT_DLP_CACHE_DIR

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["gunicorn", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "main:app", "--bind", "0.0.0.0:8000", "--timeout", "300", "--worker-connections", "1000", "--max-requests", "1000", "--max-requests-jitter", "100"]
