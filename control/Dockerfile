FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (ffmpeg for audio processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    livekit-api \
    livekit-protocol \
    aiohttp \
    boto3 \
    assemblyai

# Copy application
COPY app.py .

# Run the service
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
