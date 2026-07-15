# Discord Stream Bot

A lightweight, containerized Discord bot built specifically for running on a Raspberry Pi (or similar Linux environments). This bot captures a continuous, live hardware audio feed from your PC via a USB sound card line-in or microphone and streams it directly into a Discord voice channel with minimal latency.

## Key Features
* 🐳 **Fully Containerized:** Uses Docker and Docker Compose for a clean, zero-pollution setup on your host OS.
* 🔄 **Auto-Start on Boot:** Configured to instantly spin up and reconnect if the Raspberry Pi restarts or loses power.
* 🛠️ **Hardware Independent:** Configuration settings are handled completely through environment variables—no need to touch code.

---

## Hardware Warning: Raspberry Pi Limitation
⚠️ **Crucial Hardware Note:** The onboard 3.5mm 4-pole jack on a Raspberry Pi is **output-only** (Stereo out + Composite video). It does not feature internal hardware capable of accepting an audio line-in or microphone signal. 

To feed audio from your PC into the Pi, you **must use an external USB Sound Card or an Audio HAT** providing a Dedicated Line-In/Microphone port. 

---

## Prerequisites
Ensure the following software packages are installed on your Raspberry Pi:
* [Docker Engine](https://docker.com)
* [Docker Compose](https://docker.com)

---

## File Structure
Place all of your project files in a single, dedicated directory:
```text
discord-stream-bot/
├── .env
├── docker-compose.yml
├── Dockerfile
└── stream_bot.py
```

---

## Step 1: Find Your Hardware Device Input Name
Before configuring the bot, you must find out which hardware index ALSA (the Linux sound subsystem) has assigned to your USB audio device. 

Run the following command on your Raspberry Pi terminal:
```bash
arecord -l
```

Look for your USB audio input. The output will look something like this:
```text
**** List of CAPTURE Hardware Devices ****
card 1: USB [USB Audio], device 0: USB Audio [USB Audio]
```
Note the **card number** and **device number**. In the example above (Card 1, Device 0), the hardware identifier format is `hw:1,0`.

---

## Step 2: Create the Project Files

### 1. `.env`
Create a file named `.env` and fill it with your Discord Bot Token and the hardware identifier found in Step 1.
```env
DISCORD_TOKEN=your_actual_discord_bot_token_here
INPUT_DEVICE=hw:1,0
```

### 2. `docker-compose.yml`
Create your service orchestration file. The `devices` block explicitly allows the isolated container to map straight into the Pi's hardware layer (`/dev/snd`) with zero audio performance penalty.
```yaml
services:
  discord-bot:
    build: .
    container_name: discord_audio_bot
    restart: always
    devices:
      - /dev/snd:/dev/snd
    environment:
      - DISCORD_TOKEN=${DISCORD_TOKEN}
      - INPUT_DEVICE=${INPUT_DEVICE}
```

### 3. `Dockerfile`
Create the build recipe for the runtime environment. This image leverages a minimal Debian footprint and bundles Python with native system dependencies like FFmpeg.
```dockerfile
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
```

### 4. `stream_bot.py`
Create the main bot logic script.
```python
import os
import discord
from discord.ext import commands

TOKEN = os.getenv('DISCORD_TOKEN')
INPUT_DEVICE = os.getenv('INPUT_DEVICE', 'hw:1,0') 

intents = discord.Intents.default()
intents.message_content = True 
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Streaming bot successfully logged in as {bot.user.name}")
    print(f"Configured hardware input device: {INPUT_DEVICE}")

@bot.command()
async def start(ctx):
    """Joins the user's voice channel and starts streaming live hardware audio."""
    if not ctx.author.voice:
        await ctx.send("You must be in a voice channel to start streaming!")
        return

    channel = ctx.author.voice.channel
    vc = await channel.connect()

    ffmpeg_options = {
        'options': f'-f alsa -ac 2 -ar 44100 -i {INPUT_DEVICE}',
        'before_options': '-nostdin'
    }

    await ctx.send(f"Connected! Now streaming live hardware audio from input: {INPUT_DEVICE}")
    vc.play(discord.FFmpegPCMAudio(None, **ffmpeg_options))

@bot.command()
async def stop(ctx):
    """Stops streaming and leaves the voice channel."""
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Stopped streaming and disconnected.")
    else:
        await ctx.send("I am not currently connected to a voice channel.")

if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN variable is missing. Check your .env file.")
    else:
        bot.run(TOKEN)
```

---

## Step 3: Discord Developer Portal Requirements
For the bot to respond to text prompts, you must enable its message parsing permissions manually:
1. Open the [Discord Developer Portal](https://discord.com).
2. Click on your Application and navigate to the **Bot** tab on the left sidebar.
3. Scroll down to **Privileged Gateway Intents**.
4. Enable the **Message Content Intent** toggle switch and save your changes.

---

## Step 4: Installation & Deployment

To build the image and fire up your live bot service in detached (background) mode, run:
```bash
docker compose up -d --build
```
*Because the `restart: always` parameter is declared in the compose block, the Docker service daemon will seamlessly start the bot every single time the host machine boots up.*

### Useful Runtime Commands

* **View Active Stream Logs (Troubleshooting):**
  ```bash
  docker compose logs -f
  ```
* **Shutdown the Bot Service:**
  ```bash
  docker compose down
  ```
* **Restart the Bot Container Manually:**
  ```bash
  docker compose restart
  ```

---

## Commands
* `!start` - Directs the bot to join your active voice channel and begin encoding and piping your hardware stream.
* `!stop` - Stops the FFmpeg stream and disconnects the bot safely from voice.
