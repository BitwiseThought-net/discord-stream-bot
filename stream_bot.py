import os
import discord
from discord.ext import commands

# Fetches configurations automatically injected by Docker Compose
TOKEN = os.getenv('DISCORD_TOKEN')

# Pulls the hardware device name from environment, defaulting to 'hw:1,0' if not provided
INPUT_DEVICE = os.getenv('INPUT_DEVICE', 'hw:1,0')

# Set up required intents. Message Content must be enabled in the Discord Developer Portal.
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
    # Ensure the user invoking the command is actually in a voice channel
    if not ctx.author.voice:
        await ctx.send("You must be in a voice channel to start streaming!")
        return

    channel = ctx.author.voice.channel

    # Connect to the voice channel
    vc = await channel.connect()

    # FFmpeg options configured to capture live ALSA audio input from the host
    # -f alsa: Uses the Advanced Linux Sound Architecture format
    # -ac 2: Captures 2 audio channels (Stereo)
    # -ar 44100: Sets the audio sample rate to 44.1kHz
    # -i: Specifies the hardware input path
    ffmpeg_options = {
        'options': f'-f alsa -ac 2 -ar 44100 -i {INPUT_DEVICE}',
        'before_options': '-nostdin'
    }

    await ctx.send(f"Connected! Now streaming live hardware audio from input: {INPUT_DEVICE}")

    # Begin streaming the hardware audio capture directly into the voice connection
    vc.play(discord.FFmpegPCMAudio(None, **ffmpeg_options))

@bot.command()
async def stop(ctx):
    """Stops streaming and leaves the voice channel."""
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Stopped streaming and disconnected.")
    else:
        await ctx.send("I am not currently connected to a voice channel.")

# Initialize the bot using the environment token
if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN variable is missing. Check your .env file.")
    else:
        bot.run(TOKEN)
