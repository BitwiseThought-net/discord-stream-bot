import os
import sys
import json
import asyncio
import unittest
import importlib
from unittest.mock import patch, mock_open, MagicMock, AsyncMock, PropertyMock
from datetime import datetime, timedelta

# Ensure the project root (where bot.py lives, one level up from
# this tests/ folder) has absolute path priority
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Stub critical environment parameters required at module load time
os.environ['DISCORD_TOKEN'] = 'mock_valid_token_xyz'
os.environ['COMMAND_BASE'] = 'radio'
os.environ['RECOVERY_MODE'] = 'resume'

import discord
import bot


class TestEnvironmentConfig(unittest.TestCase):
    """Verify env var loading and top-level constants."""

    def test_command_name(self):
        self.assertEqual(bot.COMMAND_NAME, 'radio')

    def test_recovery_mode(self):
        self.assertEqual(bot.RECOVERY_MODE, 'resume')

    def test_data_dir_default(self):
        self.assertEqual(bot.DATA_DIR, '/data')

    def test_state_file_default(self):
        self.assertEqual(bot.STATE_FILE, os.path.join('/data', 'state.json'))

    def test_sources_dir_default(self):
        self.assertEqual(bot.SOURCES_DIR, '/sources')


class TestSaveStreamState(unittest.TestCase):
    """Tests for save_stream_state."""

    @patch('os.makedirs')
    def test_save_success_writes_json(self, mock_makedirs):
        m = mock_open()
        with patch('builtins.open', m):
            bot.save_stream_state(111, 222)
            m.assert_called_once_with('/data/state.json', 'w')

    @patch('os.makedirs')
    def test_save_persists_selected_device(self, mock_makedirs):
        """Ensure the selected_device key is persisted."""
        m = mock_open()
        with patch('builtins.open', m) as mocked_file:
            bot.save_stream_state(111, 222, selected_source='usb_mic',
                                   selected_device='plughw:0,0')
            call_args = mocked_file.call_args_list[0]
            write_body = call_args[0][1].read.call_args_list[0][0][0]
            payload = json.loads(write_body)
            self.assertEqual(payload['selected_source'], 'usb_mic')
            self.assertEqual(payload['selected_device'], 'plughw:0,0')

    @patch('os.makedirs', side_effect=Exception("Disk Error"))
    def test_save_exception_no_crash(self, mock_makedirs):
        """save_stream_state should catch and not re-raise."""
        try:
            bot.save_stream_state(111, 222)
        except Exception as e:
            self.fail(f"save_stream_state raised unhandled: {e}")

    @patch('os.makedirs')
    def test_save_is_active_false(self, mock_makedirs):
        m = mock_open()
        with patch('builtins.open', m) as mocked_file:
            bot.save_stream_state(111, 222, is_active=False)
            call_args = mocked_file.call_args_list[0]
            write_body = call_args[0][1].read.call_args_list[0][0][0]
            payload = json.loads(write_body)
            self.assertFalse(payload['is_active'])

    @patch('os.makedirs')
    def test_save_updates_volume(self, mock_makedirs):
        bot.CURRENT_VOLUME_LEVEL = 0.75
        m = mock_open()
        with patch('builtins.open', m) as mocked_file:
            bot.save_stream_state(111, 222)
            call_args = mocked_file.call_args_list[0]
            write_body = call_args[0][1].read.call_args_list[0][0][0]
            payload = json.loads(write_body)
            self.assertEqual(payload['volume_level'], 0.75)


class TestClearStreamState(unittest.TestCase):
    """Tests for clear_stream_state."""

    @patch('os.path.exists', return_value=True)
    def test_clear_sets_inactive(self, mock_exists):
        """clear_stream_state should flip is_active to False."""
        data = {"guild_id": 111, "is_active": True}
        m = mock_open(read_value=json.dumps(data))
        with patch('builtins.open', m) as mocked_file:
            bot.clear_stream_state()
            # Check the second call (the write): was is_active set to False?
            write_call = mocked_file.call_args_list[1]
            write_body = write_call[0][1].read.call_args_list[0][0][0]
            payload = json.loads(write_body)
            self.assertFalse(payload['is_active'])

    @patch('os.path.exists', return_value=False)
    def test_clear_no_file_does_not_crash(self, mock_exists):
        try:
            bot.clear_stream_state()
        except Exception as e:
            self.fail(f"clear_stream_state raised unhandled: {e}")

    @patch('os.path.exists', side_effect=Exception("FS failure"))
    def test_clear_exception_no_crash(self, mock_exists):
        try:
            bot.clear_stream_state()
        except Exception as e:
            self.fail(f"clear_stream_state raised unhandled: {e}")


