import os
import re
import json
import asyncio
import discord
from discord import app_commands
from datetime import datetime, timedelta

TOKEN = os.getenv('DISCORD_TOKEN')
COMMAND_NAME = os.getenv('COMMAND_BASE', 'stream').lower().strip()
RECOVERY_MODE = os.getenv('RECOVERY_MODE', 'stay_disconnected').lower().strip()

STATE_FILE = "/data/state.json"

intents = discord.Intents.default()

def save_stream_state(guild_id: int, channel_id: int):
    """Writes the active voice execution profile coordinates to the persistent file layer."""
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        state_data = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "timestamp": datetime.now().isoformat()
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(state_data, f)
        print(f"💾 [State Persist] Active stream target saved for Channel: {channel_id}")
    except Exception as e:
        print(f"⚠️ [State Persist] Failed to write file snapshot: {e}")

def clear_stream_state():
    """Wipes the file tracking markers when the stream terminates cleanly or errors out."""
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            print("💾 [State Persist] Disconnected cleanly. State configuration flushed.")
    except Exception as e:
        print(f"⚠️ [State Persist] Failed to clear snapshot targets: {e}")

def discover_hardware_profile():
    """Scans /mnt/asound natively to resolve current active USB soundcards and mapping channels."""
    base_dir = "/mnt/asound"
    default_device = "plughw:1,0"
    default_channels = "2"

    if not os.path.exists(base_dir):
        print("⚠️ [Discovery] /mnt/asound mount missing. Falling back to defaults.")
        return default_device, default_channels

    for entry in os.listdir(base_dir):
        if entry.startswith("card") and os.path.isdir(os.path.join(base_dir, entry)):
            card_num = entry.replace("card", "").strip()
            stream_file = os.path.join(base_dir, entry, "stream0")

            if os.path.exists(stream_file):
                try:
                    with open(stream_file, 'r') as f:
                        content = f.read().lower()

                    discovered_device = f"plughw:{card_num},0"
                    if "1 channel" in content and "2 channels" not in content:
                        print(f"🎯 [Discovery] Dynamically mapped active MONO USB device: {discovered_device}")
                        return discovered_device, '1'
                    else:
                        print(f"🎯 [Discovery] Dynamically mapped active STEREO USB device: {discovered_device}")
                        return discovered_device, '2'
                except Exception as e:
                    print(f"⚠️ [Discovery] Error parsing stream file for card {card_num}: {e}")

    print("⚠️ [Discovery] No active USB audio stream profiles found. Deploying system defaults.")
    return default_device, default_channels

