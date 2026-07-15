# Discord Stream Bot

A lightweight, containerized Discord bot built for Linux environments. This bot captures a continuous, live hardware audio feed from your computer's line-in or microphone interface and streams it directly into a Discord voice channel with minimal latency using modern native slash commands.

## Key Features
* 🐳 **Fully Containerized:** Uses Docker and Docker Compose for a clean, zero-pollution setup on your host OS.
* 🔄 **Auto-Start on Boot:** Configured to instantly spin up and reconnect if the host environment restarts or loses power.
* 🛠️ **Hardware Independent:** Configuration settings are handled completely through environment variables—no need to touch code.
* 📜 **Automated Installer:** Includes a cross-distro bash setup script that configures required dependencies automatically across Debian/Ubuntu, Fedora/RHEL, and Arch Linux ecosystems.
* 🚀 **Slash Commands:** Integrated via native Discord application commands (`/start` and `/stop`) requiring no privileged message intents.

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
├── stream_bot.py
└── setup.sh
```

---

## Step 1: Find Your Hardware Device Input Name
Before configuring the bot, you must find out which hardware index ALSA (the Linux sound subsystem) has assigned to your audio interface capture device.

Run the following command on your terminal:
```bash
arecord -l
```

Look for your target audio input source. The output will look something like this:
```text
**** List of CAPTURE Hardware Devices ****
card 1: USB [USB Audio], device 0: USB Audio [USB Audio]
```
Note the **card number** and **device number**. In the example above (Card 1, Device 0), the hardware identifier format is `hw:1,0`.

---

## Step 2: Discord Developer Portal Requirements & Invite Link
Because this framework uses slash commands, you **do not need** to turn on the "Message Content Intent" toggle. Follow these steps to configure application scopes and create your server invite link:

1. Open the [Discord Developer Portal](https://discord.com) and select your application dashboard.
2. Navigate to the **OAuth2** tab in the left sidebar, then click on **URL Generator**.
3. Under the **Scopes** section, check the following two boxes:
   * [x] `bot`
   * [x] `applications.commands` *(This allows your bot to inject `/start` and `/stop` directly into Discord's interface)*
4. Scroll down to the **Bot Permissions** section that appears below and select:
   * **Text Permissions:** `Send Messages`
   * **Voice Permissions:** `Connect` and `Speak`
5. Copy the generated URL string at the bottom of the page. Open this link in a browser tab to authorize and add the bot to your chosen Discord server.

---

## Step 3: Installation & Deployment

You can deploy the entire setup automatically using the `setup.sh` script. This script automatically detects your Linux base, updates native package mirrors, configures Docker and Docker Compose layers if missing, provisions configuration files, and deploys the background process.

1. Give the setup script permissions to execute:
   ```bash
   chmod +x setup.sh
   ```
2. Execute the setup script as a normal user (**do not** execute using `sudo` explicitly):
   ```bash
   ./setup.sh
   ```
3. Open the newly generated `.env` file and insert your real Discord Token and the hardware identifier found in Step 1:
   ```env
   DISCORD_TOKEN=your_actual_discord_bot_token_here
   INPUT_DEVICE=hw:1,0
   ```
4. Finalize the setup by restarting your container engine parameters to load your updated token variables:
   ```bash
   docker compose up -d --build
   ```

*Because the `restart: always` directive is declared within the compose block, the system daemon architecture will seamlessly spin up the stream application instance every single time your host machine undergoes a boot execution lifecycle.*

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
Type these commands into any text channel visible to the bot inside your authorized server:

* `/start` - Directs the bot to join your active voice channel, initialize hardware layouts, and begin piping your live PC audio stream feed.
* `/stop` - Terminates the active FFmpeg audio recording channel and safely disconnects the bot from voice.
