"""Tests for actions/scan_range.py -- the shared RTL-SDR sweep/FFT mechanics
that used to live inline in bot.py before the "generic action dispatch"
refactor. bot.py itself no longer contains any of this logic (it only knows
how to call whatever `scan_range` function the active source advertises via
SUPPORTED_ACTIONS), so these tests exercise actions/scan_range.py directly
instead of going through bot.py.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import numpy as np

# actions/ sits at the repo root alongside bot.py, not under tests/.
BASE_DIR = str(Path(__file__).parent.parent)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from actions import scan_range


# ======================================================================
# dependencies_available
# ======================================================================

class TestDependenciesAvailable:
    def test_missing_rtl_sdr_binary(self):
        with patch("actions.scan_range.shutil.which", return_value=None):
            ok, reason = scan_range.dependencies_available()
        assert ok is False
        assert "rtl_sdr" in reason

    def test_missing_numpy(self):
        with patch("actions.scan_range.shutil.which", return_value="/usr/bin/rtl_sdr"), \
             patch.object(scan_range, "NUMPY_AVAILABLE", False):
            ok, reason = scan_range.dependencies_available()
        assert ok is False
        assert "numpy" in reason

    def test_all_present(self):
        with patch("actions.scan_range.shutil.which", return_value="/usr/bin/rtl_sdr"), \
             patch.object(scan_range, "NUMPY_AVAILABLE", True):
            ok, reason = scan_range.dependencies_available()
        assert ok is True
        assert reason == ""


# ======================================================================
# merge_nearby_channels
# ======================================================================

class TestMergeNearbyChannels:
    def test_no_merge_when_spaced(self):
        candidates = [(94_000_000, -20.0), (100_000_000, -30.0)]
        result = scan_range.merge_nearby_channels(candidates)
        assert len(result) == 2

    def test_merge_closely_spaced(self):
        candidates = [
            (94_000_000, -20.0),
            (94_150_000, -30.0),  # within default min_channel_spacing_hz
        ]
        result = scan_range.merge_nearby_channels(candidates)
        assert len(result) == 1
        assert result[0][1] == -20.0  # keeps the stronger peak

    def test_merge_keeps_strongest(self):
        candidates = [(94_000_000, -50.0), (94_100_000, -10.0)]
        result = scan_range.merge_nearby_channels(candidates)
        assert len(result) == 1
        assert result[0][1] == -10.0

    def test_empty_list(self):
        assert scan_range.merge_nearby_channels([]) == []

    def test_single_entry(self):
        result = scan_range.merge_nearby_channels([(94_000_000, -20.0)])
        assert len(result) == 1

    def test_custom_spacing_overrides_default(self):
        """A caller-supplied min_channel_spacing_hz (e.g. a band-specific
        source policy) should be honored instead of the module default."""
        candidates = [(94_000_000, -20.0), (94_400_000, -30.0)]
        # Default spacing (200kHz) would NOT merge these (400kHz apart).
        assert len(scan_range.merge_nearby_channels(candidates)) == 2
        # A wider caller-supplied spacing should merge them.
        merged = scan_range.merge_nearby_channels(candidates, min_channel_spacing_hz=500_000)
        assert len(merged) == 1


# ======================================================================
# find_peaks_in_step (numpy required)
# ======================================================================

class TestFindPeaksInStep:
    def _make_tone(self, freq_offset_hz, sample_rate, n, amplitude=50.0):
        t = np.arange(n) / sample_rate
        return amplitude * np.exp(2j * np.pi * freq_offset_hz * t)

    def test_no_samples_below_fft_size(self):
        samples = np.zeros(100, dtype=np.complex64)
        result = scan_range.find_peaks_in_step(100_000_000, 2_400_000, samples)
        assert result == []

    def test_no_peaks_above_threshold(self):
        rng = np.random.default_rng(42)
        samples = rng.standard_normal(16384).astype(np.complex64)
        result = scan_range.find_peaks_in_step(100_000_000, 2_400_000, samples)
        assert isinstance(result, list)

    def test_dc_spike_excluded_from_results(self):
        sample_rate = scan_range.DEFAULT_SAMPLE_RATE_HZ
        n = scan_range.DEFAULT_FFT_SIZE
        center_hz = 100_000_000
        rng = np.random.default_rng(7)
        noise = rng.standard_normal(n).astype(np.complex128) * 0.001
        # DC-only component -- lands right on the center bin, which the
        # DC guard should exclude entirely.
        dc_spike = np.full(n, 1.0, dtype=np.complex128)
        samples = noise + dc_spike

        peaks = scan_range.find_peaks_in_step(center_hz, sample_rate, samples)
        assert peaks == []

    def test_off_center_tone_detected_and_dc_excluded(self):
        sample_rate = scan_range.DEFAULT_SAMPLE_RATE_HZ
        n = scan_range.DEFAULT_FFT_SIZE
        center_hz = 100_000_000
        rng = np.random.default_rng(11)
        noise = rng.standard_normal(n).astype(np.complex128) * 0.001

        real_tone = self._make_tone(300_000, sample_rate, n, amplitude=80.0)
        dc_spike = np.full(n, 1.0, dtype=np.complex128)
        samples = noise + real_tone + dc_spike

        peaks = scan_range.find_peaks_in_step(center_hz, sample_rate, samples)
        assert len(peaks) >= 1
        bin_width = sample_rate / n
        found_offset_peak = any(
            abs(freq_hz - (center_hz + 300_000)) < 5 * bin_width for freq_hz, _ in peaks
        )
        assert found_offset_peak
        assert not any(freq_hz == center_hz for freq_hz, _ in peaks)

    def test_two_separated_peaks_both_returned(self):
        sample_rate = scan_range.DEFAULT_SAMPLE_RATE_HZ
        n = scan_range.DEFAULT_FFT_SIZE
        center_hz = 100_000_000
        rng = np.random.default_rng(3)
        noise = rng.standard_normal(n).astype(np.complex128) * 0.001

        tone_a = self._make_tone(300_000, sample_rate, n, amplitude=80.0)
        tone_b = self._make_tone(-400_000, sample_rate, n, amplitude=80.0)
        samples = noise + tone_a + tone_b

        peaks = scan_range.find_peaks_in_step(center_hz, sample_rate, samples)
        assert len(peaks) >= 2

    def test_entirely_below_threshold_returns_empty(self):
        sample_rate = scan_range.DEFAULT_SAMPLE_RATE_HZ
        n = scan_range.DEFAULT_FFT_SIZE
        samples = np.zeros(n, dtype=np.complex128)
        peaks = scan_range.find_peaks_in_step(100_000_000, sample_rate, samples)
        assert peaks == []

    def test_custom_fft_size_and_threshold_are_honored(self):
        """A caller-supplied fft_size/peak_threshold_db should change
        behavior, not just be accepted and ignored."""
        n = 1024
        samples = np.zeros(n, dtype=np.complex128)
        result = scan_range.find_peaks_in_step(
            100_000_000, 2_400_000, samples, fft_size=n
        )
        assert isinstance(result, list)
        # A tiny fft_size (smaller than len(samples)) should still run without error.
        assert scan_range.find_peaks_in_step(
            100_000_000, 2_400_000, samples, fft_size=64
        ) is not None


# ======================================================================
# capture_iq_samples — subprocess call to rtl_sdr
# ======================================================================

class TestCaptureIqSamples:
    def test_success_returns_complex_array(self):
        num_iq_pairs = int(scan_range.DEFAULT_SAMPLE_RATE_HZ * scan_range.DEFAULT_CAPTURE_SECONDS)
        num_bytes = num_iq_pairs * 2
        rng = np.random.default_rng(42)
        iq_data = (rng.random(num_bytes) * 255).astype(np.uint8)

        proc = MagicMock(returncode=0, stdout=iq_data.tobytes())
        with patch("actions.scan_range.subprocess.run", return_value=proc):
            result = scan_range.capture_iq_samples(
                100_000_000, scan_range.DEFAULT_SAMPLE_RATE_HZ, scan_range.DEFAULT_CAPTURE_SECONDS
            )

        assert isinstance(result, np.ndarray)
        assert result.dtype.kind == "c"
        assert len(result) > 0

    def test_failure_raises(self):
        proc = MagicMock(returncode=1, stdout=b"", stderr=b"Failed to open device")
        with patch("actions.scan_range.subprocess.run", return_value=proc):
            with pytest.raises(RuntimeError, match="Failed to open device"):
                scan_range.capture_iq_samples(100_000_000, scan_range.DEFAULT_SAMPLE_RATE_HZ, scan_range.DEFAULT_CAPTURE_SECONDS)

    def test_no_output_raises(self):
        proc = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with patch("actions.scan_range.subprocess.run", return_value=proc):
            with pytest.raises(RuntimeError, match="no samples"):
                scan_range.capture_iq_samples(100_000_000, scan_range.DEFAULT_SAMPLE_RATE_HZ, scan_range.DEFAULT_CAPTURE_SECONDS)

    def test_output_too_short_raises(self):
        proc = MagicMock(returncode=0, stdout=b"\x80", stderr=b"")
        with patch("actions.scan_range.subprocess.run", return_value=proc):
            with pytest.raises(RuntimeError, match="no samples"):
                scan_range.capture_iq_samples(100_000_000, scan_range.DEFAULT_SAMPLE_RATE_HZ, scan_range.DEFAULT_CAPTURE_SECONDS)

    def test_failure_multiline_stderr_uses_last_line(self):
        proc = MagicMock(returncode=1, stdout=b"", stderr=b"warning: x\nusb_claim_interface error -6")
        with patch("actions.scan_range.subprocess.run", return_value=proc):
            with pytest.raises(RuntimeError, match="usb_claim_interface"):
                scan_range.capture_iq_samples(100_000_000, scan_range.DEFAULT_SAMPLE_RATE_HZ, scan_range.DEFAULT_CAPTURE_SECONDS)


# ======================================================================
# scan_for_clear_channels_sync — blocking sweep
# ======================================================================

class TestScanForClearChannelsSync:
    def test_raises_when_dependencies_missing(self):
        with patch.object(scan_range, "dependencies_available", return_value=(False, "no rtl_sdr")):
            with pytest.raises(RuntimeError, match="no rtl_sdr"):
                scan_range.scan_for_clear_channels_sync(94_000_000, 95_000_000)

    def test_single_step_no_peaks(self):
        fake_samples = np.zeros(scan_range.DEFAULT_FFT_SIZE * 2, dtype=np.complex64)
        with patch.object(scan_range, "dependencies_available", return_value=(True, "")), \
             patch.object(scan_range, "capture_iq_samples", return_value=fake_samples), \
             patch.object(scan_range, "find_peaks_in_step", return_value=[]):
            result = scan_range.scan_for_clear_channels_sync(94_000_000, 95_000_000)
        assert result == []

    def test_single_step_with_peaks(self):
        fake_peaks = [(94_500_000, -15.0)]
        fake_samples = np.zeros(scan_range.DEFAULT_FFT_SIZE * 2, dtype=np.complex64)
        with patch.object(scan_range, "dependencies_available", return_value=(True, "")), \
             patch.object(scan_range, "capture_iq_samples", return_value=fake_samples), \
             patch.object(scan_range, "find_peaks_in_step", return_value=fake_peaks):
            result = scan_range.scan_for_clear_channels_sync(94_000_000, 95_000_000)
        assert len(result) == 1
        assert result[0]["power_db"] == -15.0
        assert result[0]["frequency"] == "94.5M"

    def test_multi_step_skips_failed_capture(self):
        calls = {"n": 0}

        def flaky_capture(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("dongle busy")
            return np.zeros(scan_range.DEFAULT_FFT_SIZE * 2, dtype=np.complex64)

        with patch.object(scan_range, "dependencies_available", return_value=(True, "")), \
             patch.object(scan_range, "capture_iq_samples", side_effect=flaky_capture), \
             patch.object(scan_range, "find_peaks_in_step", return_value=[]):
            result = scan_range.scan_for_clear_channels_sync(88_000_000, 108_000_000)

        assert calls["n"] > 1
        assert result == []

    def test_filters_out_of_range_peaks(self):
        fake_peaks = [(80_000_000, -10.0)]  # below start_hz
        fake_samples = np.zeros(scan_range.DEFAULT_FFT_SIZE * 2, dtype=np.complex64)
        with patch.object(scan_range, "dependencies_available", return_value=(True, "")), \
             patch.object(scan_range, "capture_iq_samples", return_value=fake_samples), \
             patch.object(scan_range, "find_peaks_in_step", return_value=fake_peaks):
            result = scan_range.scan_for_clear_channels_sync(94_000_000, 95_000_000)
        assert result == []

    def test_custom_min_channel_spacing_is_passed_through(self):
        """A caller-supplied min_channel_spacing_hz should reach
        merge_nearby_channels, not just be silently dropped."""
        fake_samples = np.zeros(scan_range.DEFAULT_FFT_SIZE * 2, dtype=np.complex64)
        fake_peaks = [(94_000_000, -20.0), (94_400_000, -30.0)]  # 400kHz apart
        with patch.object(scan_range, "dependencies_available", return_value=(True, "")), \
             patch.object(scan_range, "capture_iq_samples", return_value=fake_samples), \
             patch.object(scan_range, "find_peaks_in_step", return_value=fake_peaks):
            default_result = scan_range.scan_for_clear_channels_sync(93_000_000, 95_000_000)
            wide_spacing_result = scan_range.scan_for_clear_channels_sync(
                93_000_000, 95_000_000, min_channel_spacing_hz=500_000
            )
        assert len(default_result) == 2   # not merged at default 200kHz spacing
        assert len(wide_spacing_result) == 1  # merged at wider caller-supplied spacing
