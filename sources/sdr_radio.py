"""
RTL-SDR Radio (FM & HAM) source.

Self-contained audio source plugin -- see the "SOURCE PLUGIN CONTRACT"
docstring in bot.py for the interface this file implements (SOURCE_TYPE,
DESCRIPTION, discover(), build_command(), and the optional probe_signal()).
Also advertises the optional "scan_range" action (SUPPORTED_ACTIONS +
scan_range()) via actions/scan_range.py.

Detects an RTL2832U-based USB dongle via `lsusb` and demodulates wideband
FM at the currently tuned frequency using `rtl_fm`, piping straight into
ffmpeg for resampling into the shared FIFO. All rtl_fm/USB-chipset-specific
logic lives in this one file -- if `lsusb`/`rtl_fm` are missing or no
dongle is plugged in, only this source fails to discover anything.
"""

import os
import subprocess

from actions import scan_range as scan_range_action

SOURCE_TYPE = "sdr_radio"
DESCRIPTION = "Radio (FM & HAM)"

# Debian/apt package names this file shells out to: usbutils for lsusb,
# rtl-sdr for rtl_fm, ffmpeg for resampling. Purely declarative -- see the
# SOURCE PLUGIN CONTRACT note in bot.py. bot.py is the only thing that ever
# installs these, and only if they're already on the admin-maintained
# allowlist.
REQUIRED_PACKAGES = ["usbutils", "rtl-sdr", "ffmpeg"]

# Optional capabilities this source exposes beyond the base discover()/
# build_command() contract, e.g. bot.py's `/radio channel scan` only offers
# itself for the currently active source if "scan_range" is listed here --
# see the "SOURCE PLUGIN CONTRACT" note in bot.py.
SUPPORTED_ACTIONS = ["scan_range"]

# FM-broadcast-specific policy for the scan_range action below -- these are
# opinions about *this band*, not RTL-SDR hardware facts, so they live here
# rather than in actions/scan_range.py (which stays band-agnostic). bot.py
# reads SCAN_DEFAULT_START_MHZ/SCAN_DEFAULT_END_MHZ/SCAN_MAX_SPAN_MHZ off
# the active source module when parsing a bare "scan" argument with no
# explicit range.
SCAN_DEFAULT_START_MHZ = 88.0
SCAN_DEFAULT_END_MHZ = 108.0
SCAN_MAX_SPAN_MHZ = 60.0                # sanity cap so a mistyped range can't trigger a runaway scan
SCAN_MIN_CHANNEL_SPACING_HZ = 200_000   # standard FM broadcast channel spacing

# RTL2832U-based dongles report this vendor:product USB ID.
USB_VENDOR_ID = "0bda"
USB_PRODUCT_ID = "2838"
USB_CHIPSET_ID = f"{USB_VENDOR_ID}:{USB_PRODUCT_ID}"
SYSFS_USB_DEVICES = "/sys/bus/usb/devices"


def _rtl_sdr_dongle_present() -> bool:
    """Checks sysfs directly for a device reporting the RTL2832U
    vendor:product ID. Deliberately avoids relying on `lsusb`'s
    human-readable output -- that text depends on usbutils' own USB-ID
    name database, which can fail to load (seen in the wild as an
    "unable to initialize usb spec" warning) and silently produce a
    truncated device list even though the kernel/sysfs still sees the
    device fine. sysfs is just raw kernel-reported integers, so it isn't
    affected by that failure mode.
    """
    try:
        for entry in os.listdir(SYSFS_USB_DEVICES):
            try:
                with open(os.path.join(SYSFS_USB_DEVICES, entry, "idVendor")) as vf:
                    vendor = vf.read().strip().lower()
                with open(os.path.join(SYSFS_USB_DEVICES, entry, "idProduct")) as pf:
                    product = pf.read().strip().lower()
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
                continue
            if vendor == USB_VENDOR_ID and product == USB_PRODUCT_ID:
                return True
    except Exception:
        pass

    # Fallback: lsusb, in case sysfs isn't available on this host at all
    # (e.g. non-Linux). Kept as a secondary check only -- see docstring
    # above for why it isn't trusted as the primary source of truth.
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
        f"rtl_fm -f {frequency} -M wbo -s 170k -r 48k -g 40 | "
        f"ffmpeg -y -f s16le -ar 48k -ac 1 -i pipe:0 -f s16le -ar 48k -ac 2 pipe:1 >> {fifo_pipe}"
    )


# No probe_signal() -- there's exactly one dongle-backed instance here (not
# several indistinguishable ones like the USB mic case), so there's nothing
# to disambiguate with a live-signal probe.


def scan_range(start_hz: float, end_hz: float) -> list:
    """The "scan_range" action advertised in SUPPORTED_ACTIONS above.
    Sweeps [start_hz, end_hz) for clear FM broadcast channels, delegating
    the actual RTL-SDR capture/FFT mechanics to the shared action and only
    supplying this band's own channel-spacing policy."""
    return scan_range_action.scan_for_clear_channels_sync(
        start_hz, end_hz,
        min_channel_spacing_hz=SCAN_MIN_CHANNEL_SPACING_HZ,
    )
