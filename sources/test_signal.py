"""
Diagnostic Test Signal source.

Self-contained audio source plugin. See the "SOURCE PLUGIN CONTRACT" docstring
in bot.py for the full interface every file in sources/ is expected to
implement. Summary:

    SOURCE_TYPE        -- unique string identifying this source
    DESCRIPTION         -- human readable default description
    discover()          -- -> list[dict], each dict describing one concrete,
                            currently-available hardware/software instance
    build_command(...)  -- -> str, shell pipeline that writes raw
                            s16le/48kHz/stereo PCM into the shared FIFO
    probe_signal(...)   -- OPTIONAL, live "is there real signal" check

This particular source never touches real hardware -- it synthesizes a
440Hz calibration tone with ffmpeg's `lavfi` sine generator. It's meant as
an always-available baseline so the bot has *something* to stream even
when no other source file in sources/ is usable (missing hardware, missing
dependencies, etc.). Because it's just a normal source plugin, dropping
this file out of sources/ simply removes it like any other source --
bot.py falls back to its own tiny built-in emergency tone in that case.
"""

SOURCE_TYPE = "test_signal"
DESCRIPTION = "🛠️ Diagnostic Test Signal (Analog Calibration Tone)"

# Debian/apt package names this file's build_command() shells out to.
# Purely declarative -- see the SOURCE PLUGIN CONTRACT note in bot.py.
# bot.py is the only thing that ever installs these, and only if they're
# already on the admin-maintained allowlist.
REQUIRED_PACKAGES = ["ffmpeg"]


def discover():
    """Always available -- no hardware dependency at all."""
    return [
        {
            "device": "virtual",
            "channels": "2",
            "description": DESCRIPTION,
        }
    ]


def build_command(instance: dict, frequency: str, fifo_pipe: str) -> str:
    """Synthesizes a fixed 440Hz sine tone directly into the FIFO."""
    return (
        'ffmpeg -y -f lavfi -i "sine=frequency=440:sample_rate=48000" '
        f'-f s16le -ar 48k -ac 2 pipe:1 >> {fifo_pipe}'
    )


# No probe_signal() -- there's nothing to "probe", the tone is either
# playing or it isn't, so this source is left out of live-signal scans.
