"""
USB Microphone (ALSA) source.

Self-contained audio source plugin -- see the "SOURCE PLUGIN CONTRACT"
docstring in bot.py for the interface this file implements (SOURCE_TYPE,
DESCRIPTION, discover(), build_command(), and the optional probe_signal()).

Discovers USB sound cards exposed under /proc/asound, one instance per
card, and figures out mono vs stereo capability by reading ALSA's own
stream info files. Captures with ffmpeg's `alsa` input device.

All ALSA/arecord-specific logic lives in this one file -- if the host has
no /proc/asound or no `arecord`/`ffmpeg` binaries, only this source fails
to discover/probe anything; it has no effect on any other source or on
bot.py itself.
"""

import os
import subprocess
import shutil
import array

SOURCE_TYPE = "alsa"
DESCRIPTION = "USB Microphone ({device})"
MONO_DESCRIPTION = "USB Mono Microphone ({device})"

ASOUND_DIR = "/proc/asound"


def discover():
    """Scans /proc/asound for sound cards and reports one instance per card."""
    instances = []
    if not os.path.exists(ASOUND_DIR):
        return instances

    try:
        cards = [
            d for d in os.listdir(ASOUND_DIR)
            if d.startswith("card") and os.path.isdir(os.path.join(ASOUND_DIR, d))
        ]
    except Exception:
        return instances

    for card in sorted(cards):
        card_index = card.replace("card", "")
        device_string = f"plughw:{card_index},0"
        channels = "2"
        label_template = DESCRIPTION

        stream_info = os.path.join(ASOUND_DIR, card, "usbstream")
        if not os.path.exists(stream_info):
            stream_info = os.path.join(ASOUND_DIR, card, "stream0")
        if os.path.exists(stream_info):
            try:
                with open(stream_info, "r") as f:
                    if "1 channel" in f.read().lower():
                        channels = "1"
                        label_template = MONO_DESCRIPTION
            except Exception:
                pass

        instances.append({
            "device": device_string,
            "channels": channels,
            "description": label_template.format(device=device_string),
        })

    return instances


def build_command(instance: dict, frequency: str, fifo_pipe: str) -> str:
    """Captures raw audio straight off the ALSA device with ffmpeg."""
    device = instance.get("device", "")
    channels = instance.get("channels", "2")
    return (
        f"ffmpeg -y -f alsa -ac {channels} -i {device} "
        f"-f s16le -ar 48k -ac 2 pipe:1 >> {fifo_pipe}"
    )


def probe_signal(instance: dict, duration: float = 0.3, rms_threshold: float = 50.0):
    """Records a short raw snippet directly from the ALSA capture device and
    checks for non-silence via RMS amplitude. Used to auto-detect which of
    several identical USB microphone entries is actually receiving live
    audio, since card index alone can't distinguish between otherwise
    identical hardware.

    Returns a (status, detail) tuple instead of a bare bool:
      status == "signal" : audio captured, RMS above threshold
      status == "silent" : device opened and captured fine, RMS below threshold
      status == "error"  : could not get a real reading at all -- device
                            busy, arecord missing, unsupported format/rate,
                            permission denied on /dev/snd, timeout, etc.
                            This is deliberately distinct from "silent" so
                            callers can tell "nothing plugged in" apart from
                            "the probe itself couldn't run".

    NOTE: if the bot is *currently* streaming from this exact device, arecord
    will typically fail to open it (device busy) -- that surfaces as an
    "error" with a busy/in-use detail rather than a false "silent" reading.
    """
    device = instance.get("device", "")
    if not device or not device.startswith("plughw"):
        return ("error", "not a probeable ALSA device")

    if shutil.which("arecord") is None:
        return ("error", "arecord not found on PATH -- install alsa-utils in the container image")

    try:
        result = subprocess.run(
            ["arecord", "-D", device, "-f", "S16_LE", "-r", "48000",
             "-c", "1", "-d", str(duration), "-t", "raw"],
            capture_output=True, timeout=duration + 2.0
        )
    except subprocess.TimeoutExpired:
        return ("error", "arecord timed out opening the device")
    except Exception as e:
        return ("error", f"failed to launch arecord: {e}")

    if result.returncode != 0:
        stderr_text = result.stderr.decode(errors="ignore").strip()
        last_line = stderr_text.splitlines()[-1] if stderr_text else f"exit code {result.returncode}"
        return ("error", last_line)

    raw = result.stdout
    if len(raw) < 2:
        return ("error", "arecord exited cleanly but returned no audio bytes")

    samples = array.array('h', raw[: len(raw) - (len(raw) % 2)])
    if not samples:
        return ("error", "empty sample buffer after capture")

    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
    if rms > rms_threshold:
        return ("signal", f"rms={rms:.1f}")
    return ("silent", f"rms={rms:.1f}")
