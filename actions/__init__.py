"""
Shared "action" implementations that a source in sources/ can opt into.

Unlike sources/, this package is NOT dynamically loaded/hot-swapped -- it's
baked into the image alongside bot.py and imported normally
(`from actions import scan_range`). It exists so that mechanics generic to
a *class* of hardware (e.g. "any RTL-SDR-backed source can sweep a
frequency range and FFT the result") aren't duplicated across every source
file that wants them, without forcing that logic into bot.py itself --
bot.py stays hardware-agnostic and only ever calls whatever the *active
source* exposes.

Each action module here should stay free of any Discord-specific code
(no `discord.Interaction`, no message formatting) -- that glue belongs in
bot.py's command handlers. An action module's job is purely: given
hardware-facing parameters, return plain data.

A source declares which of these it supports via a `SUPPORTED_ACTIONS`
list (same shape as `REQUIRED_PACKAGES`) and exposes a same-named
function that wraps the shared implementation with its own policy/
defaults, e.g.:

    # sources/sdr_radio.py
    from actions import scan_range as scan_range_action

    SUPPORTED_ACTIONS = ["scan_range"]

    def scan_range(start_hz, end_hz):
        return scan_range_action.scan_for_clear_channels_sync(
            start_hz, end_hz,
            sample_rate=2_400_000,
            min_channel_spacing_hz=200_000,  # FM channel spacing
        )
"""
