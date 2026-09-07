"""
RTL-SDR Aircraft (AM) source.

Self-contained audio source plugin -- see the "SOURCE PLUGIN CONTRACT"
docstring in bot.py for the interface this file implements (SOURCE_TYPE,
DESCRIPTION, discover(), build_command(), and the optional probe_signal()).

Detects an RTL2832U-based USB dongle via `lsusb` and demodulates AM (as
used by aircraft-band voice traffic) at the currently tuned frequency with
`rtl_fm`, resampling through `sox` and `ffmpeg` into the shared FIFO. All
rtl_fm/USB-chipset-specific logic lives in this one file -- if
`lsusb`/`rtl_fm`/`sox` are missing or no dongle is plugged in, only this
source fails to discover anything.
"""

import subprocess

SOURCE_TYPE = "sdr_aircraft"
DESCRIPTION = "Aircraft (ADS-B)"

# Debian/apt package names this file shells out to: usbutils for lsusb,
# rtl-sdr for rtl_fm, sox/libsox-fmt-all for resampling, ffmpeg for the
# final stage. Purely declarative -- see the SOURCE PLUGIN CONTRACT note in
# bot.py. bot.py is the only thing that ever installs these, and only if
# they're already on the admin-maintained allowlist.
REQUIRED_PACKAGES = ["usbutils", "rtl-sdr", "sox", "libsox-fmt-all", "ffmpeg"]

# RTL2832U-based dongles report this vendor:product USB ID (or the "rtl2832"
# string somewhere in lsusb's description of the device).
USB_CHIPSET_ID = "0bda:2838"


def _rtl_sdr_dongle_present() -> bool:
    try:
        result = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5.0)
        usb_output = result.stdout.lower()
    except Exception:
        return False
    return USB_CHIPSET_ID in usb_output or "rtl2832" in usb_output


def discover():
    if not _rtl_sdr_dongle_present():
        return []
    return [
        {
            "device": "rtlsdr",
            "channels": "1",
            "description": DESCRIPTION,
        }
    ]


def build_command(instance: dict, frequency: str, fifo_pipe: str) -> str:
    return (
        f"rtl_fm -f {frequency} -M am -s 25k -r 24k -g 48 | "
        "sox -t raw -r 24k -e signed-integer -b 16 -c 1 - -t raw -r 48k - | "
        f"ffmpeg -y -f s16le -ar 48k -ac 1 -i pipe:0 -f s16le -ar 48k -ac 2 pipe:1 >> {fifo_pipe}"
    )


# No probe_signal() -- there's exactly one dongle-backed instance here (not
# several indistinguishable ones like the USB mic case), so there's nothing
# to disambiguate with a live-signal probe.
