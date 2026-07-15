# Discord Stream Bot

A lightweight, containerized Discord bot built specifically for running on a Raspberry Pi (or similar Linux environments). This bot captures a continuous, live hardware audio feed from your PC via a USB sound card line-in or microphone and streams it directly into a Discord voice channel with minimal latency.

## Key Features
* 🐳 **Fully Containerized:** Uses Docker and Docker Compose for a clean, zero-pollution setup on your host OS.
* 🔄 **Auto-Start on Boot:** Configured to instantly spin up and reconnect if the Raspberry Pi restarts or loses power.
* 🛠️ **Hardware Independent:** Configuration settings are handled completely through environment variables—no need to touch code.
* 📜 **Automated Installer:** Includes a simple bash setup script that configures required dependencies automatically.

---

## Hardware Warning: Raspberry Pi Limitation
⚠️ **Crucial Hardware Note:** The onboard 3.5mm 4-pole jack on a Raspberry Pi is **output-only** (Stereo out + Composite video). It does not feature internal hardware capable of accepting an audio line-in or microphone signal. 

To feed audio from your PC into the Pi, you **must use an external USB Sound Card or an Audio HAT** providing a Dedicated Line-In/Microphone port. 

---

## File Structure
Place all of your project files in a single, dedicated directory:
```text
discord-stream-bot/
├── .env
├── docker-compose.yml
├── Dockerfile
├── stream_bot.py
└── setup.sh
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

## Step 2: Discord Developer Portal Requirements
For the bot to respond to text prompts, you must enable its message parsing permissions manually:
1. Open the [Discord Developer Portal](https://discord.com).
2. Click on your Application and navigate to the **Bot** tab on the left sidebar.
3. Scroll down to **Privileged Gateway Intents**.
4. Enable the **Message Content Intent** toggle switch and save your changes.

---

## Step 3: Installation & Deployment

You can deploy the entire setup automatically using the `setup.sh` script. This script updates your packages, installs Docker/Docker Compose if missing, creates a template `.env` file, and boots up the service.

1. Give the setup script permission to run:
   ```bash
   chmod +x setup.sh
   ```
2. Execute the setup script as a normal user (**do not** use `sudo` directly):
   ```bash
   ./setup.sh
   ```
3. Open the newly generated `.env` file and insert your real Discord Token and the hardware identifier found in Step 1:
   ```env
   DISCORD_TOKEN=your_actual_discord_bot_token_here
   INPUT_DEVICE=hw:1,0
   ```
4. Finalize the setup by restarting your containers to load your updated token:
   ```bash
   docker compose up -d --build
   ```

*Because the `restart: always` parameter is configured inside Docker, the service daemon will seamlessly start the bot every single time your Raspberry Pi boots up.*

---

## Useful Runtime Commands

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