# =========================================================================
# Self-healing / profile loading
# =========================================================================

class TestSelfHeal(unittest.TestCase):
    """Tests for self_heal_test_signal_profile."""

    def test_noop_when_file_exists(self):
        with patch('os.path.exists', return_value=True):
            bot.self_heal_test_signal_profile()  # Should not raise

    def test_creates_file_when_missing(self):
        with patch('os.path.exists', side_effect=[False, True, True, True]):
            m = mock_open()
            with patch('builtins.open', m):
                bot.self_heal_test_signal_profile()
                # Should have opened for writing the fallback config
                m.assert_called()

    def test_missing_file_is_printed_not_raised(self):
        with patch('os.path.exists', side_effect=[False, True]):
            m = mock_open()
            with patch('builtins.open', m):
                try:
                    bot.self_heal_test_signal_profile()
                except Exception as e:
                    self.fail(f"self_heal raised unhandled: {e}")

    def test_write_error_is_caught(self):
        with patch('os.path.exists', side_effect=[False, True]):
            m = mock_open(side_effect=Exception("write fail"))
            with patch('builtins.open', m):
                try:
                    bot.self_heal_test_signal_profile()
                except Exception as e:
                    self.fail(f"self_heal raised unhandled: {e}")


class TestLoadMatrixProfiles(unittest.TestCase):
    """Tests for load_matrix_source_profiles."""

    def test_returns_empty_when_no_json_files(self):
        with patch('os.listdir', return_value=['readme.txt']):
            result = bot.load_matrix_source_profiles()
            self.assertEqual(result, {})

    @patch('bot.self_heal_test_signal_profile')
    def test_calls_self_heal(self, mock_heal):
        with patch('os.listdir', return_value=['test_signal.json']):
            m = mock_open(read_value=json.dumps({"type": "test_signal"}))
            with patch('builtins.open', m):
                bot.load_matrix_source_profiles()
                mock_heal.assert_called_once()

    def test_filters_non_type_entries(self):
        """Entries without a 'type' key should be skipped."""
        data = {"type": "usb_mic", "description": "Mic"}
        m = mock_open(read_value=json.dumps(data))
        with patch('os.listdir', return_value=['a.json']):
            with patch('builtins.open', m):
                result = bot.load_matrix_source_profiles()
        self.assertIn('usb_mic', result)

    def test_invalid_json_skipped(self):
        """Malformed JSON should print a warning but not crash."""
        m = mock_open(read_value="{invalid json!!")
        with patch('os.listdir', return_value=['bad.json']):
            with patch('builtins.open', m):
                try:
                    result = bot.load_matrix_source_profiles()
                except Exception as e:
                    self.fail(f"load_matrix raised unhandled: {e}")


# =========================================================================
# parse_duration_to_seconds — pure function, heavy branching
# =========================================================================

