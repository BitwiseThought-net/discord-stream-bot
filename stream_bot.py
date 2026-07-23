import os
import sys
import json
import asyncio
import re
import subprocess
from datetime import datetime, timedelta
import discord
from discord import app_commands
from discord.ext import commands

# =========================================================================
# 1. ENVIRONMENT CONFIGURATION & DATA INSTANTIATIONS
# =========================================================================
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
COMMAND_NAME = os.getenv('COMMAND_BASE', 'radio')
RECOVERY_MODE = os.getenv('RECOVERY_MODE', 'resume')

STATE_FILE = "/data/state.json"
SOURCES_FILE = "/data/sources.json"
FIFO_PIPE = "/data/audio_pipe"      # Continuous shared audio stream buffer
CURRENT_TUNED_CHANNEL = "94.9M"

if not DISCORD_TOKEN:
    print("❌ Critical Error: DISCORD_TOKEN environment variable is missing.")
    sys.exit(1)

# Ensure the decoupled filesystem Named Pipe exists immediately
if not os.path.exists(FIFO_PIPE):
    try:
        os.makedirs(os.path.dirname(FIFO_PIPE), exist_ok=True)
        os.mkfifo(FIFO_PIPE)
    except Exception as e:
        print(f"❌ Failed to construct native FIFO audio stream buffer: {e}")

# Open a permanent, global Read/Write file descriptor to prevent EOF stream closures
try:
    GLOBAL_FIFO_FD = os.open(FIFO_PIPE, os.O_RDWR | os.O_NONBLOCK)
    PIPE_WRITE_HANDLE = os.fdopen(GLOBAL_FIFO_FD, "wb")
except Exception as e:
    print(f"❌ Failed to secure persistent global pipe handles: {e}")
    sys.exit(1)

class StreamBotClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # Verified Privileged Intent Parameter
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.sleep_tasks = {}  # guild_id -> active sleep worker task
        self.wake_tasks = {}   # guild_id -> active wake worker task
        
        # Explicit process tree registers to handle safe hot-swapping
        self.hardware_process = None
        self.sox_process = None
        self.ffmpeg_process = None

    async def setup_hook(self):
        self.tree.add_command(radio_group)
        await self.tree.sync()

bot = StreamBotClient()
radio_group = app_commands.Group(name=COMMAND_NAME, description="Audio hardware and SDR streaming matrix controls")

# =========================================================================
# 2. PERSISTENT LOCAL FILE STATE WRAPPERS (EXPANDED FOR PERSISTENCE)
# =========================================================================
def save_stream_state(guild_id: int, channel_id: int, selected_index: int = 0, is_active: bool = True):
    """Serializes absolute tracking boundaries and active tuning channels to disk."""
    global CURRENT_TUNED_CHANNEL
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        payload = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "selected_index": selected_index,
            "tuned_frequency": CURRENT_TUNED_CHANNEL,
            "is_active": is_active
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"⚠️ [State Storage] Failed writing configuration payload: {e}")

