FROM python:3.11-slim

# Prevent Python from writing cached compiled .pyc tracks onto the container image
ENV PYTHONTONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies, including Rust, Cargo, ALSA, SoX, usbutils, and RTL-SDR packages
RUN apt-get update && apt-get install -y \
    ffmpeg \
    alsa-utils \
    usbutils \
    build-essential \
    libffi-dev \
    cargo \
    rustc \
    cmake \
    git \
    pkg-config \
    libusb-1.0-0-dev \
    libasound2-dev \
    sox \
    libsox-fmt-all \
    rtl-sdr \
    librtlsdr-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements including PyNaCl, encryption tools, and davey protocol layers
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir discord.py PyNaCl davey pytest pytest-cov

# Copy the application repository files into the image workspace layer
COPY . .

# Expose our structural state storage folder for volume hooks
RUN mkdir -p /data

# Run the script
CMD ["python", "stream_bot.py"]