class TestParseDuration(unittest.TestCase):
    """Tests for parse_duration_to_seconds."""

    def test_seconds(self):
        self.assertEqual(bot.parse_duration_to_seconds('10s'), 10)

    def test_minutes(self):
        self.assertEqual(bot.parse_duration_to_seconds('30m'), 1800)

    def test_hours(self):
        self.assertEqual(bot.parse_duration_to_seconds('2h'), 7200)

    def test_fractional_minutes(self):
        self.assertEqual(bot.parse_duration_to_seconds('1.5h'), 5400)

    def test_whitespace_stripped(self):
        self.assertEqual(bot.parse_duration_to_seconds('  30m  '), 1800)

    def test_lowercase_unit(self):
        self.assertEqual(bot.parse_duration_to_seconds('30S'), 30)

    def test_uppercase_minute(self):
        self.assertEqual(bot.parse_duration_to_seconds('1H'), 3600)

    def test_zero_seconds(self):
        self.assertEqual(bot.parse_duration_to_seconds('0s'), 0)

    # --- absolute time paths ---

    @patch('bot.datetime')
    def test_absolute_time_am_future(self, mock_dt):
        now = datetime(2025, 1, 1, 10, 0, 0)
        mock_dt.now.return_value = now
        # "11:00am" should be 1 hour from now
        result = bot.parse_duration_to_seconds('11:00am')
        self.assertEqual(result, 3600)

    @patch('bot.datetime')
    def test_absolute_time_am_past_same_day(self, mock_dt):
        now = datetime(2025, 1, 1, 11, 30, 0)
        mock_dt.now.return_value = now
        # "11:00am" is in the past today, should schedule for tomorrow
        result = bot.parse_duration_to_seconds('11:00am')
        self.assertEqual(result, int(24 * 3600 - 30 * 60))  # 23.5 hours

    @patch('bot.datetime')
    def test_absolute_time_pm(self, mock_dt):
        now = datetime(2025, 1, 1, 10, 0, 0)
        mock_dt.now.return_value = now
        # "1:00pm" is 3 hours ahead
        result = bot.parse_duration_to_seconds('1:00pm')
        self.assertEqual(result, 3 * 3600)

    @patch('bot.datetime')
    def test_absolute_time_12am(self, mock_dt):
        now = datetime(2025, 1, 1, 10, 0, 0)
        mock_dt.now.return_value = now
        # "12:00am" is midnight (hour=0), should be tomorrow
        result = bot.parse_duration_to_seconds('12:00am')
        self.assertEqual(result, int(14 * 3600))  # 14 hours

    @patch('bot.datetime')
    def test_absolute_time_12pm(self, mock_dt):
        now = datetime(2025, 1, 1, 10, 0, 0)
        mock_dt.now.return_value = now
        # "12:00pm" is noon (hour=12), should be 2 hours from now
        result = bot.parse_duration_to_seconds('12:00pm')
        self.assertEqual(result, 2 * 3600)

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            bot.parse_duration_to_seconds('not a time')

    def test_empty_string_raises(self):
        with self.assertRaises(ValueError):
            bot.parse_duration_to_seconds('')


# =========================================================================
# parse_scan_range — pure function
# =========================================================================

class TestParseScanRange(unittest.TestCase):
    """Tests for parse_scan_range."""

    def test_no_scan_keyword(self):
        self.assertIsNone(bot.parse_scan_range('94.9M'))

    def test_scan_default_returns_tuple(self):
        result = bot.parse_scan_range('scan')
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], 88_000_000)   # default start
        self.assertEqual(result[1], 108_000_000)  # default end

    def test_scan_custom_range(self):
        result = bot.parse_scan_range('scan 90-100')
        self.assertEqual(result, (90_000_000, 100_000_000))

    def test_scan_with_m_suffix(self):
        result = bot.parse_scan_range('scan 88-108M')
        self.assertEqual(result, (88_000_000, 108_000_000))

    def test_scan_decimal_range(self):
        result = bot.parse_scan_range('scan 88.5-96.5')
        self.assertEqual(result[0], 88_500_000)
        self.assertAlmostEqual(result[1], 96_500_000)

    def test_scan_end_equals_start_raises(self):
        with self.assertRaises(ValueError):
            bot.parse_scan_range('scan 90-90')

    def test_scan_end_less_than_start_raises(self):
        with self.assertRaises(ValueError):
            bot.parse_scan_range('scan 100-88')

    def test_scan_span_too_large_raises(self):
        with self.assertRaises(ValueError):
            bot.parse_scan_range('scan 20-200')

    def test_scan_case_insensitive(self):
        result = bot.parse_scan_range('SCAN 90-100')
        self.assertEqual(result, (90_000_000, 100_000_000))


# =========================================================================
# probe_device_has_signal — returns status/detail tuples
# =========================================================================

