FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create runtime dirs
RUN mkdir -p seen_ids downloads

# Default command (use incremental mode)
CMD ["python", "monitor.py"]