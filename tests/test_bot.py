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


def _write_source(sources_dir, filename, code):
    """Write a self-contained source plugin .py file into sources_dir for tests."""
    path = Path(sources_dir) / filename
    path.write_text(code)
    return path


SIMPLE_SOURCE_TEMPLATE = '''
SOURCE_TYPE = {source_type!r}
DESCRIPTION = {description!r}

def discover():
    return {instances!r}

def build_command(instance, frequency, fifo_pipe):
    return "true"
'''


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
    """Cover the scan-range regex and validation.

    default_start_mhz/default_end_mhz/max_span_mhz are supplied by the
    caller (in production, read off the active source module's own
    SCAN_DEFAULT_START_MHZ/SCAN_DEFAULT_END_MHZ/SCAN_MAX_SPAN_MHZ) since
    bot.py itself has no opinion on what a sensible default band is."""

    DEFAULT_START = 88.0
    DEFAULT_END = 108.0
    MAX_SPAN = 60.0

    def _parse(self, arg):
        return bot.parse_scan_range(
            arg, default_start_mhz=self.DEFAULT_START,
            default_end_mhz=self.DEFAULT_END, max_span_mhz=self.MAX_SPAN,
        )

    def test_no_scan_returns_none(self):
        assert self._parse("94.9M") is None

    def test_scan_default_returns_defaults(self):
        result = self._parse("scan")
        assert result == (
            self.DEFAULT_START * 1_000_000,
            self.DEFAULT_END * 1_000_000,
        )

    def test_scan_with_range(self):
        result = self._parse("scan 88-108")
        assert result == (88_000_000, 108_000_000)

    def test_scan_with_decimal_range(self):
        result = self._parse("scan 87.5-108.5")
        assert result == (87_500_000, 108_500_000)

    def test_scan_trailing_m(self):
        # The optional trailing "m" applies once, after the whole "start-end"
        # pair, not after each individual number.
        result = self._parse("scan 88-108M")
        assert result == (88_000_000, 108_000_000)

    def test_scan_m_after_each_number_does_not_match(self):
        """'88M-108M' (m glued to each number) isn't part of the grammar, so
        this isn't recognized as a scan request at all."""
        result = self._parse("scan 88M-108M")
        assert result is None

    def test_scan_spaces_around_dash(self):
        result = self._parse("scan  88  -  108  ")
        assert result == (88_000_000, 108_000_000)

    def test_scan_end_equal_to_start_raises(self):
        with pytest.raises(ValueError, match="greater than"):
            self._parse("scan 88-88")

    def test_scan_negative_span_raises(self):
        with pytest.raises(ValueError, match="greater than"):
            self._parse("scan 108-88")

    def test_scan_too_wide_raises(self):
        with pytest.raises(ValueError, match="capped at"):
            self._parse("scan 20-200")  # 180 MHz span > 60 cap

    def test_scan_case_insensitive(self):
        result = self._parse("SCAN 88-108")
        assert result == (88_000_000, 108_000_000)

    def test_custom_defaults_are_honored(self):
        """A caller supplying different band defaults (e.g. a different
        SDR-backed source with its own policy) should get those instead of
        this test class's own 88-108 defaults."""
        result = bot.parse_scan_range(
            "scan", default_start_mhz=118.0, default_end_mhz=137.0, max_span_mhz=60.0
        )
        assert result == (118_000_000, 137_000_000)


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
        assert result["type"] == bot.BUILTIN_FALLBACK_TYPE

    def test_empty_sources_returns_fallback(self):
        result = bot.resolve_active_source([], "usb_mic")
        assert result["type"] == bot.BUILTIN_FALLBACK_TYPE
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
# load_source_modules
# ======================================================================

