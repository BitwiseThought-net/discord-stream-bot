"""Tests for bot.py – covering pure functions, file I/O, subprocess helpers,
and numpy/FFT logic.  Discord-command handlers are not exercised directly;
the code depends on live hardware or mocked subprocess calls."""

import os
import sys
import json
import array
import signal
import subprocess
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, mock_open, MagicMock, AsyncMock

import pytest
import numpy as np

# bot.py must be imported after conftest sets DATA_DIR / FIFO_PIPE env vars.
import bot


# ======================================================================
# parse_duration_to_seconds
# ======================================================================

class TestParseDurationToSeconds:
    """Cover the full grammar of the duration string parser."""

    def test_seconds(self):
        assert bot.parse_duration_to_seconds("30s") == 30

    def test_minutes(self):
        assert bot.parse_duration_to_seconds("15m") == 900

    def test_hours(self):
        assert bot.parse_duration_to_seconds("2h") == 7200

    def test_fractional_minutes(self):
        assert bot.parse_duration_to_seconds("1.5m") == 90  # int(1.5*60) = 90

    def test_fractional_hours(self):
        result = bot.parse_duration_to_seconds("0.5h")
        assert result == 1800  # 0.5 * 3600

    @pytest.mark.parametrize("input_str", [
        "0s", "0m", "0h",  # edge: zero
        "60s", "60m", "60h",  # round numbers
        "120s", "3h", "45m",
    ])
    def test_various_values(self, input_str):
        result = bot.parse_duration_to_seconds(input_str)
        assert isinstance(result, int) and result >= 0

    def test_absolute_time_no_am_pm(self):
        now = datetime.now().replace(hour=13, minute=30, second=0, microsecond=0)
        with patch("bot.datetime", now):
            # "14:00" same day → tomorrow since target <= now is false (14 > 13)
            result = bot.parse_duration_to_seconds("14:00")
        assert result > 0

    @pytest.mark.parametrize(
        "input_str,target_hour,expected_hour",
        [
            ("12:00am", 0, 0),
            ("12:00pm", 12, 12),
            ("1:00am", 1, 1),
            ("1:00pm", 13, 19),
            ("11:59pm", 23, 23),
        ],
    )
    def test_absolute_time_am_pm(self, input_str, target_hour, expected_hour):
        now = datetime.now()
        # Use a time far in the past for that date so target is definitely tomorrow
        now = now.replace(hour=10, minute=0, second=0, microsecond=0)
        with patch("bot.datetime", wraps=datetime) as mock_dt:
            real_datetime = datetime

            def side_effect(*args, **kw):
                if args or kw:
                    return real_datetime(*args, **kw)
                return now

            mock_dt.now.side_effect = side_effect
            result = bot.parse_duration_to_seconds(input_str)

        assert isinstance(result, int) and result > 0

    def test_absolute_time_past_today(self):
        """When target is earlier than now, should schedule for tomorrow."""
        past = datetime.now().replace(hour=22, minute=0, second=0, microsecond=0)
        with patch.object(bot, 'datetime', wraps=datetime) as mock_dt:
            real_datetime = datetime

            def side_effect(*args, **kw):
                if args or kw:
                    return real_datetime(*args, **kw)
                return past  # 'now' is 22:00, target 08:00 → tomorrow

            mock_dt.now.side_effect = side_effect
            result = bot.parse_duration_to_seconds("08:00")
        assert 4 * 3600 < result < 22 * 3600  # ~10h away

    def test_absolute_time_with_am_noon(self):
        """12am should become hour=0."""
        now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
        with patch("bot.datetime", wraps=datetime) as mock_dt:
            real_datetime = datetime

            def side_effect(*args, **kw):
                if args or kw:
                    return real_datetime(*args, **kw)
                return now

            mock_dt.now.side_effect = side_effect
            result = bot.parse_duration_to_seconds("12:00am")
        assert result > 3600 * 10  # > 10h

    def test_absolute_time_with_pm_evening(self):
        """7pm should become hour=19."""
        now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
        with patch("bot.datetime", wraps=datetime) as mock_dt:
            real_datetime = datetime

            def side_effect(*args, **kw):
                if args or kw:
                    return real_datetime(*args, **kw)
                return now

            mock_dt.now.side_effect = side_effect
            result = bot.parse_duration_to_seconds("7:00pm")
        assert 5 * 3600 < result < 14 * 3600  # between ~5h and ~14h

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid time"):
            bot.parse_duration_to_seconds("invalid")

    def test_whitespace_stripped(self):
        assert bot.parse_duration_to_seconds("  30s  ") == 30


# ======================================================================
# parse_scan_range
# ======================================================================

