import os
import re
import discord
from discord import app_commands

TOKEN = os.getenv('DISCORD_TOKEN')

# Fetch the custom command name from environment, defaulting to 'stream' if missing
COMMAND_NAME = os.getenv('COMMAND_BASE', 'stream').lower().strip()

intents = discord.Intents.default()

class StreamBot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # 1. Create a dynamic command group named after our .env parameter
        print(f"Creating dynamic slash command group: /{COMMAND_NAME}")
        stream_group = app_commands.Group(
            name=COMMAND_NAME, 
            description=f"Commands to manage the live audio {COMMAND_NAME}."
        )

        # 2. Define the sub-commands manually and link them to the group
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
            vc.play(discord.FFmpegPCMAudio(input_device, **ffmpeg_options))

        @stream_group.command(name="stop", description="Stops the active feed and leaves the voice channel.")
        async def stop_stream(interaction: discord.Interaction):
            if interaction.guild.voice_client:
                await interaction.guild.voice_client.disconnect()
                await interaction.response.send_message("Stopped streaming and disconnected.")
            else:
                await interaction.response.send_message("I am not currently connected to a voice channel.", ephemeral=True)

        # 3. Add the fully populated group to the main command tree
        self.tree.add_command(stream_group)

        # 4. Sync commands globally with Discord's servers
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

