FROM python:3.12-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    livekit-api \
    livekit-protocol \
    aiohttp

# Copy application
COPY app.py .

# Run the service
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