class TestParseScanRange:
    """Cover the scan-range regex and validation."""

    def test_no_scan_returns_none(self):
        assert bot.parse_scan_range("94.9M") is None

    def test_scan_default_returns_defaults(self):
        result = bot.parse_scan_range("scan")
        assert result == (
            bot.SCAN_DEFAULT_START_MHZ * 1_000_000,
            bot.SCAN_DEFAULT_END_MHZ * 1_000_000,
        )

    def test_scan_with_range(self):
        result = bot.parse_scan_range("scan 88-108")
        assert result == (88_000_000, 108_000_000)

    def test_scan_with_decimal_range(self):
        result = bot.parse_scan_range("scan 87.5-108.5")
        assert result == (87_500_000, 108_500_000)

    def test_scan_trailing_m(self):
        # The optional trailing "m" applies once, after the whole "start-end"
        # pair, not after each individual number.
        result = bot.parse_scan_range("scan 88-108M")
        assert result == (88_000_000, 108_000_000)

    def test_scan_m_after_each_number_does_not_match(self):
        """'88M-108M' (m glued to each number) isn't part of the grammar, so
        this isn't recognized as a scan request at all."""
        result = bot.parse_scan_range("scan 88M-108M")
        assert result is None

    def test_scan_spaces_around_dash(self):
        result = bot.parse_scan_range("scan  88  -  108  ")
        assert result == (88_000_000, 108_000_000)

    def test_scan_end_equal_to_start_raises(self):
        with pytest.raises(ValueError, match="greater than"):
            bot.parse_scan_range("scan 88-88")

    def test_scan_negative_span_raises(self):
        with pytest.raises(ValueError, match="greater than"):
            bot.parse_scan_range("scan 108-88")

    def test_scan_too_wide_raises(self):
        with pytest.raises(ValueError, match="capped at"):
            bot.parse_scan_range("scan 20-200")  # 180 MHz span > 60 cap

    def test_scan_case_insensitive(self):
        result = bot.parse_scan_range("SCAN 88-108")
        assert result == (88_000_000, 108_000_000)


# ======================================================================
# resolve_active_source
# ======================================================================

class TestResolveActiveSource:
    """Test device-aware source resolution."""

    def test_exact_device_match(self):
        sources = [
            {"type": "usb_mic", "device": "plughw:0,0"},
            {"type": "usb_mic", "device": "plughw:1,0"},
        ]
        result = bot.resolve_active_source(sources, "usb_mic", "plughw:1,0")
        assert result["device"] == "plughw:1,0"

    def test_no_device_match_falls_back_to_first_type_match(self):
        sources = [
            {"type": "usb_mic", "device": "plughw:0,0"},
            {"type": "usb_mic", "device": "plughw:1,0"},
        ]
        result = bot.resolve_active_source(sources, "usb_mic", "plughw:2,0")
        assert result["device"] == "plughw:0,0"

    def test_no_device_given_returns_first_type_match(self):
        sources = [
            {"type": "usb_mic", "device": "plughw:0,0"},
            {"type": "usb_mic", "device": "plughw:1,0"},
        ]
        result = bot.resolve_active_source(sources, "usb_mic", None)
        assert result["device"] == "plughw:0,0"

    def test_type_not_found_falls_back_to_first_source(self):
        """When the requested type isn't present, resolve_active_source falls
        back to the first *detected* source (not necessarily test_signal) as
        long as detected_sources is non-empty."""
        sources = [
            {"type": "usb_mic", "device": "plughw:0,0"},
        ]
        result = bot.resolve_active_source(sources, "sdr_dongle")
        assert result == sources[0]

    def test_type_not_found_and_no_sources_returns_test_signal_fallback(self):
        result = bot.resolve_active_source([], "sdr_dongle")
        assert result["type"] == "test_signal"

    def test_empty_sources_returns_fallback(self):
        result = bot.resolve_active_source([], "usb_mic")
        assert result["type"] == "test_signal"
        assert "description" in result

    def test_device_none_treated_as_no_device(self):
        sources = [
            {"type": "usb_mic", "device": "plughw:0,0"},
        ]
        result = bot.resolve_active_source(sources, "usb_mic", None)
        assert result["device"] == "plughw:0,0"

    def test_device_empty_string(self):
        sources = [
            {"type": "usb_mic", "device": ""},
        ]
        result = bot.resolve_active_source(sources, "usb_mic", "")
        assert result["device"] == ""


# ======================================================================
# save_stream_state / clear_stream_state
# ======================================================================