def clear_stream_state():
    """Toggles active connection tracking keys to false instead of dropping the file layout."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
            data["is_active"] = False
            with open(STATE_FILE, 'w') as f:
                json.dump(data, f)
    except Exception as e:
        print(f"⚠️ [State Storage] Failed updating connection state parameters: {e}")
# =========================================================================
# 3. ADVANCED HARDWARE SUBSYSTEM DISCOVERY PROTOCOLS
# =========================================================================
def discover_hardware_profile():
    """Scans system registers, saves multi-input modes, and logs persistent catalogs."""
    available_sources = []
    base_dir = "/proc/asound"

    # 1. CARD INVENTORY STAGE: Extract physical ALSA endpoints (USB Microphones)
    if os.path.exists(base_dir):
        try:
            cards = [d for d in os.listdir(base_dir) if d.startswith("card") and os.path.isdir(os.path.join(base_dir, d))]
            for card in sorted(cards):
                card_index = card.replace("card", "")
                device_string = f"plughw:{card_index},0"
                channels = "2"
                label = f"USB Microphone ({device_string})"

                stream_info = os.path.join(base_dir, card, "usbstream")
                if not os.path.exists(stream_info):
                    stream_info = os.path.join(base_dir, card, "stream0")
                if os.path.exists(stream_info):
                    with open(stream_info, 'r') as f:
                        if "1 channel" in f.read().lower():
                            channels = "1"
                            label = f"USB Mono Microphone ({device_string})"

                available_sources.append({
                    "type": "alsa",
                    "device": device_string,
                    "channels": channels,
                    "description": label
                })
        except Exception as e:
            print(f"⚠️ ALSA hardware inventory scan warning: {e}")

    # 2. USB REGISTER INVENTORY STAGE: Scan for Nooelec NESDR SMArt v5 RTL2832U chipsets
    try:
        usb_check = subprocess.run(["lsusb"], capture_output=True, text=True)
        if "0bda:2838" in usb_check.stdout or "rtl2832" in usb_check.stdout.lower():
            available_sources.extend([
                {
                    "type": "sdr_radio",
                    "device": "rtlsdr",
                    "channels": "1",
                    "description": "Listen to Radio (FM & HAM) with the USB Nooelec RTL-SDR v5"
                },
                {
                    "type": "sdr_aircraft",
                    "device": "rtlsdr",
                    "channels": "1",
                    "description": "Track Aircraft (ADS-B) with the USB Nooelec RTL-SDR v5"
                },
                {
                    "type": "sdr_satellite",
                    "device": "rtlsdr",
                    "channels": "1",
                    "description": "Receive Weather Satellite Data with the USB Nooelec RTL-SDR v5"
                }
            ])
    except Exception:
        pass

    # Fallback configuration profile layer
    if not available_sources:
        available_sources.append({
            "type": "alsa",
            "device": "plughw:1,0",
            "channels": "2",
            "description": "Default System Fallback Capture Profile (plughw:1,0)"
        })

    try:
        os.makedirs(os.path.dirname(SOURCES_FILE), exist_ok=True)
        with open(SOURCES_FILE, 'w') as f:
            json.dump(available_sources, f, indent=4)
    except Exception as e:
        print(f"⚠️ Failed writing data source payload index map: {e}")

    return available_sources
# =========================================================================
# 4. BROADCAST CORE PIPELINE HANDLERS
# =========================================================================
def stop_active_hardware_process():
    """Explicitly terminates all running hardware pipeline process layers completely."""
    for proc_attr in ['ffmpeg_process', 'sox_process', 'hardware_process']:
        proc = getattr(bot, proc_attr)
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=0.2)
            except Exception:
                try: proc.kill()
                except Exception: pass
            setattr(bot, proc_attr, None)

def spawn_hardware_capture_stream(active_source):
    """Spawns processes natively as arrays, utilizing the persistent pipe handler."""
    global CURRENT_TUNED_CHANNEL
    s_type = active_source["type"]

    stop_active_hardware_process()

    if s_type == "alsa":
        device_target = active_source['device']
        channel_count = active_source['channels']
        ffmpeg_cmd = ["ffmpeg", "-y", "-f", "alsa", "-ac", channel_count, "-i", device_target, "-f", "s16le", "-ar", "48k", "-ac", "2", "pipe:1"]
        bot.ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stdout=PIPE_WRITE_HANDLE, stderr=subprocess.DEVNULL)
    else:
        if s_type == "sdr_radio":
            rtl_cmd = ["rtl_fm", "-f", CURRENT_TUNED_CHANNEL, "-M", "wbo", "-s", "170k", "-r", "48k", "-g", "40"]
            ffmpeg_cmd = ["ffmpeg", "-y", "-f", "s16le", "-ar", "48k", "-ac", "1", "-i", "pipe:0", "-f", "s16le", "-ar", "48k", "-ac", "2", "pipe:1"]
            
            bot.hardware_process = subprocess.Popen(rtl_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            bot.ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stdin=bot.hardware_process.stdout, stdout=PIPE_WRITE_HANDLE, stderr=subprocess.DEVNULL)
        
        elif s_type == "sdr_aircraft":
            rtl_cmd = ["rtl_fm", "-f", CURRENT_TUNED_CHANNEL, "-M", "am", "-s", "25k", "-r", "24k", "-g", "48"]
            sox_cmd = ["sox", "-t", "raw", "-r", "24k", "-e", "signed-integer", "-b", "16", "-c", "1", "-", "-t", "raw", "-r", "48k", "-"]
            ffmpeg_cmd = ["ffmpeg", "-y", "-f", "s16le", "-ar", "48k", "-ac", "1", "-i", "pipe:0", "-f", "s16le", "-ar", "48k", "-ac", "2", "pipe:1"]
            
            bot.hardware_process = subprocess.Popen(rtl_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            bot.sox_process = subprocess.Popen(sox_cmd, stdin=bot.hardware_process.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            bot.ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stdin=bot.sox_process.stdout, stdout=PIPE_WRITE_HANDLE, stderr=subprocess.DEVNULL)
        
        elif s_type == "sdr_satellite":
            rtl_cmd = ["rtl_fm", "-f", CURRENT_TUNED_CHANNEL, "-M", "fm", "-s", "40k", "-r", "32k", "-g", "45"]
            sox_cmd = ["sox", "-t", "raw", "-r", "32k", "-e", "signed-integer", "-b", "16", "-c", "1", "-", "-t", "raw", "-r", "48k", "-"]
            ffmpeg_cmd = ["ffmpeg", "-y", "-f", "s16le", "-ar", "48k", "-ac", "1", "-i", "pipe:0", "-f", "s16le", "-ar", "48k", "-ac", "2", "pipe:1"]
            
            bot.hardware_process = subprocess.Popen(rtl_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            bot.sox_process = subprocess.Popen(sox_cmd, stdin=bot.hardware_process.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            bot.ffmpeg_process = subprocess.Popen(ffmpeg_cmd, stdin=bot.sox_process.stdout, stdout=PIPE_WRITE_HANDLE, stderr=subprocess.DEVNULL)

async def execute_stream_pipeline(interaction: discord.Interaction, channel: discord.VoiceChannel, force_index: int = None):
    """Binds the voice client loop to our continuous filesystem FIFO stream handle, preserving historical values."""
    global CURRENT_TUNED_CHANNEL
    current_index = 0
    
    # Load settings out of local profile database index mapping records to secure state persistence
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                saved_data = json.load(f)
            current_index = saved_data.get("selected_index", 0)
            if "tuned_frequency" in saved_data:
                CURRENT_TUNED_CHANNEL = saved_data["tuned_frequency"]
        except Exception:
            pass

    if force_index is not None:
        current_index = force_index

    if not os.path.exists(SOURCES_FILE):
        discover_hardware_profile()

    try:
        with open(SOURCES_FILE, 'r') as f:
            sources = json.load(f)
        if current_index >= len(sources):
            current_index = 0
        active_source = sources[current_index]
    except Exception:
        await interaction.followup.send("❌ Data engine error. Rebuild profiles using `/radio list`.")
        return

    try:
        vc = interaction.guild.voice_client or await channel.connect()

        # Spin up or switch the underlying hardware process safely
        spawn_hardware_capture_stream(active_source)

        # Give the background process 400ms to initialize and start feeding bytes into the pipe
        await asyncio.sleep(0.4)

        # Initialize the audio player thread ONLY if it is not currently streaming data
        if not vc.is_playing():
            audio_stream = discord.FFmpegPCMAudio(
                source=FIFO_PIPE,
                before_options="-f s16le -ar 48k -ac 2",
                pipe=False
            )
            transformer = discord.PCMVolumeTransformer(audio_stream, volume=1.0)
            vc.play(transformer)
        
        save_stream_state(interaction.guild.id, channel.id, current_index, is_active=True)
        await interaction.followup.send(f"🎙️ Connected! Stream type: **{active_source['description']}**.")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed initializing device link pipeline: {e}")

@radio_group.command(name="start", description="Initialize the active hardware pipeline stream loop")
async def start(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("You must be in a voice channel to start streaming!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    # Check if a previously selected index configuration exists, otherwise pass None to retain defaults
    await execute_stream_pipeline(interaction, interaction.user.voice.channel)
@radio_group.command(name="stop", description="Terminate audio capture channels and disconnect voice maps")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message("I am not currently connected to a voice channel.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    if guild_id in bot.sleep_tasks:
        bot.sleep_tasks[guild_id].cancel()
        del bot.sleep_tasks[guild_id]

    stop_active_hardware_process()
    clear_stream_state()
    
    await vc.disconnect()
    await interaction.response.send_message("🛑 Audio pipeline disconnected and device loops flushed.")

@radio_group.command(name="volume", description="Scale the volume parameters of the live stream transformer")
async def volume(interaction: discord.Interaction, percentage: int):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("The bot is not currently streaming!", ephemeral=True)
        return

    if not vc.source or not hasattr(vc.source, "volume"):
        await interaction.response.send_message("Volume control wrapper not ready on this stream layout.", ephemeral=True)
        return

    target_volume = max(0.0, min(float(percentage) / 100.0, 2.0))
    vc.source.volume = target_volume
    await interaction.response.send_message(f"🔊 Dynamic playback volume adjusted to **{percentage}%**.")

# =========================================================================
# 5. DEVICE CATALOG SELECTION & DYNAMIC TUNING COMMANDS
# =========================================================================
@radio_group.command(name="list", description="Re-scan and display all available input sources to stream")
async def list_sources(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    sources = discover_hardware_profile()
    
    response = "📡 **Available Hardware Capture Interfaces:**\n"
    for idx, src in enumerate(sources):
        response += f"`[{idx}]` — {src['description']}\n"
    
    response += "\n*Change inputs anytime using `/radio input <index>`.*"
    await interaction.followup.send(response)

@radio_group.command(name="input", description="Switch the current capture interface using its catalog index")
async def set_input(interaction: discord.Interaction, index: int):
    if not os.path.exists(SOURCES_FILE):
        await interaction.response.send_message("❌ Error: Device catalog not initialized. Please run `/radio list` first.", ephemeral=True)
        return

    try:
        with open(SOURCES_FILE, 'r') as f:
            sources = json.load(f)
    except Exception:
        await interaction.response.send_message("❌ Error: Failed to evaluate source registry mapping rules on disk.", ephemeral=True)
        return

    if index < 0 or index >= len(sources):
        await interaction.response.send_message(f"❌ Error: Index must be a valid target between `0` and `{len(sources) - 1}`.", ephemeral=True)
        return

    vc = interaction.guild.voice_client
    guild_id = interaction.guild.id if vc else 0
    channel_id = vc.channel.id if vc else 0

    # Persist the change immediately to our serialized files before spawning processes
    save_stream_state(guild_id, channel_id, index, is_active=(vc is not None))

    if vc and vc.is_connected():
        await interaction.response.defer(ephemeral=True)
        await execute_stream_pipeline(interaction, vc.channel, force_index=index)
    else:
        await interaction.response.send_message(f"✅ Target capture source configuration locked to input entry `[{index}]`: *{sources[index]['description']}*.")

@radio_group.command(name="channel", description="Tune the NESDR SMArt v5 receiver frequency channel link")
async def tune_channel(interaction: discord.Interaction, frequency: str):
    global CURRENT_TUNED_CHANNEL
    clean_freq = frequency.strip().upper()
    
    if clean_freq.isdigit() or re.match(r'^\d+\.\d+$', clean_freq):
        clean_freq += "M"

    if not re.match(r'^\d+(\.\d+)?[MK]?$', clean_freq):
        await interaction.response.send_message("⚠️ Invalid format syntax profile. Try layout parameters like `94.9M`, `118.1M`, or `162.4M`.", ephemeral=True)
        return

    CURRENT_TUNED_CHANNEL = clean_freq
    vc = interaction.guild.voice_client
    
    if vc and vc.is_connected():
        await interaction.response.defer(ephemeral=True)
        
        current_index = 0
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r') as f:
                    current_index = json.load(f).get("selected_index", 0)
        except Exception:
            pass

        save_stream_state(interaction.guild.id, vc.channel.id, current_index, is_active=True)
        await execute_stream_pipeline(interaction, vc.channel, force_index=current_index)
    else:
        # If not streaming, cache the chosen frequency parameter to the file system right away
        current_index = 0
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    current_index = json.load(f).get("selected_index", 0)
            except Exception: pass
        save_stream_state(0, 0, current_index, is_active=False)
        await interaction.response.send_message(f"📡 Tuner frequency baseline channel set to **{clean_freq}** for next SDR stream run.")
# =========================================================================
# 6. TIMED OPERATION SCHEDULERS (SLEEP / WAKE ENGINE)
# =========================================================================
def parse_duration_to_seconds(duration_str: str) -> int:
    """Parses relative time codes or clean absolute AM/PM positional strings."""
    clean_str = duration_str.strip().lower()

    rel_match = re.match(r'^([\d.]+)\s*([smh])$', clean_str)
    if rel_match:
        val = float(rel_match.group(1))
        unit = rel_match.group(2)
        if unit == 's': return int(val)
        if unit == 'm': return int(val * 60)
        if unit == 'h': return int(val * 3600)

    abs_match = re.match(r'^(\d{1,2}):(\d{2})\s*(am|pm)?$', clean_str)
    if abs_match:
        target_hr = int(abs_match.group(1))
        target_mn = int(abs_match.group(2))
        period = abs_match.group(3)

        if period == 'pm' and target_hr < 12: target_hr += 12
        elif period == 'am' and target_hr == 12: target_hr = 0

        now = datetime.now()
        target_time = now.replace(hour=target_hr, minute=target_mn, second=0, microsecond=0)
        if target_time <= now:
            target_time += timedelta(days=1)
        return int((target_time - now).total_seconds())

    raise ValueError("Invalid time match profile formatting.")

async def sleep_timer_worker(guild_id: int, delay: int):
    await asyncio.sleep(delay)
    guild = bot.get_guild(guild_id)
    if guild and guild.voice_client:
        stop_active_hardware_process()
        clear_stream_state()
        await guild.voice_client.disconnect()
    if guild_id in bot.sleep_tasks:
        del bot.sleep_tasks[guild_id]

@radio_group.command(name="sleep", description="Establish an absolute timer target to step down device capture runs")
async def sleep(interaction: discord.Interaction, duration: str):
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message("The bot must be connected to a voice channel to set a sleep timer!", ephemeral=True)
        return

    try:
        seconds = parse_duration_to_seconds(duration)
    except ValueError:
        if any(char in duration.lower() for char in ['x', 'z', 'y']):
            await interaction.response.send_message("⚠️ Unrecognized duration unit. Please use seconds, minutes, or hours.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Invalid time string format. Try inputs like `30m`, `1.5h`, or `11:45pm`.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    if guild_id in bot.sleep_tasks:
        bot.sleep_tasks[guild_id].cancel()

    task = asyncio.create_task(sleep_timer_worker(guild_id, seconds))
    bot.sleep_tasks[guild_id] = task
    await interaction.response.send_message(f"🌙 Sleep timer locked. Audio feeds drop out in **{duration}**.")

async def wake_timer_worker(guild_id: int, channel_id: int, delay: int):
    await asyncio.sleep(delay)
    channel = bot.get_channel(channel_id)
    if channel and isinstance(channel, discord.VoiceChannel):
        class WakeInteractionObject:
            def __init__(self, g, ch):
                self.guild = g
                self.user = discord.Object(id=0)
                self.user.voice = discord.Object(id=0)
                self.user.voice.channel = ch
                self.response = discord.Object(id=0)
            async def defer(self, ephemeral=True): pass
            class followup:
                @staticmethod
                async def send(content): print(f"📢 [Wake Scheduler] {content}")

        fake_interaction = WakeInteractionObject(channel.guild, channel)
        await execute_stream_pipeline(fake_interaction, channel)
    if guild_id in bot.wake_tasks:
        del bot.wake_tasks[guild_id]

@radio_group.command(name="wake", description="Automatically boot and run streams when tracking clocks hit boundaries")
async def wake(interaction: discord.Interaction, duration: str):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("⚠️ You must be inside a voice channel when running this command so the bot knows where to connect!", ephemeral=True)
        return

    try:
        seconds = parse_duration_to_seconds(duration)
    except ValueError:
        await interaction.response.send_message("⚠️ Unrecognized wake duration unit. Use seconds, minutes, or hours.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    channel_id = interaction.user.voice.channel.id

    if guild_id in bot.wake_tasks:
        bot.wake_tasks[guild_id].cancel()

    task = asyncio.create_task(wake_timer_worker(guild_id, channel_id, seconds))
    bot.wake_tasks[guild_id] = task
    await interaction.response.send_message(f"⏰ Wake timer initialized. Broadcasting starts automatically in **{duration}**.")

# =========================================================================
# 7. CRASH RECOVERY LIFECYCLES & STARTUP HOOKS (FULLY SYSTEM INTEGRATED)
# =========================================================================
@bot.event
async def on_ready():
    global CURRENT_TUNED_CHANNEL
    print(f"🤖 Automated profile online. Logged in as: {bot.user.name}")
    
    if RECOVERY_MODE == "stay_disconnected":
        print("🔄 [Recovery] Stay disconnected policy enforced. Skipping historical trace loops.")
        return

    if not os.path.exists(STATE_FILE):
        print("🔄 [Recovery] Clean boot pipeline detected. No data traces saved to disk.")
        return

    try:
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
        
        # FIXED: Only trigger auto-reconnection loops if the bot was actively streaming before the reboot/crash
        if not data.get("is_active", True):
            print("🔄 [Recovery] Found dormant configuration profile records. Retaining cached parameters without auto-connecting.")
            if "tuned_frequency" in data:
                CURRENT_TUNED_CHANNEL = data["tuned_frequency"]
            return

        guild_id = data.get("guild_id")
        channel_id = data.get("channel_id")
        current_index = data.get("selected_index", 0)
        if "tuned_frequency" in data:
            CURRENT_TUNED_CHANNEL = data["tuned_frequency"]

        channel = bot.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.VoiceChannel):
            print("🔄 [Recovery] Saved channel context is invalid or deleted. Wiping trace file mappings.")
            clear_stream_state()
            return

        print(f"🔄 [Recovery] Resuming broadcast on target channel map: {channel.name} at frequency {CURRENT_TUNED_CHANNEL}")
        
        class SynthesizedInteraction:
            def __init__(self, g, ch):
                self.guild = g
                self.user = discord.Object(id=0)
                self.user.voice = discord.Object(id=0)
                self.user.voice.channel = ch
                self.response = discord.Object(id=0)
            async def defer(self, ephemeral=True): pass
            class followup:
                @staticmethod
                async def send(content): print(f"📢 [Recovery Notice] {content}")

        fake_interaction = SynthesizedInteraction(channel.guild, channel)
        await execute_stream_pipeline(fake_interaction, channel, force_index=current_index)
        print("🔄 [Recovery] State resume completed successfully.")
    except Exception as e:
        print(f"❌ [Recovery] Internal failure processing recovery routine payload: {e}")
        clear_stream_state()

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)