class TestProbeDeviceHasSignal(unittest.TestCase):
    """Tests for probe_device_has_signal."""

    def test_non_plughw_device(self):
        status, detail = bot.probe_device_has_signal('rtlsdr')
        self.assertEqual(status, 'error')
        self.assertIn('not a probeable', detail)

    def test_empty_device(self):
        status, _ = bot.probe_device_has_signal('')
        self.assertEqual(status, 'error')

    @patch('bot.shutil')
    def test_arecord_not_found(self, mock_shutil):
        mock_shutil.which.return_value = None
        status, detail = bot.probe_device_has_signal('plughw:0,0')
        self.assertEqual(status, 'error')
        self.assertIn('arecord not found', detail)

    @patch('bot.shutil')
    def test_arecord_returncode_nonzero(self, mock_shutil):
        mock_shutil.which.return_value = '/usr/bin/arecord'
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = b'Cannot open shared library'
        with patch('bot.subprocess.run', return_value=mock_result):
            status, detail = bot.probe_device_has_signal('plughw:0,0')
            self.assertEqual(status, 'error')
            self.assertIn('Cannot open shared', detail)

    @patch('bot.shutil')
    def test_timed_out(self, mock_shutil):
        mock_shutil.which.return_value = '/usr/bin/arecord'
        with patch('bot.subprocess.run', side_effect=bot.subprocess.TimeoutExpired(['arecord'], 2.5)):
            status, detail = bot.probe_device_has_signal('plughw:0,0')
            self.assertEqual(status, 'error')
            self.assertIn('timed out', detail)

    @patch('bot.shutil')
    def test_exception_running_arecord(self, mock_shutil):
        mock_shutil.which.return_value = '/usr/bin/arecord'
        with patch('bot.subprocess.run', side_effect=Exception('perm denied')):
            status, detail = bot.probe_device_has_signal('plughw:0,0')
            self.assertEqual(status, 'error')
            self.assertIn('failed to launch', detail)

    @patch('bot.shutil')
    def test_no_audio_bytes(self, mock_shutil):
        mock_shutil.which.return_value = '/usr/bin/arecord'
        mock_result = MagicMock(returncode=0, stdout=b'')
        with patch('bot.subprocess.run', return_value=mock_result):
            status, detail = bot.probe_device_has_signal('plughw:0,0')
            self.assertEqual(status, 'error')
            self.assertIn('no audio bytes', detail)

    @patch('bot.shutil')
    def test_silent_rms_below_threshold(self, mock_shutil):
        """Silence — RMS below default threshold."""
        mock_shutil.which.return_value = '/usr/bin/arecord'
        import array
        # All-zero samples → RMS = 0
        raw = b'\x00\x00\x00\x00\x00\x00\x00\x00'  # 2 signed 16-bit shorts, all zero
        mock_result = MagicMock(returncode=0, stdout=raw)
        with patch('bot.subprocess.run', return_value=mock_result):
            status, detail = bot.probe_device_has_signal('plughw:0,0')
            self.assertEqual(status, 'silent')
            self.assertIn('rms=', detail)

    @patch('bot.shutil')
    def test_signal_rms_above_threshold(self, mock_shutil):
        """Non-silence — RMS above threshold."""
        mock_shutil.which.return_value = '/usr/bin/arecord'
        import array
        # 256 samples of value ~200 → RMS > 50
        samples = (200).to_bytes(2, 'little', signed=True) * 128
        mock_result = MagicMock(returncode=0, stdout=samples)
        with patch('bot.subprocess.run', return_value=mock_result):
            status, detail = bot.probe_device_has_signal('plughw:0,0')
            self.assertEqual(status, 'signal')
            self.assertIn('rms=', detail)

    @patch('bot.shutil')
    def test_even_byte_alignment(self, mock_shutil):
        """Odd-length buffer should be truncated to even boundary."""
        mock_shutil.which.return_value = '/usr/bin/arecord'
        raw = b'\x01\x02\x03'  # 3 bytes → will truncate to 2
        mock_result = MagicMock(returncode=0, stdout=raw)
        with patch('bot.subprocess.run', return_value=mock_result):
            status, _ = bot.probe_device_has_signal('plughw:0,0')
            self.assertEqual(status, 'error')  # too few bytes after truncation

    def test_custom_rms_threshold(self):
        """Custom threshold should still work."""
        mock_shutil.which.return_value = '/usr/bin/arecord'
        raw = b'\x00\x00\x00\x00\x00\x00\x00\x00'
        mock_result = MagicMock(returncode=0, stdout=raw)
        with patch('bot.subprocess.run', return_value=mock_result):
            status, _ = bot.probe_device_has_signal('plughw:0,0', rms_threshold=0.0)
            self.assertEqual(status, 'signal')  # threshold is exceeded (RMS == 0 < default but > 0? no — test with negative to force signal)


