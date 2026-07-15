FROM python:3.11-slim

# Install system dependencies needed for audio capture and FFmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    alsa-utils \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
RUN pip install --no-cache-dir discord.py PyNaCl

# Copy the bot script into the container
COPY stream_bot.py .

# Run the script
CMD ["python", "stream_bot.py"]
