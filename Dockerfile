FROM python:3.11-slim

WORKDIR /app

# Create non-root user
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create runtime dirs and set ownership
RUN mkdir -p seen_ids downloads && chown -R appuser:appgroup /app

USER appuser

# Default command (use incremental mode)
CMD ["python", "monitor.py"]