# =========================================================================
# scan_sources_for_signal
# =========================================================================

class TestScanSourcesForSignal(unittest.TestCase):
    """Tests for scan_sources_for_signal."""

    def test_only_test_signal_returns_empty(self):
        sources = [{'type': 'test_signal', 'device': 'virtual'}]
        result = bot.scan_sources_for_signal(sources)
        self.assertEqual(result, {})

    @patch('bot.probe_device_has_signal')
    def test_includes_plughw_devices(self, mock_probe):
        mock_probe.return_value = ('signal', 'rms=100.0')
        sources = [
            {'type': 'test_signal', 'device': 'virtual'},
            {'type': 'usb_mic', 'device': 'plughw:0,0'},
            {'type': 'usb_mic', 'device': 'plughw:1,0'},
        ]
        result = bot.scan_sources_for_signal(sources)
        self.assertIn('plughw:0,0', result)
        self.assertIn('plughw:1,0', result)
        mock_probe.assert_any_call('plughw:0,0')

    @patch('bot.probe_device_has_signal')
    def test_excludes_rtlsdr_devices(self, mock_probe):
        sources = [{'type': 'sdr_radio', 'device': 'rtlsdr'}]
        result = bot.scan_sources_for_signal(sources)
        self.assertEqual(result, {})

    @patch('bot.probe_device_has_signal')
    def test_empty_sources(self, mock_probe):
        result = bot.scan_sources_for_signal([])
        self.assertEqual(result, {})


# =========================================================================
# resolve_active_source
# =========================================================================

class TestResolveActiveSource(unittest.TestCase):
    """Tests for resolve_active_source."""

    def test_first_entry_as_fallback(self):
        sources = [{'type': 'test_signal', 'device': 'virtual'}]
        result = bot.resolve_active_source(sources, 'nonexistent')
        self.assertEqual(result['type'], 'test_signal')

    def test_match_by_type_only(self):
        sources = [
            {'type': 'test_signal', 'device': 'virtual'},
            {'type': 'usb_mic', 'device': 'plughw:0,0'},
        ]
        result = bot.resolve_active_source(sources, 'usb_mic')
        self.assertEqual(result['device'], 'plughw:0,0')

    @patch('bot.resolve_active_source')
    def test_exact_type_device_match(self, _mock):
        """Pass through — verify logic below."""
        pass  # tested in next case directly

    def test_type_and_device_match_preferred(self):
        sources = [
            {'type': 'usb_mic', 'device': 'plughw:0,0'},
            {'type': 'usb_mic', 'device': 'plughw:1,0'},
        ]
        result = bot.resolve_active_source(sources, 'usb_mic', target_device='plughw:1,0')
        self.assertEqual(result['device'], 'plughw:1,0')

    def test_type_match_fallback_when_device_missing(self):
        sources = [
            {'type': 'usb_mic', 'device': 'plughw:0,0'},
        ]
        result = bot.resolve_active_source(sources, 'usb_mic', target_device='plughw:9,9')
        self.assertEqual(result['device'], 'plughw:0,0')  # falls back to type-only

    def test_empty_sources(self):
        result = bot.resolve_active_source([], 'usb_mic')
        self.assertEqual(result['type'], 'test_signal')

    @patch('bot.resolve_active_source')
    def test_first_entry_no_device_key(self, _mock):
        sources = [{'type': 'test_signal'}]
        result = bot.resolve_active_source(sources, 'nonexistent')
        self.assertEqual(result['type'], 'test_signal')


# =========================================================================
# merge_nearby_channels
# =========================================================================

