FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Create required runtime directories
RUN mkdir -p uploads outputs/compliance outputs/evidence outputs/evidence_images outputs/audit database logs

# Expose port (Render sets $PORT dynamically)
ENV PORT=8000
EXPOSE 8000

# Start FastAPI server
CMD ["sh", "-c", "uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT}"]
