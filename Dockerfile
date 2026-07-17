FROM python:3.11-slim

# Install system dependencies, including Rust and Cargo for compiling encryption extensions
RUN apt-get update && apt-get install -y \
    ffmpeg \
    alsa-utils \
    build-essential \
    libffi-dev \
    cargo \
    rustc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements including the newly required davey protocol module
RUN pip install --no-cache-dir discord.py PyNaCl davey

# Copy the bot script into the container
COPY stream_bot.py .

# Run the script
CMD ["python", "stream_bot.py"]
