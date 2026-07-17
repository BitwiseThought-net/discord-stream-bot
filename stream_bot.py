import os
import re
import asyncio
import discord
from discord import app_commands
from datetime import datetime, timedelta

TOKEN = os.getenv('DISCORD_TOKEN')

# Fetch the custom command name from environment, defaulting to 'stream' if missing
COMMAND_NAME = os.getenv('COMMAND_BASE', 'stream').lower().strip()

intents = discord.Intents.default()

class StreamBot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        # Tracking active sleep timer tasks so we can cancel them if stopped manually
        self.sleep_tasks = {}

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

            await interaction.followup.send(f"Connected! Now streaming live hardware audio ({detected_channels}-channel mode) from input: {input_device}")
            
            raw_source = discord.FFmpegPCMAudio(input_device, **ffmpeg_options)
            vc.play(discord.PCMVolumeTransformer(raw_source, volume=1.0))

        @stream_group.command(name="stop", description="Stops the active feed and leaves the voice channel.")
        async def stop_stream(interaction: discord.Interaction):
            guild_id = interaction.guild.id
            # Cancel any pending sleep timer for this server if it exists
            if guild_id in self.sleep_tasks:
                self.sleep_tasks[guild_id].cancel()
                del self.sleep_tasks[guild_id]

            if interaction.guild.voice_client:
                await interaction.guild.voice_client.disconnect()
                await interaction.response.send_message("Stopped streaming and disconnected.")
            else:
                await interaction.response.send_message("I am not currently connected to a voice channel.", ephemeral=True)

        @stream_group.command(name="volume", description="Adjusts the volume of the audio feed on the fly.")
        @app_commands.describe(percentage="The target volume percentage from 0 to 100.")
        async def adjust_volume(interaction: discord.Interaction, percentage: app_commands.Range[int, 0, 100]):
            vc = interaction.guild.voice_client
            
            if not vc or not vc.source:
                await interaction.response.send_message("The bot is not currently streaming!", ephemeral=True)
                return

            if isinstance(vc.source, discord.PCMVolumeTransformer):
                vc.source.volume = percentage / 100.0
                await interaction.response.send_message(f"🎵 Volume set to **{percentage}%**.")
            else:
                await interaction.response.send_message("Volume control wrapper not ready on this stream layout.", ephemeral=True)

        @stream_group.command(name="sleep", description="Sets a sleep timer to automatically turn off the stream.")
        @app_commands.describe(duration="Time string like '3:34pm', '45s', '15m', or '1.5h'.")
        async def sleep_timer(interaction: discord.Interaction, duration: str):
            guild_id = interaction.guild.id
            vc = interaction.guild.voice_client

            if not vc:
                await interaction.response.send_message("The bot must be connected to a voice channel to set a sleep timer!", ephemeral=True)
                return

            # Clean and parse the input target string
            raw_input = duration.lower().strip()
            delay_seconds = None

            # 1. Match Relative Duration Formats (e.g., '3s', '15minutes', '1.5h')
            # Extracts floating point numerical components and matches character flags
            relative_match = re.match(r"^([0-9.]+)\s*([a-z]+)$", raw_input)
            
            if relative_match:
                value = float(relative_match.group(1))
                unit = relative_match.group(2)
                
                if unit in ['s', 'sec', 'second', 'seconds']:
                    delay_seconds = value
                elif unit in ['m', 'min', 'minute', 'minutes']:
                    delay_seconds = value * 60
                elif unit in ['h', 'hr', 'hour', 'hours']:
                    delay_seconds = value * 3600
                else:
                    await interaction.response.send_message("⚠️ Unrecognized duration unit. Please use seconds, minutes, or hours.", ephemeral=True)
                    return

            # 2. Match Absolute Dynamic Time Formats (e.g., '3:34pm', '15:20')
            else:
                time_formats = ["%I:%M%p", "%I:%M %p", "%H:%M"]
                now = datetime.now()
                
                for fmt in time_formats:
                    try:
                        parsed_time = datetime.strptime(raw_input.upper(), fmt)
                        # Construct a timestamp using today's calendar date
                        target_dt = now.replace(hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0)
                        
                        # If the absolute time targeted has already passed today, assume it rolls over to tomorrow
                        if target_dt < now:
                            target_dt += timedelta(days=1)
                            
                        delay_seconds = (target_dt - now).total_seconds()
                        break
                    except ValueError:
                        continue

            if delay_seconds is None or delay_seconds <= 0:
                await interaction.response.send_message("⚠️ Invalid time string format. Try inputs like `30m`, `1.5h`, or `11:45pm`.", ephemeral=True)
                return

            # Cancel any existing sleep timer task running on this specific server guild
            if guild_id in self.sleep_tasks:
                self.sleep_tasks[guild_id].cancel()

            # Define the background sleep worker function thread safely
            async def sleep_worker(seconds, voice_client):
                await asyncio.sleep(seconds)
                if voice_client and voice_client.is_connected():
                    await voice_client.disconnect()
                    print(f"💤 Sleep timer reached. Automatically disconnected from server guild: {guild_id}")
                if guild_id in self.sleep_tasks:
                    del self.sleep_tasks[guild_id]

            # Register and immediately launch the scheduled task thread inside the loop
            task = asyncio.create_task(sleep_worker(delay_seconds, vc))
            self.sleep_tasks[guild_id] = task

            # Calculate user-facing clean presentation strings for confirmation message
            total_minutes = int(delay_seconds // 60)
            remaining_seconds = int(delay_seconds % 60)
            
            if total_minutes > 0:
                time_display = f"{total_minutes}m {remaining_seconds}s"
            else:
                time_display = f"{remaining_seconds} seconds"

            await interaction.response.send_message(f"💤 Sleep timer locked in! The stream will turn off in **{time_display}**.")

        self.tree.add_command(stream_group)
        print("Syncing slash commands globally...")
        await self.tree.sync()

bot = StreamBot(intents=intents)

def discover_hardware_profile():
    """
    Scans the read-only /mnt/asound directory to dynamically locate the active 
    USB audio capture card and detect its native audio mode (mono vs stereo).
    Returns a tuple of (device_string, channel_count_string).
    """
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

@bot.event
async def on_ready():
    print(f"Streaming bot successfully logged in as {bot.user.name}")
    dev, ch = discover_hardware_profile()
    print(f"Startup scan check -> Device: {dev} | Channels: {ch}")

if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN variable is missing. Check your .env file.")
    else:
        bot.run(TOKEN)