class StreamBot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.sleep_tasks = {}
        self.wake_tasks = {}

    async def setup_hook(self):
        print(f"Creating dynamic slash command group: /{COMMAND_NAME}")
        stream_group = app_commands.Group(
            name=COMMAND_NAME, 
            description=f"Commands to manage the live audio {COMMAND_NAME}."
        )

        @stream_group.command(name="start", description="Joins your voice channel and starts the live audio feed.")
        async def start_stream(interaction: discord.Interaction):
            if not interaction.user.voice:
                await interaction.response.send_message("You must be in a voice channel to start streaming!", ephemeral=True)
                return

            channel = interaction.user.voice.channel
            await interaction.response.send_message(f"Connecting to {channel.name}... Please wait.")

            vc = await channel.connect()
            input_device, detected_channels = discover_hardware_profile()

            ffmpeg_options = {
                'before_options': f'-nostdin -f alsa -ac {detected_channels} -ar 44100',
                'options': ''
            }

            save_stream_state(interaction.guild.id, channel.id)

            await interaction.followup.send(f"🎵 Now streaming.")
            raw_source = discord.FFmpegPCMAudio(input_device, **ffmpeg_options)
            vc.play(discord.PCMVolumeTransformer(raw_source, volume=1.0))

        @stream_group.command(name="stop", description="Stops the active feed and leaves the voice channel.")
        async def stop_stream(interaction: discord.Interaction):
            guild_id = interaction.guild.id
            if guild_id in self.sleep_tasks:
                self.sleep_tasks[guild_id].cancel()
                del self.sleep_tasks[guild_id]

            clear_stream_state()

            if interaction.guild.voice_client:
                await interaction.guild.voice_client.disconnect()
                await interaction.response.send_message("Stopped streaming and disconnected.")
            else:
                await interaction.response.send_message("Not currently connected to a voice channel.", ephemeral=True)

        @stream_group.command(name="volume", description="Adjusts the volume of the audio feed on the fly.")
        @app_commands.describe(percentage="The target volume percentage from 0 to 100.")
        async def adjust_volume(interaction: discord.Interaction, percentage: app_commands.Range[int, 0, 100]):
            vc = interaction.guild.voice_client
            if not vc or not vc.source:
                await interaction.response.send_message("The {COMMAND_NAME} is not currently streaming!", ephemeral=True)
                return

            if isinstance(vc.source, discord.PCMVolumeTransformer):
                vc.source.volume = percentage / 100.0
                await interaction.response.send_message(f"🔊 Volume set to **{percentage}%**.")
            else:
                await interaction.response.send_message("Volume control wrapper not ready on this stream layout.", ephemeral=True)

        @stream_group.command(name="sleep", description="Sets a sleep timer to automatically turn off the stream.")
        @app_commands.describe(time="Time string like '3:34pm', '45s', '15m', or '1.5h'.")
        async def sleep_timer(interaction: discord.Interaction, time: str):
            guild_id = interaction.guild.id
            vc = interaction.guild.voice_client

            if not vc:
                await interaction.response.send_message("The {bot.user.name} must be connected to a voice channel to set a sleep timer!", ephemeral=True)
                return

            raw_input = time.lower().strip()
            delay_seconds = None

            relative_match = re.match(r"^([0-9.]+)\s*([a-z]+)$", raw_input)
            if relative_match:
                value = float(relative_match.group(1))
                unit = relative_match.group(2)
                if unit in ['s', 'sec', 'second', 'seconds']: delay_seconds = value
                elif unit in ['m', 'min', 'minute', 'minutes']: delay_seconds = value * 60
                elif unit in ['h', 'hr', 'hour', 'hours']: delay_seconds = value * 3600
                else:
                    await interaction.response.send_message("⚠️ Unrecognized time unit. Please use seconds, minutes, or hours.", ephemeral=True)
                    return
            else:
                time_formats = ["%I:%M%p", "%I:%M %p", "%H:%M"]
                now = datetime.now()
                for fmt in time_formats:
                    try:
                        parsed_time = datetime.strptime(raw_input.upper(), fmt)
                        target_dt = now.replace(hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0)
                        if target_dt < now: target_dt += timedelta(days=1)
                        delay_seconds = (target_dt - now).total_seconds()
                        break
                    except ValueError: continue

            if delay_seconds is None or delay_seconds <= 0:
                await interaction.response.send_message("⚠️ Invalid time string format. Try inputs like `30m`, `1.5h`, or `11:45pm`.", ephemeral=True)
                return

            if guild_id in self.sleep_tasks:
                self.sleep_tasks[guild_id].cancel()

            async def sleep_worker(seconds, voice_client):
                await asyncio.sleep(seconds)
                clear_stream_state()
                if voice_client and voice_client.is_connected():
                    await voice_client.disconnect()
                    print(f"💤 Sleep timer reached. Automatically disconnected from server guild: {guild_id}")
                if guild_id in self.sleep_tasks:
                    del self.sleep_tasks[guild_id]

            task = asyncio.create_task(sleep_worker(delay_seconds, vc))
            self.sleep_tasks[guild_id] = task

            total_minutes = int(delay_seconds // 60)
            remaining_seconds = int(delay_seconds % 60)
            time_display = f"{total_minutes}m {remaining_seconds}s" if total_minutes > 0 else f"{remaining_seconds} seconds"
            await interaction.response.send_message(f"💤 Sleep timer set. {COMMAND_NAME.capitalize()} will turn off in **{time_display}**.")

        @stream_group.command(name="wake", description="Sets a wake timer to automatically turn on the stream.")
        @app_commands.describe(time="Time string like '7:00am', '10s', '5m', or '1h'.")
        async def wake_timer(interaction: discord.Interaction, time: str):
            guild_id = interaction.guild.id
            if not interaction.user.voice:
                await interaction.response.send_message("⚠️ You must be inside a voice channel when running this command so the {bot.user.name} knows where to connect!", ephemeral=True)
                return

            target_channel = interaction.user.voice.channel
            raw_input = time.lower().strip()
            delay_seconds = None

            relative_match = re.match(r"^([0-9.]+)\s*([a-z]+)$", raw_input)
            if relative_match:
                value = float(relative_match.group(1))
                unit = relative_match.group(2)
                if unit in ['s', 'sec', 'second', 'seconds']: delay_seconds = value
                elif unit in ['m', 'min', 'minute', 'minutes']: delay_seconds = value * 60
                elif unit in ['h', 'hr', 'hour', 'hours']: delay_seconds = value * 3600
                else:
                    await interaction.response.send_message("⚠️ Unrecognized wake time unit. Use seconds, minutes, or hours.", ephemeral=True)
                    return
            else:
                time_formats = ["%I:%M%p", "%I:%M %p", "%H:%M"]
                now = datetime.now()
                for fmt in time_formats:
                    try:
                        parsed_time = datetime.strptime(raw_input.upper(), fmt)
                        target_dt = now.replace(hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0)
                        if target_dt < now: target_dt += timedelta(days=1)
                        delay_seconds = (target_dt - now).total_seconds()
                        break
                    except ValueError: continue

            if delay_seconds is None or delay_seconds <= 0:
                await interaction.response.send_message("⚠️ Invalid wake time format. Try inputs like `10m`, `1h`, or `7:30am`.", ephemeral=True)
                return

            if guild_id in self.wake_tasks:
                self.wake_tasks[guild_id].cancel()

            async def wake_worker(seconds, channel_target):
                await asyncio.sleep(seconds)
                if interaction.guild.voice_client:
                    if interaction.guild.voice_client.is_connected():
                        await interaction.guild.voice_client.disconnect()
                try:
                    vc = await channel_target.connect()
                    input_device, detected_channels = discover_hardware_profile()

                    save_stream_state(guild_id, channel_target.id)

                    ffmpeg_options = {
                        'before_options': f'-nostdin -f alsa -ac {detected_channels} -ar 44100',
                        'options': ''
                    }
                    raw_source = discord.FFmpegPCMAudio(input_device, **ffmpeg_options)
                    vc.play(discord.PCMVolumeTransformer(raw_source, volume=1.0))
                    print(f"⏰ Wake timer reached! Automatically streaming to channel: {channel_target.name}")
                except Exception as e:
                    print(f"❌ Wake timer worker failed to connect/stream: {e}")

                if guild_id in self.wake_tasks:
                    del self.wake_tasks[guild_id]

            task = asyncio.create_task(wake_worker(delay_seconds, target_channel))
            self.wake_tasks[guild_id] = task

            total_minutes = int(delay_seconds // 60)
            remaining_seconds = int(delay_seconds % 60)
            time_display = f"{total_minutes}m {remaining_seconds}s" if total_minutes > 0 else f"{remaining_seconds} seconds"
            await interaction.response.send_message(f"⏰ Wake timer set. {COMMAND_NAME.capitalize()} will automatically start streaming in **{time_display}**.")

        self.tree.add_command(stream_group)
        print("Syncing slash commands globally...")
        await self.tree.sync()

bot = StreamBot(intents=intents)

@bot.event
async def on_ready():
    print(f"Streaming {COMMAND_NAME} successfully logged in as {bot.user.name}")
    dev, ch = discover_hardware_profile()
    print(f"Startup scan check -> Device: {dev} | Channels: {ch}")
    print(f"Configured recovery initialization policy mode: '{RECOVERY_MODE}'")

    if RECOVERY_MODE == "resume":
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)

                guild_id = state.get("guild_id")
                channel_id = state.get("channel_id")

                print(f"🔄 [Recovery] Active crash footprint trace discovered for Channel ID: {channel_id}")
                channel = bot.get_channel(channel_id)

                if channel and isinstance(channel, discord.VoiceChannel):
                    print(f"🔄 [Recovery] Reconnecting to channel: '{channel.name}'...")
                    vc = await channel.connect()

                    input_device, detected_channels = discover_hardware_profile()
                    ffmpeg_options = {
                        'before_options': f'-nostdin -f alsa -ac {detected_channels} -ar 44100',
                        'options': ''
                    }
                    raw_source = discord.FFmpegPCMAudio(input_device, **ffmpeg_options)
                    vc.play(discord.PCMVolumeTransformer(raw_source, volume=1.0))
                    print("🔄 [Recovery] State resume completed successfully.")
                else:
                    print("❌ [Recovery] Channel target missing or invalid on current guild indexing context.")
                    clear_stream_state()
            except Exception as e:
                print(f"❌ [Recovery] Crash engine loop failed to parse or reconstruct state parameters: {e}")
                clear_stream_state()
        else:
            print("🔄 [Recovery] Clean boot pipeline detected. No data traces saved to disk.")
    else:
        print("🔄 [Recovery] Stay disconnected policy enforced. Skipping historical trace parsing loops.")

if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN variable is missing. Check your .env file.")
    else:
        bot.run(TOKEN)