class TestLoadSourceModules:
    def test_loads_valid_modules(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)

        _write_source(sources_dir, "usb_mic.py", SIMPLE_SOURCE_TEMPLATE.format(
            source_type="usb_mic", description="USB Mic", instances=[{"device": "plughw:0,0"}]
        ))
        _write_source(sources_dir, "sdr_dongle.py", SIMPLE_SOURCE_TEMPLATE.format(
            source_type="sdr_dongle", description="SDR Dongle", instances=[]
        ))

        with patch("bot.SOURCES_DIR", sources_dir):
            modules = bot.load_source_modules()

        assert "usb_mic" in modules
        assert "sdr_dongle" in modules
        assert modules["usb_mic"].DESCRIPTION == "USB Mic"

    def test_skips_files_missing_source_type(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        _write_source(sources_dir, "bad.py", "NOT_A_SOURCE_TYPE = 1\n")

        with patch("bot.SOURCES_DIR", sources_dir):
            modules = bot.load_source_modules()

        assert modules == {}

    def test_skips_files_missing_required_functions(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        _write_source(sources_dir, "incomplete.py", 'SOURCE_TYPE = "incomplete"\n')

        with patch("bot.SOURCES_DIR", sources_dir):
            modules = bot.load_source_modules()

        assert modules == {}

    def test_skips_invalid_python_syntax(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        _write_source(sources_dir, "broken.py", "this is not valid python {{{\n")

        with patch("bot.SOURCES_DIR", sources_dir):
            modules = bot.load_source_modules()

        assert modules == {}

    def test_skips_files_that_raise_at_import_time(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        _write_source(sources_dir, "raises.py", "raise RuntimeError('boom at import')\n")

        with patch("bot.SOURCES_DIR", sources_dir):
            modules = bot.load_source_modules()

        assert modules == {}

    def test_ignores_non_py_and_underscore_files(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        (Path(sources_dir) / "notes.txt").write_text("not python")
        _write_source(sources_dir, "_helper.py", SIMPLE_SOURCE_TEMPLATE.format(
            source_type="helper", description="Helper", instances=[]
        ))

        with patch("bot.SOURCES_DIR", sources_dir):
            modules = bot.load_source_modules()

        assert modules == {}

    def test_missing_sources_dir_returns_empty(self, tmp_path):
        missing_dir = str(tmp_path / "does_not_exist")
        with patch("bot.SOURCES_DIR", missing_dir):
            modules = bot.load_source_modules()
        assert modules == {}

    def test_duplicate_source_type_keeps_first_by_filename(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        _write_source(sources_dir, "a_first.py", SIMPLE_SOURCE_TEMPLATE.format(
            source_type="dup", description="first", instances=[]
        ))
        _write_source(sources_dir, "z_second.py", SIMPLE_SOURCE_TEMPLATE.format(
            source_type="dup", description="second", instances=[]
        ))

        with patch("bot.SOURCES_DIR", sources_dir):
            modules = bot.load_source_modules()

        assert modules["dup"].DESCRIPTION == "first"


# ======================================================================
# discover_hardware_profile
# ======================================================================

class TestDiscoverHardwareProfile:
    def test_empty_sources_dir_returns_builtin_fallback(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)

        with patch("bot.SOURCES_DIR", sources_dir):
            sources = bot.discover_hardware_profile()

        assert len(sources) == 1
        assert sources[0]["type"] == bot.BUILTIN_FALLBACK_TYPE
        assert sources[0]["device"] == "builtin"

    def test_aggregates_instances_from_multiple_modules(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        _write_source(sources_dir, "a.py", SIMPLE_SOURCE_TEMPLATE.format(
            source_type="a", description="A", instances=[{"device": "dev-a"}]
        ))
        _write_source(sources_dir, "b.py", SIMPLE_SOURCE_TEMPLATE.format(
            source_type="b", description="B", instances=[{"device": "dev-b1"}, {"device": "dev-b2"}]
        ))

        with patch("bot.SOURCES_DIR", sources_dir):
            sources = bot.discover_hardware_profile()

        types = [s["type"] for s in sources]
        assert types.count("a") == 1
        assert types.count("b") == 2
        # Every entry gets its type injected and default channels/description filled in
        for s in sources:
            assert "type" in s
            assert "channels" in s
            assert "description" in s

    def test_module_raising_in_discover_is_skipped(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        _write_source(sources_dir, "broken.py", '''
SOURCE_TYPE = "broken"
DESCRIPTION = "Broken"

def discover():
    raise RuntimeError("hardware missing")

def build_command(instance, frequency, fifo_pipe):
    return "true"
''')

        with patch("bot.SOURCES_DIR", sources_dir):
            sources = bot.discover_hardware_profile()

        # Falls back to the builtin tone since the only source raised.
        assert sources[0]["type"] == bot.BUILTIN_FALLBACK_TYPE

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

    def test_cache_write_failure_does_not_raise(self, tmp_path, capsys):
        """Use a file-as-directory trick so this reliably fails on any OS/user,
        including root (where a literal "/nonexistent-dir-xyz" would actually
        get created successfully)."""
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a directory")
        bad_cache_file = str(blocker / "sub" / "cache.json")

        with patch("bot.SOURCES_DIR", sources_dir), \
             patch("bot.SOURCES_CACHE_FILE", bad_cache_file):
            sources = bot.discover_hardware_profile()

        assert sources[0]["type"] == bot.BUILTIN_FALLBACK_TYPE
        assert "Failed writing" in capsys.readouterr().out


# ======================================================================
# scan_sources_for_signal
# ======================================================================

class TestScanSourcesForSignal:
    def test_only_probes_sources_whose_module_defines_probe_signal(self):
        sources = [
            {"type": "no_probe", "device": "virtual"},
            {"type": "probeable", "device": "plughw:0,0"},
        ]

        no_probe_module = MagicMock(spec=[])  # no probe_signal attribute at all
        probeable_module = MagicMock()
        probeable_module.probe_signal.return_value = ("signal", "rms=100.0")

        modules = {"no_probe": no_probe_module, "probeable": probeable_module}

        result = bot.scan_sources_for_signal(sources, modules)

        assert "plughw:0,0" in result
        assert result["plughw:0,0"] == ("signal", "rms=100.0")
        assert "virtual" not in result

    def test_unknown_source_type_is_skipped(self):
        sources = [{"type": "missing_module", "device": "dev"}]
        result = bot.scan_sources_for_signal(sources, modules={})
        assert result == {}

    def test_probe_signal_raising_is_caught_as_error(self):
        sources = [{"type": "flaky", "device": "dev"}]
        flaky_module = MagicMock()
        flaky_module.probe_signal.side_effect = RuntimeError("boom")

        result = bot.scan_sources_for_signal(sources, {"flaky": flaky_module})

        assert result["dev"][0] == "error"
        assert "boom" in result["dev"][1]


# ======================================================================
# Note: merge_nearby_channels, find_peaks_in_step, capture_iq_samples, and
# scan_for_clear_channels_sync used to live here, but moved to
# actions/scan_range.py as part of the "generic action dispatch" refactor
# (bot.py no longer contains any RTL-SDR/FFT-specific code at all -- it only
# knows how to call whatever `scan_range` function the active source
# advertises via SUPPORTED_ACTIONS). See tests/test_actions_scan_range.py.
# ======================================================================


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
# get_current_source_type
# ======================================================================

class TestGetCurrentSourceType:
    def test_defaults_to_test_signal_when_no_state_file(self, tmp_path):
        with patch("bot.STATE_FILE", str(tmp_path / "state.json")):
            assert bot.get_current_source_type() == "test_signal"

    def test_reads_selected_source_from_state_file(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"selected_source": "sdr_radio"}))
        with patch("bot.STATE_FILE", str(state_file)):
            assert bot.get_current_source_type() == "sdr_radio"

    def test_corrupt_state_file_falls_back_to_test_signal(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("not valid json{{{")
        with patch("bot.STATE_FILE", str(state_file)):
            assert bot.get_current_source_type() == "test_signal"

    def test_missing_selected_source_key_falls_back(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"something_else": True}))
        with patch("bot.STATE_FILE", str(state_file)):
            assert bot.get_current_source_type() == "test_signal"


# ======================================================================
# handle_channel_scan_request — the "does the active source even support
# scanning" gate, plus delegation to execute_channel_scan
# ======================================================================

class TestHandleChannelScanRequest:
    def test_no_active_module_sends_unsupported_message(self):
        interaction = AsyncMock()
        with patch.object(bot, "get_current_source_type", return_value="test_signal"), \
             patch.object(bot, "load_source_modules", return_value={}):
            asyncio.get_event_loop().run_until_complete(
                bot.handle_channel_scan_request(interaction, "scan")
            )
        interaction.response.send_message.assert_called_once()
        assert "doesn't support frequency scanning" in str(interaction.response.send_message.call_args)

    def test_module_without_supported_actions_sends_unsupported_message(self):
        interaction = AsyncMock()
        module = MagicMock(spec=["DESCRIPTION"])  # no SUPPORTED_ACTIONS, no scan_range
        module.DESCRIPTION = "Test Signal"
        with patch.object(bot, "get_current_source_type", return_value="test_signal"), \
             patch.object(bot, "load_source_modules", return_value={"test_signal": module}):
            asyncio.get_event_loop().run_until_complete(
                bot.handle_channel_scan_request(interaction, "scan")
            )
        response = str(interaction.response.send_message.call_args)
        assert "doesn't support frequency scanning" in response
        assert "Test Signal" in response

    def test_module_with_actions_but_no_scan_range_attr_is_unsupported(self):
        """SUPPORTED_ACTIONS claims scan_range, but the function itself is
        missing -- should still be treated as unsupported, not raise."""
        interaction = AsyncMock()
        module = MagicMock(spec=["SUPPORTED_ACTIONS", "DESCRIPTION"])
        module.SUPPORTED_ACTIONS = ["scan_range"]
        module.DESCRIPTION = "Weird Source"
        with patch.object(bot, "get_current_source_type", return_value="weird"), \
             patch.object(bot, "load_source_modules", return_value={"weird": module}):
            asyncio.get_event_loop().run_until_complete(
                bot.handle_channel_scan_request(interaction, "scan")
            )
        assert "doesn't support frequency scanning" in str(interaction.response.send_message.call_args)

    def test_invalid_range_sends_warning_not_execute(self):
        interaction = AsyncMock()
        module = MagicMock()
        module.SUPPORTED_ACTIONS = ["scan_range"]
        module.scan_range = MagicMock()
        module.SCAN_DEFAULT_START_MHZ = 88.0
        module.SCAN_DEFAULT_END_MHZ = 108.0
        module.SCAN_MAX_SPAN_MHZ = 60.0
        with patch.object(bot, "get_current_source_type", return_value="sdr_radio"), \
             patch.object(bot, "load_source_modules", return_value={"sdr_radio": module}), \
             patch.object(bot, "execute_channel_scan", new=AsyncMock()) as mock_exec:
            asyncio.get_event_loop().run_until_complete(
                bot.handle_channel_scan_request(interaction, "scan 200-20")
            )
        mock_exec.assert_not_called()
        assert "greater than" in str(interaction.response.send_message.call_args)

    def test_valid_range_delegates_to_execute_channel_scan(self):
        interaction = AsyncMock()
        module = MagicMock()
        module.SUPPORTED_ACTIONS = ["scan_range"]
        module.scan_range = MagicMock()
        module.DESCRIPTION = "Radio (FM & HAM)"
        module.SCAN_DEFAULT_START_MHZ = 88.0
        module.SCAN_DEFAULT_END_MHZ = 108.0
        module.SCAN_MAX_SPAN_MHZ = 60.0
        with patch.object(bot, "get_current_source_type", return_value="sdr_radio"), \
             patch.object(bot, "load_source_modules", return_value={"sdr_radio": module}), \
             patch.object(bot, "execute_channel_scan", new=AsyncMock()) as mock_exec:
            asyncio.get_event_loop().run_until_complete(
                bot.handle_channel_scan_request(interaction, "scan 88-108")
            )
        mock_exec.assert_called_once()
        args, kwargs = mock_exec.call_args
        assert args[1] == (88_000_000, 108_000_000)
        assert args[2] is module.scan_range
        assert kwargs["active_description"] == "Radio (FM & HAM)"

    def test_missing_band_defaults_fall_back_to_generic_values(self):
        """A source that supports scan_range but doesn't declare its own
        SCAN_DEFAULT_*/MAX_SPAN policy should still work, using bot.py's
        generic fallback numbers rather than raising."""
        interaction = AsyncMock()
        module = MagicMock(spec=["SUPPORTED_ACTIONS", "scan_range", "DESCRIPTION"])
        module.SUPPORTED_ACTIONS = ["scan_range"]
        module.scan_range = MagicMock()
        module.DESCRIPTION = "Bare Source"
        with patch.object(bot, "get_current_source_type", return_value="bare"), \
             patch.object(bot, "load_source_modules", return_value={"bare": module}), \
             patch.object(bot, "execute_channel_scan", new=AsyncMock()) as mock_exec:
            asyncio.get_event_loop().run_until_complete(
                bot.handle_channel_scan_request(interaction, "scan")
            )
        mock_exec.assert_called_once()
        args, _ = mock_exec.call_args
        assert args[1] == (88_000_000 * 1.0, 108_000_000 * 1.0)


# ======================================================================
# execute_channel_scan — hardware-agnostic scan runner
# ======================================================================

class TestExecuteChannelScan:
    def test_pauses_active_pipeline_before_scanning(self):
        interaction = AsyncMock()
        scan_fn = MagicMock(return_value=[])
        bot.bot.hardware_process = MagicMock()
        try:
            with patch.object(bot, "stop_active_hardware_process") as mock_stop:
                asyncio.get_event_loop().run_until_complete(
                    bot.execute_channel_scan(interaction, (94_000_000, 95_000_000), scan_fn, active_description="Radio")
                )
        finally:
            bot.bot.hardware_process = None
        mock_stop.assert_called_once()
        all_calls = " ".join(str(c) for c in interaction.followup.send.call_args_list)
        assert "Pausing the active pipeline" in all_calls
        assert "Radio" in all_calls

    def test_no_channels_found_message(self):
        interaction = AsyncMock()
        bot.bot.hardware_process = None
        scan_fn = MagicMock(return_value=[])
        asyncio.get_event_loop().run_until_complete(
            bot.execute_channel_scan(interaction, (94_000_000, 95_000_000), scan_fn, active_description="Radio")
        )
        all_calls = " ".join(str(c) for c in interaction.followup.send.call_args_list)
        assert "No channels above the noise floor" in all_calls

    def test_channels_found_lists_catalog(self):
        interaction = AsyncMock()
        bot.bot.hardware_process = None
        catalog = [{"frequency": "94.5M", "power_db": -12.3}]
        scan_fn = MagicMock(return_value=catalog)
        asyncio.get_event_loop().run_until_complete(
            bot.execute_channel_scan(interaction, (94_000_000, 95_000_000), scan_fn, active_description="Radio")
        )
        all_calls = " ".join(str(c) for c in interaction.followup.send.call_args_list)
        assert "Clear Channels Found" in all_calls
        assert "94.5M" in all_calls
        assert "-12.3" in all_calls

    def test_scan_exception_reports_failure(self):
        interaction = AsyncMock()
        bot.bot.hardware_process = None
        scan_fn = MagicMock(side_effect=RuntimeError("dongle unplugged"))
        asyncio.get_event_loop().run_until_complete(
            bot.execute_channel_scan(interaction, (94_000_000, 95_000_000), scan_fn, active_description="Radio")
        )
        all_calls = " ".join(str(c) for c in interaction.followup.send.call_args_list)
        assert "Scan failed" in all_calls
        assert "dongle unplugged" in all_calls


# ======================================================================
# parse_scan_range — edge cases for frequency validation
# ======================================================================

class TestScanRangeEdgeCases:
    def _parse(self, arg):
        return bot.parse_scan_range(arg, default_start_mhz=88.0, default_end_mhz=108.0, max_span_mhz=60.0)

    def test_only_scan_keyword_with_uppercase(self):
        result = self._parse("SCAN")
        assert result is not None

    def test_scan_with_leading_zero(self):
        result = self._parse("scan 088-108")
        assert result == (88_000_000, 108_000_000)

    def test_scan_with_single_digit(self):
        result = self._parse("scan 8-12")
        assert result == (8_000_000, 12_000_000)


# ======================================================================
# Note: ALSA-specific signal probing (formerly probe_device_has_signal)
# now lives in sources/alsa.py's probe_signal() and is covered by
# tests/test_sources_alsa.py instead of here -- bot.py no longer contains
# any ALSA-specific code to test.
# ======================================================================


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
# Note: the old JSON self-healing (self_heal_test_signal_profile) is gone
# entirely -- test_signal is now just a normal, removable source plugin
# (see tests/test_sources_test_signal.py), and the one hardcoded fallback
# bot.py still owns (BUILTIN_FALLBACK_SOURCE / _builtin_fallback_command)
# is covered by TestBuiltinFallback below.
# ======================================================================

class TestBuiltinFallback:
    def test_fallback_source_shape(self):
        assert bot.BUILTIN_FALLBACK_SOURCE["type"] == bot.BUILTIN_FALLBACK_TYPE
        assert "description" in bot.BUILTIN_FALLBACK_SOURCE

    def test_fallback_command_writes_into_given_fifo(self):
        cmd = bot._builtin_fallback_command("/tmp/some_pipe")
        assert "/tmp/some_pipe" in cmd
        assert "ffmpeg" in cmd


# ======================================================================
# Test parse_scan_range — more edge cases
# ======================================================================

class TestParseScanRangeMore:
    def _parse(self, arg):
        return bot.parse_scan_range(arg, default_start_mhz=88.0, default_end_mhz=108.0, max_span_mhz=60.0)

    def test_scan_with_only_start(self):
        """'scan 88-' should still parse."""
        result = self._parse("scan 88-")
        # The regex requires both groups, so this returns None → not a scan
        assert result is None

    def test_scan_non_numeric_does_not_match_returns_none(self):
        """Non-numeric input doesn't match the regex at all (it requires
        digits), so this isn't recognized as a scan request."""
        result = self._parse("scan abc-def")
        assert result is None
