"""
Shared "scan_range" action: sweep a frequency range with an RTL-SDR dongle
and report which sub-ranges have a signal above the noise floor.

This module only knows about RTL-SDR mechanics (rtl_sdr, IQ samples, FFTs)
-- it has no opinion on *what* band is worth scanning, what counts as
"nearby" for merging purposes, or what argument syntax a user types into
Discord. Those are policy decisions that belong to whichever source module
calls in here (e.g. sources/sdr_radio.py supplies FM broadcast-band
defaults and 200kHz channel spacing; a different RTL-SDR-backed source
could supply entirely different numbers for its own band).

See actions/__init__.py for the general contract this package follows.
"""

import shutil
import subprocess

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

# Sensible defaults for the mechanics of a sweep step itself (FFT
# resolution, how far above the noise floor counts as "a signal", how much
# consecutive steps overlap). These aren't band-specific, so callers can
# rely on the defaults and only override what they actually care about.
DEFAULT_FFT_SIZE = 4096
DEFAULT_STEP_OVERLAP = 0.9             # step by 90% of sample rate so band edges aren't missed
DEFAULT_PEAK_THRESHOLD_DB = 12.0       # dB above a step's own noise floor to count as a channel
DEFAULT_MAX_STEPS = 120                # sanity cap so a mistyped range can't trigger a runaway scan
DEFAULT_SAMPLE_RATE_HZ = 2_400_000     # rtl-sdr's standard stable sample rate
DEFAULT_CAPTURE_SECONDS = 0.25         # raw IQ capture window per sweep step
DEFAULT_MIN_CHANNEL_SPACING_HZ = 200_000  # merge-distance if a caller doesn't supply its own


def dependencies_available() -> tuple:
    """Returns (True, "") if this action's tooling is present on this host,
    else (False, <human-readable reason>). Callers can check this up front
    for a fast/clear failure, or just let scan_for_clear_channels_sync()
    raise the same message once it actually needs the missing tool."""
    if shutil.which("rtl_sdr") is None:
        return False, "`rtl_sdr` not found on PATH -- install the rtl-sdr tools package in the container image to use frequency scanning."
    if not NUMPY_AVAILABLE:
        return False, "`numpy` is not installed -- it's required to run the FFT over captured samples for frequency scanning."
    return True, ""


def capture_iq_samples(center_hz: float, sample_rate: int, duration_s: float):
    """Captures raw 8-bit IQ samples from the SDR dongle via rtl_sdr and returns them
    as a complex numpy array centered on baseband. This needs exclusive access to the
    dongle, callers must make sure no hardware pipeline (rtl_fm, etc.) currently holds
    it open, or rtl_sdr will fail to claim the USB interface."""
    num_iq_pairs = int(sample_rate * duration_s)
    cmd = [
        "rtl_sdr", "-f", str(int(center_hz)), "-s", str(sample_rate),
        "-n", str(num_iq_pairs * 2), "-"
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=duration_s + 5.0)
    if result.returncode != 0 or len(result.stdout) < 2:
        stderr_text = result.stderr.decode(errors="ignore").strip()
        last_line = stderr_text.splitlines()[-1] if stderr_text else "rtl_sdr produced no samples"
        raise RuntimeError(last_line)

    raw = np.frombuffer(result.stdout, dtype=np.uint8).astype(np.float64)
    raw = raw[: len(raw) - (len(raw) % 2)]
    iq = (raw - 127.5) / 127.5
    i_samples = iq[0::2]
    q_samples = iq[1::2]
    n = min(len(i_samples), len(q_samples))
    return i_samples[:n] + 1j * q_samples[:n]


