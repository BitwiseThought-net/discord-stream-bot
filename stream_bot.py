import os
import discord
from discord import app_commands

# Fetches configurations automatically injected by Docker Compose
TOKEN = os.getenv('DISCORD_TOKEN')

# Pulls the hardware device name from environment, defaulting to 'hw:1,0' if not provided
INPUT_DEVICE = os.getenv('INPUT_DEVICE', 'hw:1,0') 

# Default intents are sufficient now. Message Content Intent is NO LONGER required.
intents = discord.Intents.default()

class StreamBot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        # The CommandTree holds and manages all slash commands
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Syncs slash commands globally with Discord's servers on startup
        print("Syncing slash commands globally...")
        await self.tree.sync()

bot = StreamBot(intents=intents)

@bot.event
async def on_ready():
    print(f"Streaming bot successfully logged in as {bot.user.name}")
    print(f"Configured hardware input device: {INPUT_DEVICE}")

@bot.tree.command(name="start", description="Joins your voice channel and starts streaming live hardware audio.")
async def start(interaction: discord.Interaction):
    """Slash command to start the live stream."""
    # Ensure the user invoking the command is actually in a voice channel
    if not interaction.user.voice:
        await interaction.response.send_message("You must be in a voice channel to start streaming!", ephemeral=True)
        return

    channel = interaction.user.voice.channel

    # Acknowledge the interaction immediately to prevent timing out (3-second limit)
    await interaction.response.send_message(f"Connecting to {channel.name}... Please wait.")

    # Connect to the voice channel
    vc = await channel.connect()

    # FFmpeg options configured to capture live ALSA audio input from the host
    ffmpeg_options = {
        'options': f'-f alsa -ac 2 -ar 44100 -i {INPUT_DEVICE}',
        'before_options': '-nostdin'
    }

    # Follow up with a public confirmation message once streaming begins
    await interaction.followup.send(f"Connected! Now streaming live hardware audio from input: {INPUT_DEVICE}")

    # Begin streaming the hardware audio capture directly into the voice connection
    vc.play(discord.FFmpegPCMAudio(None, **ffmpeg_options))

@bot.tree.command(name="stop", description="Stops streaming and leaves the voice channel.")
async def stop(interaction: discord.Interaction):
    """Slash command to stop the live stream."""
    # Check if the bot is in a voice channel within this specific server guild
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("Stopped streaming and disconnected.")
    else:
        await interaction.response.send_message("I am not currently connected to a voice channel.", ephemeral=True)

# Initialize the bot using the environment token
if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN variable is missing. Check your .env file.")
    else:
        bot.run(TOKEN)