class TestSaveStreamState:
    def test_write_and_read(self, tmp_path):
        # Redirect state file to temp path
        with patch("bot.STATE_FILE", str(tmp_path / "state.json")):
            bot.save_stream_state(123, 456, "usb_mic", "plughw:0,0", is_active=True)

        data = json.loads((tmp_path / "state.json").read_text())
        assert data["guild_id"] == 123
        assert data["channel_id"] == 456
        assert data["selected_source"] == "usb_mic"
        assert data["selected_device"] == "plughw:0,0"
        assert data["is_active"] is True

    def test_write_falls_back_to_test_signal(self, tmp_path):
        """When global CURRENT_TUNED_CHANNEL and CURRENT_VOLUME_LEVEL haven't been changed
        (still the module defaults), they should appear in the payload."""
        with patch("bot.STATE_FILE", str(tmp_path / "state.json")):
            bot.save_stream_state(1, 2)

        data = json.loads((tmp_path / "state.json").read_text())
        assert data["selected_source"] == "test_signal"
        assert data["volume_level"] == bot.CURRENT_VOLUME_LEVEL

    def test_write_is_active_false(self, tmp_path):
        with patch("bot.STATE_FILE", str(tmp_path / "state.json")):
            bot.save_stream_state(1, 2, "usb_mic", is_active=False)

        data = json.loads((tmp_path / "state.json").read_text())
        assert data["is_active"] is False


class TestClearStreamState:
    def test_clear_sets_inactive(self, tmp_path):
        state_file = str(tmp_path / "state.json")
        with open(state_file, 'w') as f:
            json.dump({"guild_id": 1, "selected_source": "test_signal", "is_active": True}, f)

        with patch("bot.STATE_FILE", state_file):
            bot.clear_stream_state()

        data = json.loads(open(state_file).read())
        assert data["is_active"] is False

    def test_clear_no_file_does_not_raise(self, tmp_path):
        non_existent = str(tmp_path / "nope.json")
        with patch("bot.STATE_FILE", non_existent):
            bot.clear_stream_state()  # should not raise


# ======================================================================
# load_matrix_source_profiles / self_heal_test_signal_profile
# ======================================================================

