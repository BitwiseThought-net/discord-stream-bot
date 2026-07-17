import os
import discord
from discord import app_commands

TOKEN = os.getenv('DISCORD_TOKEN')
INPUT_DEVICE = os.getenv('INPUT_DEVICE', 'hw:1,0') 

# Pull the descriptive text mode and normalize it
MODE_PARAM = os.getenv('INPUT_MODE', 'stereo').lower().strip()

# Map the text mode parameter down to actual raw hardware channels
if MODE_PARAM in ['mono', 'sterio', '1']:
    INPUT_CHANNELS = '1'
else:
    INPUT_CHANNELS = '2'

intents = discord.Intents.default()

class StreamBot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        print("Syncing slash commands globally...")
        await self.tree.sync()

bot = StreamBot(intents=intents)

@bot.event
async def on_ready():
    print(f"Streaming bot successfully logged in as {bot.user.name}")
    print(f"Configured hardware input device: {INPUT_DEVICE}")
    print(f"Hardware input mapping mode: {MODE_PARAM} ({INPUT_CHANNELS}-channel mode)")

@bot.tree.command(name="start", description="Joins your voice channel and starts streaming live hardware audio.")
async def start(interaction: discord.Interaction):
    """Slash command to start the live stream using explicit environment parameters."""
    if not interaction.user.voice:
        await interaction.response.send_message("You must be in a voice channel to start streaming!", ephemeral=True)
        return

    channel = interaction.user.voice.channel
    await interaction.response.send_message(f"Connecting to {channel.name}... Please wait.")
    vc = await channel.connect()

    # Clear, explicit argument parameters matching your environment variables
    ffmpeg_options = {
        'before_options': f'-nostdin -f alsa -ac {INPUT_CHANNELS} -ar 44100',
        'options': ''
    }

    await interaction.followup.send(f"Connected! Now streaming live hardware audio from input: {INPUT_DEVICE}")
    vc.play(discord.FFmpegPCMAudio(INPUT_DEVICE, **ffmpeg_options))

@bot.tree.command(name="stop", description="Stops streaming and leaves the voice channel.")
async def stop(interaction: discord.Interaction):
    """Slash command to stop the live stream."""
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("Stopped streaming and disconnected.")
    else:
        await interaction.response.send_message("I am not currently connected to a voice channel.", ephemeral=True)

if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN variable is missing. Check your .env file.")
    else:
        bot.run(TOKEN)
