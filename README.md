[![Tests](https://github.com/BitwiseThought-net/discord-stream-bot/actions/workflows/tests-python.yml/badge.svg)](https://github.com/BitwiseThought-net/discord-stream-bot/actions/workflows/tests-python.yml)
[![Coverage](https://raw.githubusercontent.com/BitwiseThought-net/discord-stream-bot/main/badges/coverage-badge.svg)](https://github.com/BitwiseThought-net/discord-stream-bot/actions/workflows/tests-python.yml)
[![Tests Passing](https://raw.githubusercontent.com/BitwiseThought-net/discord-stream-bot/main/badges/tests-badge.svg)](https://github.com/BitwiseThought-net/discord-stream-bot/actions/workflows/tests-python.yml)

# Discord Stream Bot

A lightweight, containerized Discord bot built for Linux environments (including compact single-board systems like the Raspberry Pi). This bot captures a continuous, live hardware audio feed from your computer's line-in or microphone interface and streams it directly into a Discord voice channel with minimal latency using modern native slash commands.

---

## Key Features
* 🐳 **Fully Containerized:** Uses Docker and Docker Compose for a clean, zero-pollution setup on your host OS.
* 🔄 **Auto-Start on Boot:** Configured to instantly spin up and reconnect if the host environment restarts or loses power.
* 🛠️ **Zero Manual Coding Configuration:** Configuration settings are handled completely through environment variables.
* 📜 **Automated Installer:** Includes a cross-distro bash setup script that configures required dependencies automatically across Debian/Ubuntu, Fedora/RHEL, and Arch Linux ecosystems.
* 🚀 **Custom Dynamic Slash Commands:** The root name of the slash command group can be parameterized right in your environment configuration (e.g., `/radio start` vs `/stream start`).
* 🔍 **Automated Hardware Discovery:** The bot scans hardware capabilities on startup to automatically determine correct ALSA card assignments and mono/stereo channel capability mappings.
* 🎵 **On-the-Fly Volume Control:** Allows users to slide audio volume constraints smoothly between 0% and 100% at runtime natively in Discord.
* 💤 **Sleep & Wake Timers:** Features flexible absolute (e.g., `3:34pm`) and relative (e.g., `45m`, `1.5h`) duration arguments to handle automated connection and disconnection rules.
* 💾 **Persistent Crash Recovery Engine:** Tracks streaming coordinates securely via a read-write volume mounting layer so the bot can automatically resume its previous voice broadcast channel location on boot.

---

## Hardware Warning: Audio Input Constraints
⚠️ **Crucial Hardware Note:** Certain compact host systems or single-board computers (such as standard Raspberry Pi hardware) feature 3.5mm onboard audio jacks that are strictly **output-only** (Stereo sound out + Composite video out). They lack internal design architecture to parse a microphone line-in signal natively. 

If you are deploying on a machine lacking integrated audio capture interfaces, you **must use an external USB Sound Card or an Audio HAT** providing a Dedicated Line-In/Microphone input matrix to feed audio from your sound source into the host OS.

---

## File Structure
Place all of your project files in a single, dedicated directory:
```text
discord-stream-bot/
├── .env
├── docker-compose.yml
├── Dockerfile
├── bot.py
└── setup.sh
```

---

## Step 1: Discord Developer Portal Requirements & Invite Link
Because this framework uses slash commands, you **do not need** to turn on the "Message Content Intent" toggle. Follow these steps to configure application scopes and create your server invite link:

1. Open the [Discord Developer Portal](https://discord.com/developers/home) and select your application dashboard.
2. Navigate to the **OAuth2** tab in the left sidebar, then click on **URL Generator**.
3. Under the **Scopes** section, check the following two boxes:
   * [x] `bot`
   * [x] `applications.commands` *(This allows your bot to inject custom subcommands directly into Discord's interface)*
4. Scroll down to the **Bot Permissions** section that appears below and select:
   * **Text Permissions:** `Send Messages`
   * **Voice Permissions:** `Connect` and `Speak`
5. Copy the generated URL string at the bottom of the page. Open this link in a browser tab to authorize and add the bot to your chosen Discord server.

---

## Step 2: Installation & Deployment

You can deploy the entire setup automatically using the `setup.sh` script. This script automatically detects your Linux base, updates native package mirrors, configures Docker and Docker Compose layers if missing, provisions configuration files, and analyzes your audio device cards.

1. Give the setup script permissions to execute:
   ```bash
   chmod +x setup.sh
   ```
2. Execute the setup script as a normal user (**do not** execute using `sudo` explicitly):
   ```bash
   ./setup.sh
   ```
3. Open the newly generated `.env` file and insert your real Discord Token and desired structural configurations:
   ```env
   DISCORD_TOKEN=your_actual_discord_bot_token_here
   COMMAND_BASE=radio
   RECOVERY_MODE=resume
   ```

### RECOVERY_MODE Options
The `RECOVERY_MODE` variable manages how the bot responds when the container engine restarts or reboots:
* `resume` – Automatically checks the persistent storage layer for previous broadcast sessions. If a crash or restart occurs mid-stream, the bot immediately rejoins the last active voice channel and resumes the audio feed.
* `stay_disconnected` – Wipes any historical streaming footprints on startup. The bot stays completely disconnected and idle until a user manually issues a start command in chat.

4. Finalize the setup by compiling your container infrastructure to load your updated token parameters:
   ```bash
   docker compose up -d --build
   ```

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
*Assuming `COMMAND_BASE=radio` inside your `.env` configuration file (type these instructions into any text channel visible to the bot inside your authorized server):*

* `/radio start` - Directs the bot to join your active voice channel, auto-detect hardware variables, and begin piping your live PC audio stream.
* `/radio stop` - Terminates the active FFmpeg session, flushes current crash recovery data states, and safely disconnects the bot from voice.
* `/radio volume <0-100>` - Dynamically modifies stream playback amplitude parameters on the fly via a native integer slider inside Discord.
* `/radio sleep <time>` - Configures a sleep timer to automatically disconnect after a set time. Accepts relative intervals (e.g., `45s`, `15m`, `1.5h`) or absolute timeline positions (e.g., `11:45pm`, `14:30`).
* `/radio wake <time>` - Configures a wake timer based on the user's active channel position. When the absolute or relative duration value hits zero, the bot automatically wakes up, joins that voice slot, and resumes encoding live audio.

