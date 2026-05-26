FROM python:3.11-slim

WORKDIR /app

# Install system deps for video processing if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create runtime dirs
RUN mkdir -p seen_ids downloads

# Default command (use incremental mode)
CMD ["python", "monitor.py"]