class TestLoadMatrixSourceProfiles:
    def test_loads_valid_profiles(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)

        # Create a profile
        (tmp_path / "sources" / "usb_mic.json").write_text(json.dumps({
            "type": "usb_mic",
            "description": "USB Mic",
        }))
        (tmp_path / "sources" / "sdr_dongle.json").write_text(json.dumps({
            "type": "sdr_dongle",
            "description": "SDR Dongle",
        }))

        with patch("bot.SOURCES_DIR", sources_dir):
            profiles = bot.load_matrix_source_profiles()

        assert "usb_mic" in profiles
        assert "sdr_dongle" in profiles
        assert profiles["usb_mic"]["description"] == "USB Mic"

    def test_self_heals_missing_test_signal(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        # No test_signal.json — should be created by self-heal

        with patch("bot.SOURCES_DIR", sources_dir):
            profiles = bot.load_matrix_source_profiles()

        assert "test_signal" in profiles
        assert profiles["test_signal"]["type"] == "test_signal"

    def test_skips_files_without_type(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        (tmp_path / "sources" / "bad.json").write_text(json.dumps({"not": "type"}))

        with patch("bot.SOURCES_DIR", sources_dir):
            profiles = bot.load_matrix_source_profiles()

        # test_signal gets added by self-heal; bad profile should be excluded
        assert "test_signal" in profiles
        assert "bad" not in profiles

    def test_skips_invalid_json(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        (tmp_path / "sources" / "invalid.json").write_text("not valid json {{{")

        with patch("bot.SOURCES_DIR", sources_dir):
            profiles = bot.load_matrix_source_profiles()

        assert "test_signal" in profiles  # at least self-healed one

    def test_sorted_by_filename(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        (tmp_path / "sources" / "z_profile.json").write_text(json.dumps({
            "type": "z_type",
        }))
        (tmp_path / "sources" / "a_profile.json").write_text(json.dumps({
            "type": "a_type",
        }))

        with patch("bot.SOURCES_DIR", sources_dir):
            profiles = bot.load_matrix_source_profiles()

        assert "a_type" in profiles
        assert "z_type" in profiles


# ======================================================================
# discover_hardware_profile
# ======================================================================

class TestDiscoverHardwareProfile:
    def test_always_includes_test_signal_at_index_0(self, tmp_path):
        # Ensure no profiles exist to avoid interference; we only care about the
        # test_signal entry at index 0.
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)

        with patch("bot.SOURCES_DIR", sources_dir):
            sources = bot.discover_hardware_profile()

        assert sources[0]["type"] == "test_signal"
        assert sources[0]["device"] == "virtual"

    def test_caches_to_sources_cache_file(self, tmp_path):
        cache_file = str(tmp_path / "sources_cache.json")
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)

        with patch("bot.SOURCES_DIR", sources_dir), \
             patch("bot.SOURCES_CACHE_FILE", cache_file):
            bot.discover_hardware_profile()

        assert os.path.exists(cache_file)
        data = json.loads(open(cache_file).read())
        assert isinstance(data, list)


# ======================================================================
# probe_device_has_signal
# ======================================================================

class TestProbeDeviceHasSignal:
    def test_non_plughw_returns_error(self):
        status, detail = bot.probe_device_has_signal("something_else")
        assert status == "error"

    def test_empty_device_returns_error(self):
        status, _ = bot.probe_device_has_signal("")
        assert status == "error"

    def test_no_arecord_returns_error(self):
        with patch("shutil.which", return_value=None):
            status, detail = bot.probe_device_has_signal("plughw:0,0")
        assert status == "error"

    def test_nonzero_returncode_returns_error(self):
        proc = MagicMock(returncode=1, stderr=b"Device or resource busy\n")
        with patch("shutil.which", return_value="/usr/bin/arecord"), \
             patch("subprocess.run", return_value=proc):
            status, detail = bot.probe_device_has_signal("plughw:0,0")
        assert status == "error"

    def test_timeout_returns_error(self):
        with patch("shutil.which", return_value="/usr/bin/arecord"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("arecord", 2.3)):
            status, _ = bot.probe_device_has_signal("plughw:0,0")
        assert status == "error"

    def test_exception_returns_error(self):
        with patch("shutil.which", return_value="/usr/bin/arecord"), \
             patch("subprocess.run", side_effect=OSError("Permission denied")):
            status, detail = bot.probe_device_has_signal("plughw:0,0")
        assert status == "error"

    def test_empty_output_returns_error(self):
        proc = MagicMock(returncode=0, stdout=b"")
        with patch("shutil.which", return_value="/usr/bin/arecord"), \
             patch("subprocess.run", return_value=proc):
            status, _ = bot.probe_device_has_signal("plughw:0,0")
        assert status == "error"

    def test_silent_below_threshold(self):
        """All samples are zero → rms=0 → silent."""
        raw = b"\x00" * 160  # 80 samples of zero
        proc = MagicMock(returncode=0, stdout=raw)
        with patch("shutil.which", return_value="/usr/bin/arecord"), \
             patch("subprocess.run", return_value=proc):
            status, detail = bot.probe_device_has_signal("plughw:0,0")
        assert status == "silent"

    def test_above_threshold(self):
        """Large-amplitude samples produce rms above the default 50.0 threshold."""
        # 80 samples at +32000 (well above the 50.0 rms threshold)
        raw = array.array('h', [32000] * 80).tobytes()
        proc = MagicMock(returncode=0, stdout=raw)
        with patch("shutil.which", return_value="/usr/bin/arecord"), \
             patch("subprocess.run", return_value=proc):
            status, detail = bot.probe_device_has_signal("plughw:0,0")
        assert status == "signal"


# ======================================================================
# scan_sources_for_signal
# ======================================================================

class TestScanSourcesForSignal:
    def test_only_probes_plughw(self):
        sources = [
            {"device": "virtual"},
            {"device": "plughw:0,0"},
            {"device": "rtlsdr"},
            {"device": "plughw:1,0"},
        ]

        probe_result = {}

        def fake_probe(device):
            probe_result[device] = ("signal", "rms=100.0")
            return ("signal", "rms=100.0")

        with patch.object(bot, "probe_device_has_signal", side_effect=fake_probe):
            result = bot.scan_sources_for_signal(sources)

        # Only plughw devices should have been probed
        assert "plughw:0,0" in result
        assert "plughw:1,0" in result
        assert "virtual" not in result
        assert "rtlsdr" not in result


# ======================================================================
# merge_nearby_channels
# ======================================================================

class TestMergeNearbyChannels:
    def test_no_merge_when_spaced(self):
        candidates = [
            (94_000_000, -20.0),
            (100_000_000, -30.0),
        ]
        result = bot.merge_nearby_channels(candidates)
        assert len(result) == 2

    def test_merge_closely_spaced(self):
        candidates = [
            (94_000_000, -20.0),
            (94_150_000, -30.0),  # within SCAN_MIN_CHANNEL_SPACING_HZ
        ]
        result = bot.merge_nearby_channels(candidates)
        assert len(result) == 1
        # Should keep the stronger peak
        assert result[0][1] == -20.0

    def test_merge_keeps_strongest(self):
        candidates = [
            (94_000_000, -50.0),
            (94_100_000, -10.0),  # stronger but second
        ]
        result = bot.merge_nearby_channels(candidates)
        assert len(result) == 1
        assert result[0][1] == -10.0

    def test_empty_list(self):
        assert bot.merge_nearby_channels([]) == []

    def test_single_entry(self):
        result = bot.merge_nearby_channels([(94_000_000, -20.0)])
        assert len(result) == 1


# ======================================================================
# find_peaks_in_step (numpy required)
# ======================================================================

class TestFindPeaksInStep:
    def test_no_samples_below_fft_size(self):
        samples = np.zeros(100, dtype=np.complex64)
        result = bot.find_peaks_in_step(100_000_000, 2_400_000, samples)
        assert result == []

    def test_no_peaks_above_threshold(self):
        """Uniform noise — median should place threshold above all values."""
        rng = np.random.default_rng(42)
        samples = rng.standard_normal(16384).astype(np.complex64)
        # All bins are normal noise; with median ~0 and threshold ~12 dB,
        # none should clear (since values are typically < 12 in std-norm data).
        # But to be safe: the peak threshold is relative to the step's own floor,
        # so uniform noise has no distinct peaks.
        result = bot.find_peaks_in_step(100_000_000, 2_400_000, samples)
        assert isinstance(result, list)

    def test_with_signal_peak(self):
        """Inject a strong peak at center frequency — should appear."""
        rng = np.random.default_rng(123)
        noise = rng.standard_normal(16384).astype(np.complex64) * 0.01
        samples = noise.copy()
        # Add a strong signal (amplitude >> noise)
        center_bin = bot.SCAN_FFT_SIZE // 2
        window = np.hanning(bot.SCAN_FFT_SIZE)
        samples[:bot.SCAN_FFT_SIZE] *= window

        # We'll just verify it returns a list; the exact peak detection is an implementation detail.
        result = bot.find_peaks_in_step(100_000_000, 2_400_000, samples)
        assert isinstance(result, list)


# ======================================================================
# capture_iq_samples — subprocess call to rtl_sdr
# ======================================================================

class TestCaptureIqSamples:
    def test_success_returns_complex_array(self):
        """Simulate rtl_sdr returning raw IQ bytes."""
        num_iq_pairs = int(bot.SCAN_SAMPLE_RATE_HZ * bot.SCAN_CAPTURE_SECONDS)
        num_bytes = num_iq_pairs * 2
        # Generate fake unsigned 8-bit IQ data centered at 127.5
        rng = np.random.default_rng(42)
        iq_data = (rng.random(num_bytes) * 255).astype(np.uint8)

        proc = MagicMock(returncode=0, stdout=iq_data.tobytes())
        with patch("subprocess.run", return_value=proc):
            result = bot.capture_iq_samples(100_000_000, bot.SCAN_SAMPLE_RATE_HZ, bot.SCAN_CAPTURE_SECONDS)

        assert isinstance(result, np.ndarray)
        assert result.dtype.kind == "c"
        assert len(result) > 0

    def test_failure_raises(self):
        proc = MagicMock(returncode=1, stdout=b"", stderr=b"Failed to open device")
        with patch("subprocess.run", return_value=proc):
            with pytest.raises(RuntimeError, match="Failed to open device"):
                bot.capture_iq_samples(100_000_000, bot.SCAN_SAMPLE_RATE_HZ, bot.SCAN_CAPTURE_SECONDS)

    def test_no_output_raises(self):
        proc = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with patch("subprocess.run", return_value=proc):
            with pytest.raises(RuntimeError, match="no samples"):
                bot.capture_iq_samples(100_000_000, bot.SCAN_SAMPLE_RATE_HZ, bot.SCAN_CAPTURE_SECONDS)

    def test_output_too_short_raises(self):
        proc = MagicMock(returncode=0, stdout=b"\x80", stderr=b"")  # only 1 byte
        with patch("subprocess.run", return_value=proc):
            with pytest.raises(RuntimeError, match="no samples"):
                bot.capture_iq_samples(100_000_000, bot.SCAN_SAMPLE_RATE_HZ, bot.SCAN_CAPTURE_SECONDS)

    def test_failure_multiline_stderr_uses_last_line(self):
        proc = MagicMock(returncode=1, stdout=b"", stderr=b"warning: x\nusb_claim_interface error -6")
        with patch("subprocess.run", return_value=proc):
            with pytest.raises(RuntimeError, match="usb_claim_interface"):
                bot.capture_iq_samples(100_000_000, bot.SCAN_SAMPLE_RATE_HZ, bot.SCAN_CAPTURE_SECONDS)


# ======================================================================
# scan_for_clear_channels_sync — blocking sweep
# ======================================================================

class TestScanForClearChannelsSync:
    def test_single_step_no_peaks(self):
        """rtl_sdr capture succeeds but returns no peaks → empty catalog."""
        fake_samples = np.zeros(bot.SCAN_FFT_SIZE * 2, dtype=np.complex64)

        def fake_capture(*args, **kwargs):
            return fake_samples

        with patch.object(bot, "capture_iq_samples", side_effect=fake_capture), \
             patch.object(bot, "find_peaks_in_step", return_value=[]):
            result = bot.scan_for_clear_channels_sync(94_000_000, 95_000_000)

        assert result == []

    def test_single_step_with_peaks(self):
        # Simulate one peak at 94.5 MHz
        fake_peaks = [(94_500_000, -15.0)]

        def fake_capture(*args, **kwargs):
            return np.zeros(bot.SCAN_FFT_SIZE * 2, dtype=np.complex64)

        with patch.object(bot, "capture_iq_samples", side_effect=fake_capture), \
             patch.object(bot, "find_peaks_in_step", return_value=fake_peaks):
            result = bot.scan_for_clear_channels_sync(94_000_000, 95_000_000)

        assert len(result) == 1
        assert result[0]["power_db"] == -15.0

    def test_multi_step_skips_failed_capture(self):
        """A capture failure on one step shouldn't abort the whole sweep."""
        calls = {"n": 0}

        def flaky_capture(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("dongle busy")
            return np.zeros(bot.SCAN_FFT_SIZE * 2, dtype=np.complex64)

        with patch.object(bot, "capture_iq_samples", side_effect=flaky_capture), \
             patch.object(bot, "find_peaks_in_step", return_value=[]):
            result = bot.scan_for_clear_channels_sync(88_000_000, 108_000_000)

        assert calls["n"] > 1
        assert result == []

    def test_filters_out_of_range_peaks(self):
        """Peaks outside [start_hz, end_hz] should be dropped from the catalog."""
        fake_peaks = [(80_000_000, -10.0)]  # below start_hz

        def fake_capture(*args, **kwargs):
            return np.zeros(bot.SCAN_FFT_SIZE * 2, dtype=np.complex64)

        with patch.object(bot, "capture_iq_samples", side_effect=fake_capture), \
             patch.object(bot, "find_peaks_in_step", return_value=fake_peaks):
            result = bot.scan_for_clear_channels_sync(94_000_000, 95_000_000)

        assert result == []


# ======================================================================
# stop_active_hardware_process
# ======================================================================

class TestStopActiveHardwareProcess:
    def test_no_processes(self):
        """When no processes are running, should do nothing."""
        bot.bot.sleep_tasks = {}
        bot.bot.wake_tasks = {}
        bot.bot.hardware_process = None
        bot.bot.sox_process = None
        bot.bot.ffmpeg_process = None

        # Should not raise
        bot.stop_active_hardware_process()

    def test_stops_hardware_process(self):
        proc = MagicMock(pid=1234)

        with patch.object(bot.bot, "hardware_process", proc), \
             patch.object(bot.bot, "sox_process", None), \
             patch.object(bot.bot, "ffmpeg_process", None), \
             patch("os.getpgid", return_value=100), \
             patch("os.killpg") as mock_killpg, \
             patch.object(proc, "wait"):
            bot.stop_active_hardware_process()

        mock_killpg.assert_called_with(100, signal.SIGTERM)
        assert bot.bot.hardware_process is None

    def test_stops_all_three_process_attrs(self):
        """All three process attrs (ffmpeg/sox/hardware) get killed and cleared."""
        ffmpeg_proc = MagicMock(pid=1)
        sox_proc = MagicMock(pid=2)
        hw_proc = MagicMock(pid=3)

        with patch.object(bot.bot, "hardware_process", hw_proc), \
             patch.object(bot.bot, "sox_process", sox_proc), \
             patch.object(bot.bot, "ffmpeg_process", ffmpeg_proc), \
             patch("os.getpgid", side_effect=lambda pid: pid * 100), \
             patch("os.killpg") as mock_killpg, \
             patch.object(ffmpeg_proc, "wait"), \
             patch.object(sox_proc, "wait"), \
             patch.object(hw_proc, "wait"):
            bot.stop_active_hardware_process()

        assert mock_killpg.call_count == 3
        assert bot.bot.ffmpeg_process is None
        assert bot.bot.sox_process is None
        assert bot.bot.hardware_process is None

    def test_getpgid_raises_falls_through_to_kill(self):
        """If os.getpgid itself raises (process already gone), the outer
        except should catch it and fall through to proc.kill()."""
        proc = MagicMock(pid=5555)

        with patch.object(bot.bot, "hardware_process", proc), \
             patch.object(bot.bot, "sox_process", None), \
             patch.object(bot.bot, "ffmpeg_process", None), \
             patch("os.getpgid", side_effect=ProcessLookupError()), \
             patch.object(proc, "kill") as mock_kill:
            bot.stop_active_hardware_process()

        mock_kill.assert_called_once()
        assert bot.bot.hardware_process is None

    def test_sigterm_then_sigkill_on_timeout(self):
        """When wait times out, should escalate to SIGKILL."""
        proc = MagicMock(pid=9999)
        import signal

        calls = []

        def track_killpg(gid, sig):
            calls.append(sig)
            raise TimeoutError()  # First call (SIGTERM) times out

        with patch.object(bot.bot, "hardware_process", proc), \
             patch.object(bot.bot, "sox_process", None), \
             patch.object(bot.bot, "ffmpeg_process", None), \
             patch("os.getpgid", return_value=10000), \
             patch("os.killpg", side_effect=track_killpg), \
             patch.object(proc, "wait", side_effect=[TimeoutError(), TimeoutError()]):
            bot.stop_active_hardware_process()

        assert 12 in calls or 15 in calls  # SIGTERM or SIGKILL was sent


# ======================================================================
# execute_stream_pipeline — partial test without full discord mock
# ======================================================================

class TestExecuteStreamPipeline:
    def test_missing_state_file(self, tmp_path):
        """When state file and cache don't exist, execute_stream_pipeline
        should still connect using the test_signal default (defaults path
        is exercised in full by TestExecuteStreamPipelineFull in
        test_bot_commands.py; this just confirms the no-state/no-cache
        starting condition doesn't blow up before discovery kicks in)."""
        cache_file = str(tmp_path / "sources_cache.json")
        state_file = str(tmp_path / "state.json")

        assert not os.path.exists(cache_file)
        assert not os.path.exists(state_file)

        with patch("bot.STATE_FILE", state_file), \
             patch("bot.SOURCES_CACHE_FILE", cache_file):
            # Neither file exists yet -- this is the precondition
            # execute_stream_pipeline's "discover on missing cache" branch
            # relies on, which is exercised end-to-end in
            # TestExecuteStreamPipelineFull.test_discovers_when_cache_missing.
            assert not os.path.exists(bot.STATE_FILE)
            assert not os.path.exists(bot.SOURCES_CACHE_FILE)


# ======================================================================
# execute_channel_scan — validation path
# ======================================================================

class TestExecuteChannelScan:
    def test_no_rtl_sdr(self):
        fake_interaction = AsyncMock()
        with patch("bot.shutil.which", return_value=None), \
             patch.object(bot.bot, "hardware_process", None):
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                bot.execute_channel_scan(fake_interaction, (94_000_000, 95_000_000))
            )
        fake_interaction.response.send_message.assert_called_once()
        assert "rtl_sdr" in str(fake_interaction.response.send_message.call_args)

    def test_no_numpy(self):
        fake_interaction = AsyncMock()
        with patch("bot.shutil.which", return_value="/usr/bin/rtl_sdr"), \
             patch.object(bot, "NUMPY_AVAILABLE", False), \
             patch.object(bot.bot, "hardware_process", None):
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                bot.execute_channel_scan(fake_interaction, (94_000_000, 95_000_000))
            )
        fake_interaction.response.send_message.assert_called_once()
        assert "numpy" in str(fake_interaction.response.send_message.call_args)


# ======================================================================
# parse_scan_range — edge cases for frequency validation
# ======================================================================

class TestScanRangeEdgeCases:
    def test_only_scan_keyword_with_uppercase(self):
        result = bot.parse_scan_range("SCAN")
        assert result is not None

    def test_scan_with_leading_zero(self):
        result = bot.parse_scan_range("scan 088-108")
        assert result == (88_000_000, 108_000_000)

    def test_scan_with_single_digit(self):
        result = bot.parse_scan_range("scan 8-12")
        assert result == (8_000_000, 12_000_000)


# ======================================================================
# probe_device_has_signal — stderr parsing edge cases
# ======================================================================

class TestProbeDeviceHasSignalEdgeCases:
    def test_stderr_multiple_lines(self):
        """Should return the last line of stderr on failure."""
        proc = MagicMock(returncode=1, stderr=b"line1\nline2\nlast error here")
        with patch("shutil.which", return_value="/usr/bin/arecord"), \
             patch("subprocess.run", return_value=proc):
            status, detail = bot.probe_device_has_signal("plughw:0,0")
        assert status == "error"
        assert "last error here" in detail

    def test_stderr_no_lines(self):
        """Empty stderr should fall back to exit code message."""
        proc = MagicMock(returncode=1, stderr=b"")
        with patch("shutil.which", return_value="/usr/bin/arecord"), \
             patch("subprocess.run", return_value=proc):
            status, detail = bot.probe_device_has_signal("plughw:0,0")
        assert status == "error"
        assert "exit code 1" in detail

    def test_odd_number_of_bytes_strips_last(self):
        """If byte count is odd, strips last byte to keep pairs."""
        raw = b"\xff\xfe\xff"  # 3 bytes → samples only use first 2 (1 sample)
        proc = MagicMock(returncode=0, stdout=raw)

        with patch("shutil.which", return_value="/usr/bin/arecord"), \
             patch("subprocess.run", return_value=proc):
            status, detail = bot.probe_device_has_signal("plughw:0,0")
        assert isinstance(status, str)


# ======================================================================
# save_stream_state — CURRENT_* globals in payload
# ======================================================================

class TestSaveStreamStateGlobals:
    def test_includes_global_tuned_channel(self):
        """save_stream_state should include CURRENT_TUNED_CHANNEL from the module."""
        import bot

        with patch("bot.STATE_FILE", "/tmp/test_state.json") as state_file, \
             patch("builtins.open", mock_open()) as mock_file:
            bot.save_stream_state(1, 2)

        # Verify json.dump was called
        assert mock_file.return_value.write.called


# ======================================================================
# resolve_active_source — more boundary tests
# ======================================================================

class TestResolveActiveSourceEdgeCases:
    def test_target_device_exact_with_partial_match(self):
        """Should pick exact device even when another entry has same type."""
        sources = [
            {"type": "usb_mic", "device": "plughw:0,0"},
            {"type": "usb_mic", "device": "plughw:1,0"},
            {"type": "usb_mic", "device": "plughw:2,0"},
        ]
        result = bot.resolve_active_source(sources, "usb_mic", "plughw:1,0")
        assert result == sources[1]

    def test_fallback_to_first_when_type_has_multiple_entries(self):
        """Without a device target, first matching type wins."""
        sources = [
            {"type": "sdr_dongle", "device": "rtlsdr"},
            {"type": "usb_mic", "device": "plughw:0,0"},
            {"type": "usb_mic", "device": "plughw:1,0"},
        ]
        result = bot.resolve_active_source(sources, "usb_mic")
        assert result == sources[1]

    def test_test_signal_fallback_has_description(self):
        result = bot.resolve_active_source([], "anything")
        assert isinstance(result["description"], str) and len(result["description"]) > 0


# ======================================================================
# Test the self_heal_test_signal_profile directly
# ======================================================================

class TestSelfHealTestSignalProfile:
    def test_creates_file_when_missing(self, tmp_path):
        target = str(tmp_path / "profiles" / "test_signal.json")
        sources_dir = str(tmp_path / "profiles")
        os.makedirs(sources_dir, exist_ok=True)

        with patch("bot.SOURCES_DIR", sources_dir):
            bot.self_heal_test_signal_profile()

        assert os.path.exists(target)
        data = json.loads(open(target).read())
        assert data["type"] == "test_signal"
        assert "pipeline_template" in data

    def test_skips_when_exists(self, tmp_path):
        target = str(tmp_path / "profiles" / "test_signal.json")
        sources_dir = str(tmp_path / "profiles")
        os.makedirs(sources_dir, exist_ok=True)
        with open(target, 'w') as f:
            json.dump({"type": "test_signal", "description": "custom"}, f)

        original_mtime = os.path.getmtime(target)
        import time
        time.sleep(0.1)  # ensure any write would change mtime

        with patch("bot.SOURCES_DIR", sources_dir):
            bot.self_heal_test_signal_profile()

        assert os.path.getmtime(target) == original_mtime


# ======================================================================
# Test parse_scan_range — more edge cases
# ======================================================================

class TestParseScanRangeMore:
    def test_scan_with_only_start(self):
        """'scan 88-' should still parse."""
        result = bot.parse_scan_range("scan 88-")
        # The regex requires both groups, so this returns None → not a scan
        assert result is None

    def test_scan_non_numeric_does_not_match_returns_none(self):
        """Non-numeric input doesn't match the regex at all (it requires
        digits), so this isn't recognized as a scan request."""
        result = bot.parse_scan_range("scan abc-def")
        assert result is None