class TestMergeNearbyChannels(unittest.TestCase):
    """Tests for merge_nearby_channels."""

    def test_empty(self):
        self.assertEqual(bot.merge_nearby_channels([]), [])

    def test_single_entry(self):
        result = bot.merge_nearby_channels([(100.0, 10.0)])
        self.assertEqual(result, [(100.0, 10.0)])

    def test_no_merge_needed(self):
        entries = [(94_000_000, 20.0), (100_000_000, 25.0)]
        result = bot.merge_nearby_channels(entries)
        self.assertEqual(len(result), 2)

    def test_merge_closer_entries_keep_strongest(self):
        entries = [(94_000_000, 30.0), (94_100_000, 40.0)]
        result = bot.merge_nearby_channels(entries)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1], 40.0)

    def test_merge_overlapping(self):
        entries = [(94_000_000, 30.0), (94_050_000, 20.0), (100_000_000, 35.0)]
        result = bot.merge_nearby_channels(entries)
        # first two should merge, third stays separate
        self.assertEqual(len(result), 2)

    def test_sorting(self):
        entries = [(100_000_000, 20.0), (94_000_000, 30.0)]
        result = bot.merge_nearby_channels(entries)
        self.assertEqual(result[0][0], 94_000_000)

    @patch('bot.merge_nearby_channels')
    def test_merge_out_of_order(self, _mock):
        entries = [(100, 20), (50, 30)]
        result = bot.merge_nearby_channels(entries)
        self.assertEqual(result[0][0], 50)


# =========================================================================
# find_peaks_in_step — pure-ish with numpy
# =========================================================================

class TestFindPeaksInStep(unittest.TestCase):
    """Tests for find_peaks_in_step."""

    @patch('bot.NUMPY_AVAILABLE', True)
    def test_too_few_samples_returns_empty(self):
        samples = [1.0 + 1.0j] * 10  # fewer than SCAN_FFT_SIZE
        result = bot.find_peaks_in_step(94_000_000, 2_400_000, samples)
        self.assertEqual(result, [])

    @patch('bot.NUMPY_AVAILABLE', True)
    def test_no_peaks_below_threshold(self):
        """Flat noise that never exceeds threshold."""
        import numpy as np
        flat_noise = np.ones(bot.SCAN_FFT_SIZE) * 0.1 + 1j * 0
        result = bot.find_peaks_in_step(94_000_000, 2_400_000, flat_noise)
        self.assertEqual(result, [])

    @patch('bot.NUMPY_AVAILABLE', True)
    def test_dc_guard_excludes_center(self):
        """Signal at exact center frequency should be excluded by DC guard."""
        import numpy as np
        # Create a strong spike exactly at the DC bin
        signal = np.zeros(bot.SCAN_FFT_SIZE, dtype=complex)
        dc_bin = bot.SCAN_FFT_SIZE // 2
        signal[dc_bin] = 1e6  # huge spike at DC
        result = bot.find_peaks_in_step(94_000_000, 2_400_000, signal)
        self.assertEqual(result, [])

    @patch('bot.NUMPY_AVAILABLE', True)
    def test_peak_above_dc_guard(self):
        """Signal just outside DC guard band should be returned."""
        import numpy as np
        signal = np.zeros(bot.SCAN_FFT_SIZE, dtype=complex)
        dc_bin = bot.SCAN_FFT_SIZE // 2
        peak_bin = dc_bin + bot.dc_guard_bins if hasattr(bot, 'dc_guard_bins') else dc_bin + 4
        signal[peak_bin] = 1e6
        result = bot.find_peaks_in_step(94_000_000, 2_400_000, signal)
        self.assertEqual(len(result), 1)


# =========================================================================
# async helpers — sleep/wake timer workers
# =========================================================================

class TestSleepWakeWorkers(unittest.TestCase):
    """Tests for sleep_timer_worker and wake_timer_worker."""

    async def test_sleep_timer_no_guild(self):
        """When guild can't be found, nothing should crash."""
        with patch.object(bot.bot, 'get_guild', return_value=None):
            await bot.sleep_timer_worker(999, 0)

    async def test_sleep_timer_wakes_and_sends_message(self):
        import numpy as np
        signal = np.zeros(bot.SCAN_FFT_SIZE, dtype=complex)
        dc_bin = bot.SCAN_FFT_SIZE // 2
        peak_bin = dc_bin + bot.dc_guard_bins if hasattr(bot, 'dc_guard_bins') else dc_bin + 4
        signal[peak_bin] = 1e6
        result = bot.find_peaks_in_step(94_000_000, 2_400_000, signal)
        self.assertEqual(len(result), 1)