def find_peaks_in_step(center_hz: float, sample_rate: int, complex_samples,
                        fft_size: int = DEFAULT_FFT_SIZE,
                        peak_threshold_db: float = DEFAULT_PEAK_THRESHOLD_DB):
    """Runs a windowed FFT over one capture window and returns candidate
    (freq_hz, power_db) peaks that clear the step's own noise floor."""
    if len(complex_samples) < fft_size:
        return []

    window = np.hanning(fft_size)
    windowed = complex_samples[:fft_size] * window
    spectrum = np.fft.fftshift(np.fft.fft(windowed, n=fft_size))
    power_db = 20.0 * np.log10(np.abs(spectrum) + 1e-12)
    freq_offsets = np.fft.fftshift(np.fft.fftfreq(fft_size, d=1.0 / sample_rate))
    freq_bins = freq_offsets + center_hz

    noise_floor_db = float(np.median(power_db))
    threshold = noise_floor_db + peak_threshold_db

    # The exact center frequency carries a DC spike that's a dongle artifact, not
    # a real signal, and would otherwise register as a "channel" on every single
    # step regardless of what's actually tuned in. Exclude a small guard band
    # around it.
    dc_bin = fft_size // 2
    dc_guard_bins = 3

    above = np.where(power_db > threshold)[0]
    if len(above) == 0:
        return []

    # Group contiguous bin runs into single peaks (a real signal typically lights
    # up several adjacent bins), keep only the strongest bin per run.
    peaks = []
    run_start = above[0]
    prev = above[0]
    for b in list(above[1:]) + [None]:
        if b is not None and b == prev + 1:
            prev = b
            continue
        in_dc_guard = (dc_bin - dc_guard_bins <= run_start) and (prev <= dc_bin + dc_guard_bins)
        if not in_dc_guard:
            run = range(run_start, prev + 1)
            best_idx = max(run, key=lambda i: power_db[i])
            peaks.append((float(freq_bins[best_idx]), float(power_db[best_idx])))
        if b is not None:
            run_start = b
            prev = b
    return peaks


def merge_nearby_channels(candidates, min_channel_spacing_hz: float = DEFAULT_MIN_CHANNEL_SPACING_HZ):
    """Collapses candidate peaks within min_channel_spacing_hz of each other
    (e.g. the same station seen from two overlapping sweep steps) into a single
    entry, keeping whichever reading was strongest."""
    if not candidates:
        return []
    candidates = sorted(candidates, key=lambda c: c[0])
    merged = [candidates[0]]
    for freq_hz, power_db in candidates[1:]:
        last_freq, last_power = merged[-1]
        if freq_hz - last_freq <= min_channel_spacing_hz:
            if power_db > last_power:
                merged[-1] = (freq_hz, power_db)
        else:
            merged.append((freq_hz, power_db))
    return merged


def scan_for_clear_channels_sync(start_hz: float, end_hz: float, *,
                                  sample_rate: int = DEFAULT_SAMPLE_RATE_HZ,
                                  capture_seconds: float = DEFAULT_CAPTURE_SECONDS,
                                  fft_size: int = DEFAULT_FFT_SIZE,
                                  step_overlap: float = DEFAULT_STEP_OVERLAP,
                                  peak_threshold_db: float = DEFAULT_PEAK_THRESHOLD_DB,
                                  min_channel_spacing_hz: float = DEFAULT_MIN_CHANNEL_SPACING_HZ,
                                  max_steps: int = DEFAULT_MAX_STEPS):
    """Sweeps [start_hz, end_hz) in sample-rate-sized steps, running an FFT over each
    capture window to build a power spectrum, and returns a sorted list of
    {"frequency": "94.9M", "power_db": float} channel catalog entries.

    All the keyword arguments have generic RTL-SDR defaults; callers with
    band-specific policy (channel spacing, etc.) should override them --
    see the module docstring for an example.

    This is the blocking implementation (rtl_sdr subprocess calls plus numpy FFT
    work) -- callers must run it off the bot's event loop, e.g. via
    asyncio.to_thread(), or it will stall every other Discord interaction for the
    duration of the sweep.
    """
    ok, reason = dependencies_available()
    if not ok:
        raise RuntimeError(reason)

    step_hz = sample_rate * step_overlap
    span_hz = end_hz - start_hz
    num_steps = min(max_steps, max(1, int(span_hz / step_hz) + 1))

    all_candidates = []
    for step in range(num_steps):
        center_hz = start_hz + (sample_rate / 2.0) + (step * step_hz)
        if center_hz - (sample_rate / 2.0) > end_hz:
            break
        try:
            samples = capture_iq_samples(center_hz, sample_rate, capture_seconds)
        except Exception as e:
            print(f"⚠️ [Scan] Skipping step at {center_hz / 1e6:.3f}MHz, capture failed: {e}")
            continue
        all_candidates.extend(find_peaks_in_step(
            center_hz, sample_rate, samples,
            fft_size=fft_size, peak_threshold_db=peak_threshold_db
        ))

    merged = merge_nearby_channels(all_candidates, min_channel_spacing_hz)
    catalog = []
    for freq_hz, power_db in merged:
        if freq_hz < start_hz or freq_hz > end_hz:
            continue
        freq_mhz = round(freq_hz / 1_000_000, 1)
        catalog.append({"frequency": f"{freq_mhz}M", "power_db": round(power_db, 1)})

    catalog.sort(key=lambda c: float(c["frequency"].rstrip("M")))
    return catalog
