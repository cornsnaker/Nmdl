FROM python:3.12-slim

# System deps for building any C extensions if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first to leverage Docker layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

CMD ["python", "-m", "bot"]