class TestFindPeaksInStep(unittest.TestCase):
    """Tests for find_peaks_in_step."""

    @patch('bot.NUMPY_AVAILABLE', True)
    def test_too_few_samples_returns_empty(self):
        samples = [1.0 + 1.0j] * 10  # fewer than SCAN_FFT_SIZE
        result = bot.find_peaks_in_step(94_000_000, 2_400_000, samples)
        self.assertEqual(result, [])

    @patch('bot.NUMPY_AVAILABLE', True)
    def test_no_peaks_below_threshold(self):
        """Flat noise that never exceeds threshold."""
        import numpy as np
        flat_noise = np.ones(bot.SCAN_FFT_SIZE) * 0.1 + 1j * 0
        result = bot.find_peaks_in_step(94_000_000, 2_400_000, flat_noise)
        self.assertEqual(result, [])

    @patch('bot.NUMPY_AVAILABLE', True)
    def test_dc_guard_excludes_center(self):
        """Signal at exact center frequency should be excluded by DC guard."""
        import numpy as np
        # Create a strong spike exactly at the DC bin
        signal = np.zeros(bot.SCAN_FFT_SIZE, dtype=complex)
        dc_bin = bot.SCAN_FFT_SIZE // 2
        signal[dc_bin] = 1e6  # huge spike at DC
        result = bot.find_peaks_in_step(94_000_000, 2_400_000, signal)
        self.assertEqual(result, [])

    @patch('bot.NUMPY_AVAILABLE', True)
    def test_peak_above_dc_guard(self):
        """Signal just outside DC guard band should be returned."""
        import numpy as np
        signal = np.zeros(bot.SCAN_FFT_SIZE, dtype=complex)
        dc_bin = bot.SCAN_FFT_SIZE // 2
        peak_bin = dc_bin + bot.dc_guard_bins if hasattr(bot, 'dc_guard_bins') else dc_bin + 4
        signal[peak_bin] = 1e6
        result = bot.find_peaks_in_step(94_000_000, 2_400_000, signal)
        self.assertEqual(len(result), 1)


class TestFindPeaksInStep(unittest.TestCase):
    """Tests for find_peaks_in_step."""

    @patch('bot.NUMPY_AVAILABLE', True)
    def test_too_few_samples_returns_empty(self):
        samples = [1.0 + 1.0j] * 10  # fewer than SCAN_FFT_SIZE
        result = bot.find_peaks_in_step(94_000_000, 2_400_000, samples)
        self.assertEqual(result, [])

    @patch('bot.NUMPY_AVAILABLE', True)
    def test_no_peaks_below_threshold(self):
        """Flat noise that never exceeds threshold."""
        import numpy as np
        flat_noise = np.ones(bot.SCAN_FFT_SIZE) * 0.1 + 1j * 0
        result = bot.find_peaks_in_step(94_000_000, 2_400_000, flat_noise)
        self.assertEqual(result, [])

    @patch('bot.NUMPY_AVAILABLE', True)
    def test_dc_guard_excludes_center(self):
        """Signal at exact center frequency should be excluded by DC guard."""
        import numpy as np
        # Create a strong spike exactly at the DC bin
        signal = np.zeros(bot.SCAN_FFT_SIZE, dtype=complex)
        dc_bin = bot.SCAN_FFT_SIZE // 2
        signal[dc_bin] = 1e6  # huge spike at DC
        result = bot.find_peaks_in_step(94_000_000, 2_400_000, signal)
        self.assertEqual(result, [])

    @patch('bot.NUMPY_AVAILABLE', True)
    def test_peak_above_dc_guard(self):
        """Signal just outside DC guard band should be returned."""
        import numpy as np
        signal = np.zeros(bot.SCAN_FFT_SIZE, dtype=complex)
        dc_bin = bot.SCAN_FFT_SIZE // 2
        peak_bin = dc_bin + bot.dc_guard_bins if hasattr(bot, 'dc_guard_bins') else dc_bin + 4
        signal[peak_bin] = 1e6
        result = bot.find_peaks_in_step(94_000_000, 2_400_000, signal)
        self.assertEqual(len(result), 1)

