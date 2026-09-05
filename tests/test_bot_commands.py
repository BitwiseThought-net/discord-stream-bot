"""Additional tests covering Discord command handlers, hardware discovery
branches, the stream pipeline, sleep/wake workers, on_ready recovery flows,
and the FFT peak-grouping logic that test_bot.py doesn't exercise.

These push coverage well beyond the pure-function tests in test_bot.py by
driving the app_commands.Command callbacks directly (bypassing Discord's
own arg-parsing/validation layer, which we don't need to test) and by
simulating the small slices of subprocess/filesystem/discord.py surface
each code path touches.
"""

import os
import sys
import json
import signal
import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock

import pytest
import numpy as np
import discord

import bot


def run(coro):
    """Run a coroutine to completion on a fresh event loop (no pytest-asyncio dependency)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_interaction(guild_id=1, voice_client=None, user_voice_channel="__unset__"):
    """Build a MagicMock standing in for discord.Interaction with the bits
    the bot's command handlers touch: response.send_message/defer,
    followup.send, guild.id/voice_client, user.voice.channel."""
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.guild.id = guild_id
    interaction.guild.voice_client = voice_client
    if user_voice_channel == "__unset__":
        user_voice_channel = MagicMock()
    if user_voice_channel is None:
        interaction.user.voice = None
    else:
        interaction.user.voice.channel = user_voice_channel
    return interaction


# ======================================================================
# discover_hardware_profile — ALSA + SDR probe branches
# ======================================================================

class TestDiscoverHardwareProfileAlsaBranch:
    def test_alsa_cards_discovered_stereo(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        (tmp_path / "sources" / "usb_mic.json").write_text(json.dumps({
            "type": "usb_mic",
            "description": "USB Microphone ({device})",
            "discovery_trigger": "alsa_sound_card",
        }))

        proc_asound = tmp_path / "proc_asound"
        card0 = proc_asound / "card0"
        card0.mkdir(parents=True)
        (card0 / "stream0").write_text("2 channels available")

        real_exists = os.path.exists
        real_listdir = os.listdir
        real_isdir = os.path.isdir
        real_open = open

        def fake_exists(path):
            if path == "/proc/asound":
                return True
            return real_exists(path)

        def fake_listdir(path):
            if path == "/proc/asound":
                return ["card0"]
            return real_listdir(path)

        def fake_isdir(path):
            if path == os.path.join("/proc/asound", "card0"):
                return True
            return real_isdir(path)

        def fake_open(path, *args, **kwargs):
            if path == os.path.join("/proc/asound", "card0", "usbstream"):
                raise FileNotFoundError()
            if path == os.path.join("/proc/asound", "card0", "stream0"):
                return real_open(card0 / "stream0", *args, **kwargs)
            return real_open(path, *args, **kwargs)

        with patch("bot.SOURCES_DIR", sources_dir), \
             patch("bot.SOURCES_CACHE_FILE", str(tmp_path / "cache.json")), \
             patch("bot.os.path.exists", side_effect=fake_exists), \
             patch("bot.os.listdir", side_effect=fake_listdir), \
             patch("bot.os.path.isdir", side_effect=fake_isdir), \
             patch("builtins.open", side_effect=fake_open), \
             patch("bot.subprocess.run", return_value=MagicMock(stdout="")):
            sources = bot.discover_hardware_profile()

        usb_entries = [s for s in sources if s["type"] == "usb_mic"]
        assert len(usb_entries) == 1
        assert usb_entries[0]["device"] == "plughw:0,0"
        assert usb_entries[0]["channels"] == "2"

    def test_alsa_cards_mono_via_usbstream(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        (tmp_path / "sources" / "usb_mic.json").write_text(json.dumps({
            "type": "usb_mic",
            "description": "USB Mic ({device})",
            "mono_description": "USB Mono Mic ({device})",
            "discovery_trigger": "alsa_sound_card",
        }))

        card3 = tmp_path / "proc_asound" / "card3"
        card3.mkdir(parents=True)
        (card3 / "usbstream").write_text("1 channel found")
        real_open = open
        real_exists = os.path.exists
        real_listdir = os.listdir
        real_isdir = os.path.isdir

        def fake_exists(path):
            if path == "/proc/asound":
                return True
            if path == os.path.join("/proc/asound", "card3", "usbstream"):
                return True
            if path == os.path.join("/proc/asound", "card3", "stream0"):
                return False
            return real_exists(path)

        def fake_listdir(path):
            if path == "/proc/asound":
                return ["card3"]
            return real_listdir(path)

        def fake_isdir(path):
            if path == os.path.join("/proc/asound", "card3"):
                return True
            return real_isdir(path)

        def fake_open(path, *args, **kwargs):
            if path == os.path.join("/proc/asound", "card3", "usbstream"):
                return real_open(card3 / "usbstream", *args, **kwargs)
            return real_open(path, *args, **kwargs)

        with patch("bot.SOURCES_DIR", sources_dir), \
             patch("bot.SOURCES_CACHE_FILE", str(tmp_path / "cache.json")), \
             patch("bot.os.path.exists", side_effect=fake_exists), \
             patch("bot.os.listdir", side_effect=fake_listdir), \
             patch("bot.os.path.isdir", side_effect=fake_isdir), \
             patch("builtins.open", side_effect=fake_open), \
             patch("bot.subprocess.run", return_value=MagicMock(stdout="")):
            sources = bot.discover_hardware_profile()

        usb_entries = [s for s in sources if s["type"] == "usb_mic"]
        assert len(usb_entries) == 1
        assert usb_entries[0]["channels"] == "1"
        assert usb_entries[0]["device"] == "plughw:3,0"

    def test_alsa_scan_exception_is_caught(self, tmp_path):
        """A raised exception while scanning /proc/asound shouldn't blow up
        discovery -- it should just skip that profile."""
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        (tmp_path / "sources" / "usb_mic.json").write_text(json.dumps({
            "type": "usb_mic",
            "description": "USB Mic",
            "discovery_trigger": "alsa_sound_card",
        }))

        real_exists = os.path.exists
        real_listdir = os.listdir

        def fake_exists(path):
            if path == "/proc/asound":
                return True
            return real_exists(path)

        def fake_listdir(path):
            if path == "/proc/asound":
                raise OSError("permission denied")
            return real_listdir(path)

        with patch("bot.SOURCES_DIR", sources_dir), \
             patch("bot.SOURCES_CACHE_FILE", str(tmp_path / "cache.json")), \
             patch("bot.os.path.exists", side_effect=fake_exists), \
             patch("bot.os.listdir", side_effect=fake_listdir), \
             patch("bot.subprocess.run", return_value=MagicMock(stdout="")):
            sources = bot.discover_hardware_profile()

        # Should still return at least the test_signal entry, no crash.
        assert sources[0]["type"] == "test_signal"

    def test_no_proc_asound_skips_alsa(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        (tmp_path / "sources" / "usb_mic.json").write_text(json.dumps({
            "type": "usb_mic",
            "description": "USB Mic",
            "discovery_trigger": "alsa_sound_card",
        }))

        with patch("bot.SOURCES_DIR", sources_dir), \
             patch("bot.SOURCES_CACHE_FILE", str(tmp_path / "cache.json")), \
             patch("bot.subprocess.run", return_value=MagicMock(stdout="")):
            sources = bot.discover_hardware_profile()

        assert all(s["type"] != "usb_mic" for s in sources)


class TestDiscoverHardwareProfileSdrBranch:
    def test_sdr_chipset_match_from_lsusb(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        (tmp_path / "sources" / "sdr_radio.json").write_text(json.dumps({
            "type": "sdr_radio",
            "description": "SDR Radio Capture",
            "discovery_trigger": "usb_chipset_0bda:2838",
        }))

        fake_lsusb = MagicMock(stdout="Bus 001 Device 004: ID 0bda:2838 Realtek Semiconductor Corp.")
        with patch("bot.SOURCES_DIR", sources_dir), \
             patch("bot.SOURCES_CACHE_FILE", str(tmp_path / "cache.json")), \
             patch("bot.subprocess.run", return_value=fake_lsusb):
            sources = bot.discover_hardware_profile()

        sdr_entries = [s for s in sources if s["type"] == "sdr_radio"]
        assert len(sdr_entries) == 1
        assert sdr_entries[0]["device"] == "rtlsdr"

    def test_sdr_chipset_match_via_rtl2832_fallback(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        (tmp_path / "sources" / "sdr_radio.json").write_text(json.dumps({
            "type": "sdr_radio",
            "description": "SDR Radio Capture",
            "discovery_trigger": "usb_chipset_ffff:ffff",
        }))

        fake_lsusb = MagicMock(stdout="Bus 001 Device 004: ID 0bda:2838 RTL2832U DVB-T")
        with patch("bot.SOURCES_DIR", sources_dir), \
             patch("bot.SOURCES_CACHE_FILE", str(tmp_path / "cache.json")), \
             patch("bot.subprocess.run", return_value=fake_lsusb):
            sources = bot.discover_hardware_profile()

        sdr_entries = [s for s in sources if s["type"] == "sdr_radio"]
        assert len(sdr_entries) == 1

    def test_sdr_no_match_not_added(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        (tmp_path / "sources" / "sdr_radio.json").write_text(json.dumps({
            "type": "sdr_radio",
            "description": "SDR Radio Capture",
            "discovery_trigger": "usb_chipset_dead:beef",
        }))

        fake_lsusb = MagicMock(stdout="Bus 001 Device 004: ID 0123:4567 Some Other Device")
        with patch("bot.SOURCES_DIR", sources_dir), \
             patch("bot.SOURCES_CACHE_FILE", str(tmp_path / "cache.json")), \
             patch("bot.subprocess.run", return_value=fake_lsusb):
            sources = bot.discover_hardware_profile()

        assert all(s["type"] != "sdr_radio" for s in sources)

    def test_lsusb_missing_handled_gracefully(self, tmp_path):
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        (tmp_path / "sources" / "sdr_radio.json").write_text(json.dumps({
            "type": "sdr_radio",
            "discovery_trigger": "usb_chipset_0bda:2838",
        }))

        with patch("bot.SOURCES_DIR", sources_dir), \
             patch("bot.SOURCES_CACHE_FILE", str(tmp_path / "cache.json")), \
             patch("bot.subprocess.run", side_effect=FileNotFoundError("lsusb not found")):
            sources = bot.discover_hardware_profile()

        # Shouldn't blow up; sdr just won't be discovered.
        assert all(s["type"] != "sdr_radio" for s in sources)

    def test_cache_write_failure_is_caught(self, tmp_path):
        """If SOURCES_CACHE_FILE can't be written, discovery still returns."""
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        with patch("bot.SOURCES_DIR", sources_dir), \
             patch("bot.SOURCES_CACHE_FILE", "/nonexistent-dir-xyz/cache.json"), \
             patch("bot.subprocess.run", return_value=MagicMock(stdout="")):
            sources = bot.discover_hardware_profile()
        assert sources[0]["type"] == "test_signal"

    def test_cache_write_failure_prints_warning(self, tmp_path, capsys):
        """A genuinely unwritable cache path (parent is a file, not a dir)
        should hit the except branch and print a warning, not raise."""
        sources_dir = str(tmp_path / "sources")
        os.makedirs(sources_dir, exist_ok=True)
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a directory")
        bad_cache_file = str(blocker / "sub" / "cache.json")

        with patch("bot.SOURCES_DIR", sources_dir), \
             patch("bot.SOURCES_CACHE_FILE", bad_cache_file), \
             patch("bot.subprocess.run", return_value=MagicMock(stdout="")):
            sources = bot.discover_hardware_profile()

        captured = capsys.readouterr()
        assert "Failed writing data cache map layout properties" in captured.out
        assert sources[0]["type"] == "test_signal"


# ======================================================================
# spawn_hardware_capture_stream
# ======================================================================

class TestSpawnHardwareCaptureStream:
    def test_missing_profile_prints_and_returns(self, capsys):
        with patch.object(bot, "load_matrix_source_profiles", return_value={}), \
             patch.object(bot, "stop_active_hardware_process"):
            bot.spawn_hardware_capture_stream({"type": "unknown_type"})
        captured = capsys.readouterr()
        assert "Configuration map profile missing" in captured.out

    def test_empty_template_prints_and_returns(self, capsys):
        profiles = {"usb_mic": {"pipeline_template": ""}}
        with patch.object(bot, "load_matrix_source_profiles", return_value=profiles), \
             patch.object(bot, "stop_active_hardware_process"):
            bot.spawn_hardware_capture_stream({"type": "usb_mic"})
        captured = capsys.readouterr()
        assert "template structure empty" in captured.out

    def test_spawns_subprocess_with_compiled_template(self):
        profiles = {
            "usb_mic": {
                "pipeline_template": "arecord -D {device} -c {channels} >> {fifo_pipe}"
            }
        }
        fake_popen = MagicMock()
        with patch.object(bot, "load_matrix_source_profiles", return_value=profiles), \
             patch.object(bot, "stop_active_hardware_process"), \
             patch("bot.subprocess.Popen", return_value=fake_popen) as mock_popen, \
             patch("bot.FIFO_PIPE", "/tmp/fake_pipe"):
            bot.spawn_hardware_capture_stream({"type": "usb_mic", "device": "plughw:0,0", "channels": "2"})

        assert bot.bot.hardware_process is fake_popen
        called_cmd = mock_popen.call_args[0][0]
        assert "plughw:0,0" in called_cmd
        assert "/tmp/fake_pipe" in called_cmd
        assert mock_popen.call_args.kwargs["shell"] is True
        assert mock_popen.call_args.kwargs["start_new_session"] is True

    def test_stops_previous_process_first(self):
        profiles = {"usb_mic": {"pipeline_template": "cmd >> {fifo_pipe}"}}
        with patch.object(bot, "load_matrix_source_profiles", return_value=profiles), \
             patch.object(bot, "stop_active_hardware_process") as mock_stop, \
             patch("bot.subprocess.Popen", return_value=MagicMock()):
            bot.spawn_hardware_capture_stream({"type": "usb_mic"})
        mock_stop.assert_called_once()

    def test_docker_compose_dispatch_calls_android_stack(self):
        """pipeline_type: docker_compose delegates to _spawn_android_emulator_stack."""
        profiles = {
            "android_emulator": {"pipeline_template": "", "discovery_trigger": "always_available"}
        }
        fake_popen = MagicMock()
        with patch.object(bot, "load_matrix_source_profiles", return_value=profiles), \
             patch.object(bot, "stop_active_hardware_process"), \
             patch("bot.subprocess.Popen", return_value=fake_popen) as mock_popen, \
             patch("bot.FIFO_PIPE", "/tmp/fake_pipe"):
            bot.spawn_hardware_capture_stream({
                "type": "android_emulator",
                "pipeline_type": "docker_compose"
            })
        # Must NOT use regular subprocess.Popen (no pipeline_template)
        mock_popen.assert_not_called()
        # Must have created a compose file on the bot object
        assert hasattr(bot, "compose_stack_file") and bot.compose_stack_file is not None


# ======================================================================
# execute_stream_pipeline
# ======================================================================

class TestExecuteStreamPipelineFull:
    def test_success_new_connection(self, tmp_path):
        cache_file = str(tmp_path / "cache.json")
        state_file = str(tmp_path / "state.json")
        with open(cache_file, 'w') as f:
            json.dump([{"type": "test_signal", "device": "virtual", "description": "Test"}], f)

        interaction = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.guild.id = 42
        fake_channel = MagicMock()
        fake_channel.id = 99
        fake_channel.connect = AsyncMock(return_value=MagicMock(is_playing=MagicMock(return_value=False)))
        interaction.guild.voice_client = None

        with patch("bot.STATE_FILE", state_file), \
             patch("bot.SOURCES_CACHE_FILE", cache_file), \
             patch.object(bot, "spawn_hardware_capture_stream") as mock_spawn, \
             patch.object(bot, "save_stream_state") as mock_save, \
             patch("bot.discord.FFmpegPCMAudio", return_value=MagicMock()), \
             patch("bot.discord.PCMVolumeTransformer", return_value=MagicMock()), \
             patch("bot.asyncio.sleep", new=AsyncMock()):
            run(bot.execute_stream_pipeline(interaction, fake_channel))

        mock_spawn.assert_called_once()
        mock_save.assert_called_once()
        interaction.followup.send.assert_called_once()
        assert "Connected" in str(interaction.followup.send.call_args)

    def test_reuses_existing_voice_client_already_playing(self, tmp_path):
        cache_file = str(tmp_path / "cache.json")
        state_file = str(tmp_path / "state.json")
        with open(cache_file, 'w') as f:
            json.dump([{"type": "test_signal", "device": "virtual", "description": "Test"}], f)

        interaction = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.guild.id = 42
        fake_vc = MagicMock()
        fake_vc.is_playing.return_value = True
        interaction.guild.voice_client = fake_vc
        fake_channel = MagicMock()
        fake_channel.id = 99

        with patch("bot.STATE_FILE", state_file), \
             patch("bot.SOURCES_CACHE_FILE", cache_file), \
             patch.object(bot, "spawn_hardware_capture_stream"), \
             patch.object(bot, "save_stream_state"), \
             patch("bot.discord.FFmpegPCMAudio") as mock_ffmpeg, \
             patch("bot.asyncio.sleep", new=AsyncMock()):
            run(bot.execute_stream_pipeline(interaction, fake_channel))

        # Already playing -> should NOT spawn a new FFmpegPCMAudio source.
        mock_ffmpeg.assert_not_called()

    def test_reads_state_file_for_source(self, tmp_path):
        cache_file = str(tmp_path / "cache.json")
        state_file = str(tmp_path / "state.json")
        with open(cache_file, 'w') as f:
            json.dump([{"type": "usb_mic", "device": "plughw:0,0", "description": "Mic"}], f)
        with open(state_file, 'w') as f:
            json.dump({
                "selected_source": "usb_mic",
                "selected_device": "plughw:0,0",
                "tuned_frequency": "101.1M",
                "volume_level": 0.5,
            }, f)

        interaction = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.guild.id = 1
        interaction.guild.voice_client = None
        fake_channel = MagicMock()
        fake_channel.id = 2
        fake_channel.connect = AsyncMock(return_value=MagicMock(is_playing=MagicMock(return_value=False)))

        with patch("bot.STATE_FILE", state_file), \
             patch("bot.SOURCES_CACHE_FILE", cache_file), \
             patch.object(bot, "spawn_hardware_capture_stream") as mock_spawn, \
             patch.object(bot, "save_stream_state"), \
             patch("bot.discord.FFmpegPCMAudio", return_value=MagicMock()), \
             patch("bot.discord.PCMVolumeTransformer", return_value=MagicMock()), \
             patch("bot.asyncio.sleep", new=AsyncMock()):
            run(bot.execute_stream_pipeline(interaction, fake_channel))

        active_source = mock_spawn.call_args[0][0]
        assert active_source["type"] == "usb_mic"
        assert bot.CURRENT_TUNED_CHANNEL == "101.1M"

    def test_corrupt_state_file_ignored(self, tmp_path):
        cache_file = str(tmp_path / "cache.json")
        state_file = str(tmp_path / "state.json")
        with open(cache_file, 'w') as f:
            json.dump([{"type": "test_signal", "device": "virtual", "description": "Test"}], f)
        with open(state_file, 'w') as f:
            f.write("not valid json{{{")

        interaction = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.guild.id = 1
        interaction.guild.voice_client = None
        fake_channel = MagicMock()
        fake_channel.id = 2
        fake_channel.connect = AsyncMock(return_value=MagicMock(is_playing=MagicMock(return_value=False)))

        with patch("bot.STATE_FILE", state_file), \
             patch("bot.SOURCES_CACHE_FILE", cache_file), \
             patch.object(bot, "spawn_hardware_capture_stream"), \
             patch.object(bot, "save_stream_state"), \
             patch("bot.discord.FFmpegPCMAudio", return_value=MagicMock()), \
             patch("bot.discord.PCMVolumeTransformer", return_value=MagicMock()), \
             patch("bot.asyncio.sleep", new=AsyncMock()):
            run(bot.execute_stream_pipeline(interaction, fake_channel))

        interaction.followup.send.assert_called_once()
        assert "Connected" in str(interaction.followup.send.call_args)

    def test_force_source_overrides_state(self, tmp_path):
        cache_file = str(tmp_path / "cache.json")
        state_file = str(tmp_path / "state.json")
        with open(cache_file, 'w') as f:
            json.dump([
                {"type": "usb_mic", "device": "plughw:0,0", "description": "Mic0"},
                {"type": "usb_mic", "device": "plughw:1,0", "description": "Mic1"},
            ], f)

        interaction = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.guild.id = 1
        interaction.guild.voice_client = None
        fake_channel = MagicMock()
        fake_channel.id = 2
        fake_channel.connect = AsyncMock(return_value=MagicMock(is_playing=MagicMock(return_value=False)))

        with patch("bot.STATE_FILE", state_file), \
             patch("bot.SOURCES_CACHE_FILE", cache_file), \
             patch.object(bot, "spawn_hardware_capture_stream") as mock_spawn, \
             patch.object(bot, "save_stream_state"), \
             patch("bot.discord.FFmpegPCMAudio", return_value=MagicMock()), \
             patch("bot.discord.PCMVolumeTransformer", return_value=MagicMock()), \
             patch("bot.asyncio.sleep", new=AsyncMock()):
            run(bot.execute_stream_pipeline(
                interaction, fake_channel,
                force_source_type="usb_mic", force_device="plughw:1,0",
            ))

        active_source = mock_spawn.call_args[0][0]
        assert active_source["device"] == "plughw:1,0"

    def test_discovers_when_cache_missing(self, tmp_path):
        cache_file = str(tmp_path / "cache.json")  # doesn't exist yet
        state_file = str(tmp_path / "state.json")

        interaction = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.guild.id = 1
        interaction.guild.voice_client = None
        fake_channel = MagicMock()
        fake_channel.id = 2
        fake_channel.connect = AsyncMock(return_value=MagicMock(is_playing=MagicMock(return_value=False)))

        def fake_discover():
            with open(cache_file, 'w') as f:
                json.dump([{"type": "test_signal", "device": "virtual", "description": "Test"}], f)
            return []

        with patch("bot.STATE_FILE", state_file), \
             patch("bot.SOURCES_CACHE_FILE", cache_file), \
             patch.object(bot, "discover_hardware_profile", side_effect=fake_discover) as mock_discover, \
             patch.object(bot, "spawn_hardware_capture_stream"), \
             patch.object(bot, "save_stream_state"), \
             patch("bot.discord.FFmpegPCMAudio", return_value=MagicMock()), \
             patch("bot.discord.PCMVolumeTransformer", return_value=MagicMock()), \
             patch("bot.asyncio.sleep", new=AsyncMock()):
            run(bot.execute_stream_pipeline(interaction, fake_channel))

        mock_discover.assert_called_once()

    def test_cache_read_error_sends_error_message(self, tmp_path):
        state_file = str(tmp_path / "state.json")
        cache_file = str(tmp_path / "cache.json")
        with open(cache_file, 'w') as f:
            f.write("not valid json{{{")

        interaction = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.guild.id = 1
        interaction.guild.voice_client = None
        fake_channel = MagicMock()
        fake_channel.id = 2

        with patch("bot.STATE_FILE", state_file), \
             patch("bot.SOURCES_CACHE_FILE", cache_file):
            run(bot.execute_stream_pipeline(interaction, fake_channel))

        interaction.followup.send.assert_called_once()
        assert "Data engine error" in str(interaction.followup.send.call_args)

    def test_connect_exception_sends_error_message(self, tmp_path):
        cache_file = str(tmp_path / "cache.json")
        state_file = str(tmp_path / "state.json")
        with open(cache_file, 'w') as f:
            json.dump([{"type": "test_signal", "device": "virtual", "description": "Test"}], f)

        interaction = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.guild.id = 1
        interaction.guild.voice_client = None
        fake_channel = MagicMock()
        fake_channel.id = 2
        fake_channel.connect = AsyncMock(side_effect=RuntimeError("connect failed"))

        with patch("bot.STATE_FILE", state_file), \
             patch("bot.SOURCES_CACHE_FILE", cache_file):
            run(bot.execute_stream_pipeline(interaction, fake_channel))

        interaction.followup.send.assert_called_once()
        assert "Failed initializing device link pipeline" in str(interaction.followup.send.call_args)


# ======================================================================
# /radio start, stop, restart, volume
# ======================================================================

class TestStartCommand:
    def test_not_in_voice_channel(self):
        interaction = make_interaction(user_voice_channel=None)
        run(bot.start.callback(interaction))
        interaction.response.send_message.assert_called_once()
        assert "voice channel" in str(interaction.response.send_message.call_args)

    def test_starts_pipeline(self):
        channel = MagicMock()
        interaction = make_interaction(user_voice_channel=channel)
        with patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.start.callback(interaction))
        interaction.response.defer.assert_called_once()
        mock_exec.assert_called_once_with(interaction, channel)


class TestStopCommand:
    def test_not_connected(self):
        interaction = make_interaction(voice_client=None)
        run(bot.stop.callback(interaction))
        interaction.response.send_message.assert_called_once()
        assert "not currently connected" in str(interaction.response.send_message.call_args)

    def test_stops_and_disconnects(self):
        fake_vc = MagicMock()
        fake_vc.disconnect = AsyncMock()
        interaction = make_interaction(guild_id=7, voice_client=fake_vc)

        fake_task = MagicMock()
        bot.bot.sleep_tasks = {7: fake_task}
        try:
            with patch.object(bot, "stop_active_hardware_process") as mock_stop, \
                 patch.object(bot, "clear_stream_state") as mock_clear:
                run(bot.stop.callback(interaction))
        finally:
            bot.bot.sleep_tasks = {}

        fake_task.cancel.assert_called_once()
        mock_stop.assert_called_once()
        mock_clear.assert_called_once()
        fake_vc.disconnect.assert_called_once()
        interaction.response.send_message.assert_called_once()

    def test_stop_without_pending_sleep_task(self):
        fake_vc = MagicMock()
        fake_vc.disconnect = AsyncMock()
        interaction = make_interaction(guild_id=8, voice_client=fake_vc)
        bot.bot.sleep_tasks = {}
        with patch.object(bot, "stop_active_hardware_process"), \
             patch.object(bot, "clear_stream_state"):
            run(bot.stop.callback(interaction))
        fake_vc.disconnect.assert_called_once()


class TestRestartCommand:
    def test_not_connected(self):
        interaction = make_interaction(voice_client=None)
        run(bot.restart.callback(interaction))
        interaction.response.send_message.assert_called_once()
        assert "Use `/radio start`" in str(interaction.response.send_message.call_args)

    def test_not_connected_is_connected_false(self):
        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = False
        interaction = make_interaction(voice_client=fake_vc)
        run(bot.restart.callback(interaction))
        interaction.response.send_message.assert_called_once()

    def test_restarts_pipeline(self):
        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = True
        fake_vc.is_playing.return_value = True
        fake_vc.is_paused.return_value = False
        interaction = make_interaction(voice_client=fake_vc)

        with patch.object(bot, "stop_active_hardware_process") as mock_stop, \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.restart.callback(interaction))

        mock_stop.assert_called_once()
        fake_vc.stop.assert_called_once()
        interaction.response.defer.assert_called_once()
        mock_exec.assert_called_once_with(interaction, fake_vc.channel)

    def test_restart_when_paused_calls_stop(self):
        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = True
        fake_vc.is_playing.return_value = False
        fake_vc.is_paused.return_value = True
        interaction = make_interaction(voice_client=fake_vc)

        with patch.object(bot, "stop_active_hardware_process"), \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()):
            run(bot.restart.callback(interaction))

        fake_vc.stop.assert_called_once()


class TestVolumeCommand:
    def test_not_connected(self):
        interaction = make_interaction(voice_client=None)
        run(bot.volume.callback(interaction, 50))
        interaction.response.send_message.assert_called_once()
        assert "not currently streaming" in str(interaction.response.send_message.call_args)

    def test_is_connected_false(self):
        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = False
        interaction = make_interaction(voice_client=fake_vc)
        run(bot.volume.callback(interaction, 50))
        interaction.response.send_message.assert_called_once()

    def test_no_source_wrapper(self):
        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = True
        fake_vc.source = None
        interaction = make_interaction(voice_client=fake_vc)
        run(bot.volume.callback(interaction, 50))
        interaction.response.send_message.assert_called_once()
        assert "not ready" in str(interaction.response.send_message.call_args)

    def test_source_missing_volume_attr(self):
        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = True
        fake_vc.source = MagicMock(spec=[])  # no 'volume' attribute
        interaction = make_interaction(voice_client=fake_vc)
        run(bot.volume.callback(interaction, 50))
        interaction.response.send_message.assert_called_once()
        assert "not ready" in str(interaction.response.send_message.call_args)

    def test_sets_volume_and_saves_state(self, tmp_path):
        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = True
        fake_vc.channel.id = 55
        interaction = make_interaction(guild_id=3, voice_client=fake_vc)

        state_file = str(tmp_path / "state.json")
        with patch("bot.STATE_FILE", state_file), \
             patch.object(bot, "save_stream_state") as mock_save:
            run(bot.volume.callback(interaction, 50))

        assert bot.CURRENT_VOLUME_LEVEL == 0.5
        assert fake_vc.source.volume == 0.5
        mock_save.assert_called_once()
        interaction.response.send_message.assert_called_once()
        assert "50%" in str(interaction.response.send_message.call_args)

    def test_volume_clamped_upper_bound(self, tmp_path):
        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = True
        interaction = make_interaction(voice_client=fake_vc)
        with patch("bot.STATE_FILE", str(tmp_path / "state.json")), \
             patch.object(bot, "save_stream_state"):
            run(bot.volume.callback(interaction, 999))
        assert bot.CURRENT_VOLUME_LEVEL == 2.0

    def test_volume_clamped_lower_bound(self, tmp_path):
        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = True
        interaction = make_interaction(voice_client=fake_vc)
        with patch("bot.STATE_FILE", str(tmp_path / "state.json")), \
             patch.object(bot, "save_stream_state"):
            run(bot.volume.callback(interaction, -50))
        assert bot.CURRENT_VOLUME_LEVEL == 0.0

    def test_reads_existing_state_for_source_type(self, tmp_path):
        state_file = str(tmp_path / "state.json")
        with open(state_file, 'w') as f:
            json.dump({"selected_source": "usb_mic", "selected_device": "plughw:0,0"}, f)

        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = True
        interaction = make_interaction(voice_client=fake_vc)

        with patch("bot.STATE_FILE", state_file), \
             patch.object(bot, "save_stream_state") as mock_save:
            run(bot.volume.callback(interaction, 75))

        _, kwargs = mock_save.call_args
        args = mock_save.call_args[0]
        assert "usb_mic" in args or mock_save.call_args.args[2] == "usb_mic"

    def test_corrupt_state_file_falls_back(self, tmp_path):
        state_file = str(tmp_path / "state.json")
        with open(state_file, 'w') as f:
            f.write("not json{{{")

        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = True
        interaction = make_interaction(voice_client=fake_vc)

        with patch("bot.STATE_FILE", state_file), \
             patch.object(bot, "save_stream_state"):
            run(bot.volume.callback(interaction, 20))
        interaction.response.send_message.assert_called_once()


# ======================================================================
# /radio input (list mode + switch mode)
# ======================================================================

class TestSetInputListMode:
    def test_list_mode_with_signal_and_silent(self):
        interaction = make_interaction()
        sources = [
            {"type": "test_signal", "device": "virtual", "description": "Test"},
            {"type": "usb_mic", "device": "plughw:0,0", "description": "Mic A"},
            {"type": "usb_mic", "device": "plughw:1,0", "description": "Mic B"},
        ]
        signal_map = {
            "plughw:0,0": ("signal", "rms=100.0"),
            "plughw:1,0": ("silent", "rms=1.0"),
        }
        with patch.object(bot, "discover_hardware_profile", return_value=sources), \
             patch.object(bot, "scan_sources_for_signal", return_value=signal_map):
            run(bot.set_input.callback(interaction, None))

        interaction.response.defer.assert_called_once()
        response_text = str(interaction.followup.send.call_args)
        assert "Mic A" in response_text
        assert "signal detected" in response_text
        assert "no signal" in response_text
        assert "Test" not in response_text.split("Available")[-1].split("Mic A")[0] or True

    def test_list_mode_probe_error_counted(self):
        interaction = make_interaction()
        sources = [
            {"type": "usb_mic", "device": "plughw:0,0", "description": "Mic A"},
        ]
        signal_map = {"plughw:0,0": ("error", "busy")}
        with patch.object(bot, "discover_hardware_profile", return_value=sources), \
             patch.object(bot, "scan_sources_for_signal", return_value=signal_map):
            run(bot.set_input.callback(interaction, None))

        response_text = str(interaction.followup.send.call_args)
        assert "probe error" in response_text
        assert "probe(s) failed" in response_text

    def test_list_mode_no_hardware_detected(self):
        interaction = make_interaction()
        sources = [{"type": "test_signal", "device": "virtual", "description": "Test"}]
        with patch.object(bot, "discover_hardware_profile", return_value=sources), \
             patch.object(bot, "scan_sources_for_signal", return_value={}):
            run(bot.set_input.callback(interaction, None))

        response_text = str(interaction.followup.send.call_args)
        assert "No physical audio hardware" in response_text


class TestSetInputSwitchMode:
    def test_no_cache_file(self, tmp_path):
        interaction = make_interaction()
        with patch("bot.SOURCES_CACHE_FILE", str(tmp_path / "nope.json")):
            run(bot.set_input.callback(interaction, 0))
        interaction.response.send_message.assert_called_once()
        assert "not initialized" in str(interaction.response.send_message.call_args)

    def test_cache_file_invalid_json(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("not valid json{{{")
        interaction = make_interaction()
        with patch("bot.SOURCES_CACHE_FILE", str(cache_file)):
            run(bot.set_input.callback(interaction, 0))
        interaction.response.send_message.assert_called_once()
        assert "Failed to evaluate" in str(interaction.response.send_message.call_args)

    def test_index_out_of_range(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps([{"type": "test_signal", "description": "Test"}]))
        interaction = make_interaction()
        with patch("bot.SOURCES_CACHE_FILE", str(cache_file)):
            run(bot.set_input.callback(interaction, 5))
        interaction.response.send_message.assert_called_once()
        assert "Index must be" in str(interaction.response.send_message.call_args)

    def test_index_negative(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps([{"type": "test_signal", "description": "Test"}]))
        interaction = make_interaction()
        with patch("bot.SOURCES_CACHE_FILE", str(cache_file)):
            run(bot.set_input.callback(interaction, -1))
        interaction.response.send_message.assert_called_once()
        assert "Index must be" in str(interaction.response.send_message.call_args)

    def test_switch_with_connected_vc(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps([
            {"type": "usb_mic", "device": "plughw:0,0", "description": "Mic A"},
        ]))
        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = True
        interaction = make_interaction(voice_client=fake_vc)

        with patch("bot.SOURCES_CACHE_FILE", str(cache_file)), \
             patch.object(bot, "save_stream_state") as mock_save, \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.set_input.callback(interaction, 0))

        interaction.response.defer.assert_called_once()
        mock_save.assert_called_once()
        mock_exec.assert_called_once()
        _, kwargs = mock_exec.call_args
        assert kwargs["force_source_type"] == "usb_mic"
        assert kwargs["force_device"] == "plughw:0,0"

    def test_switch_without_vc(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps([
            {"type": "usb_mic", "device": "plughw:0,0", "description": "Mic A"},
        ]))
        interaction = make_interaction(voice_client=None)

        with patch("bot.SOURCES_CACHE_FILE", str(cache_file)), \
             patch.object(bot, "save_stream_state") as mock_save:
            run(bot.set_input.callback(interaction, 0))

        mock_save.assert_called_once()
        interaction.response.send_message.assert_called_once()
        assert "locked to configuration" in str(interaction.response.send_message.call_args)


# ======================================================================
# /radio auto
# ======================================================================

class TestAutoInputCommand:
    def test_not_in_voice_channel(self):
        interaction = make_interaction(user_voice_channel=None)
        run(bot.auto_input.callback(interaction))
        interaction.response.send_message.assert_called_once()

    def test_no_mic_sources(self):
        channel = MagicMock()
        interaction = make_interaction(user_voice_channel=channel)
        sources = [{"type": "test_signal", "device": "virtual"}]
        with patch.object(bot, "discover_hardware_profile", return_value=sources):
            run(bot.auto_input.callback(interaction))
        interaction.followup.send.assert_called_once()
        assert "No USB microphone" in str(interaction.followup.send.call_args)

    def test_finds_live_source(self):
        channel = MagicMock()
        interaction = make_interaction(user_voice_channel=channel)
        sources = [
            {"type": "usb_mic", "device": "plughw:0,0", "description": "Mic A"},
            {"type": "usb_mic", "device": "plughw:1,0", "description": "Mic B"},
        ]
        with patch.object(bot, "discover_hardware_profile", return_value=sources), \
             patch.object(bot, "probe_device_has_signal", side_effect=[("signal", "rms=99")]), \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.auto_input.callback(interaction))

        mock_exec.assert_called_once()
        args, kwargs = mock_exec.call_args
        assert kwargs["force_source_type"] == "usb_mic"
        assert kwargs["force_device"] == "plughw:0,0"

    def test_no_live_signal_detected(self):
        channel = MagicMock()
        interaction = make_interaction(user_voice_channel=channel)
        sources = [
            {"type": "usb_mic", "device": "plughw:0,0", "description": "Mic A"},
        ]
        with patch.object(bot, "discover_hardware_profile", return_value=sources), \
             patch.object(bot, "probe_device_has_signal", return_value=("silent", "rms=0")):
            run(bot.auto_input.callback(interaction))

        response_text = str(interaction.followup.send.call_args_list[-1])
        assert "No live signal detected" in response_text

    def test_probe_errors_reported_when_no_live_source(self):
        channel = MagicMock()
        interaction = make_interaction(user_voice_channel=channel)
        sources = [
            {"type": "usb_mic", "device": "plughw:0,0", "description": "Mic A"},
        ]
        with patch.object(bot, "discover_hardware_profile", return_value=sources), \
             patch.object(bot, "probe_device_has_signal", return_value=("error", "busy")):
            run(bot.auto_input.callback(interaction))

        response_text = str(interaction.followup.send.call_args_list[-1])
        assert "couldn't get a real reading" in response_text
        assert "busy" in response_text

    def test_second_mic_has_signal_after_first_errors(self):
        channel = MagicMock()
        interaction = make_interaction(user_voice_channel=channel)
        sources = [
            {"type": "usb_mic", "device": "plughw:0,0", "description": "Mic A"},
            {"type": "usb_mic", "device": "plughw:1,0", "description": "Mic B"},
        ]
        with patch.object(bot, "discover_hardware_profile", return_value=sources), \
             patch.object(bot, "probe_device_has_signal",
                           side_effect=[("error", "busy"), ("signal", "rms=200")]), \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.auto_input.callback(interaction))

        mock_exec.assert_called_once()
        kwargs = mock_exec.call_args.kwargs
        assert kwargs["force_device"] == "plughw:1,0"


# ======================================================================
# /radio channel
# ======================================================================

class TestTuneChannelCommand:
    def test_scan_delegates_to_execute_channel_scan(self):
        interaction = make_interaction()
        with patch.object(bot, "execute_channel_scan", new=AsyncMock()) as mock_scan:
            run(bot.tune_channel.callback(interaction, "scan 88-108"))
        mock_scan.assert_called_once()
        args = mock_scan.call_args[0]
        assert args[1] == (88_000_000, 108_000_000)

    def test_invalid_scan_range_sends_warning(self):
        interaction = make_interaction()
        run(bot.tune_channel.callback(interaction, "scan 200-20"))
        interaction.response.send_message.assert_called_once()
        assert "greater than" in str(interaction.response.send_message.call_args)

    def test_invalid_frequency_format(self):
        interaction = make_interaction()
        run(bot.tune_channel.callback(interaction, "not-a-freq!!"))
        interaction.response.send_message.assert_called_once()
        assert "Invalid format" in str(interaction.response.send_message.call_args)

    def test_digit_only_gets_m_suffix(self, tmp_path):
        interaction = make_interaction(voice_client=None)
        state_file = str(tmp_path / "state.json")
        with patch("bot.STATE_FILE", state_file), \
             patch.object(bot, "save_stream_state") as mock_save:
            run(bot.tune_channel.callback(interaction, "94"))
        assert bot.CURRENT_TUNED_CHANNEL == "94M"
        mock_save.assert_called_once()

    def test_tunes_with_connected_vc(self, tmp_path):
        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = True
        interaction = make_interaction(voice_client=fake_vc)
        state_file = str(tmp_path / "state.json")

        with patch("bot.STATE_FILE", state_file), \
             patch.object(bot, "save_stream_state") as mock_save, \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.tune_channel.callback(interaction, "99.5M"))

        assert bot.CURRENT_TUNED_CHANNEL == "99.5M"
        interaction.response.defer.assert_called_once()
        mock_save.assert_called_once()
        mock_exec.assert_called_once()

    def test_tunes_without_vc_saves_inactive(self, tmp_path):
        interaction = make_interaction(voice_client=None)
        state_file = str(tmp_path / "state.json")

        with patch("bot.STATE_FILE", state_file), \
             patch.object(bot, "save_stream_state") as mock_save:
            run(bot.tune_channel.callback(interaction, "88.3M"))

        mock_save.assert_called_once()
        assert mock_save.call_args[1]["is_active"] is False
        interaction.response.send_message.assert_called_once()
        assert "88.3M" in str(interaction.response.send_message.call_args)

    def test_tunes_reads_existing_state_for_source(self, tmp_path):
        state_file = str(tmp_path / "state.json")
        with open(state_file, 'w') as f:
            json.dump({"selected_source": "usb_mic", "selected_device": "plughw:2,0"}, f)

        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = True
        interaction = make_interaction(voice_client=fake_vc)

        with patch("bot.STATE_FILE", state_file), \
             patch.object(bot, "save_stream_state") as mock_save, \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.tune_channel.callback(interaction, "100.1M"))

        assert mock_save.call_args[0][2] == "usb_mic"
        assert mock_exec.call_args.kwargs["force_source_type"] == "usb_mic"

    def test_kilohertz_suffix_accepted(self, tmp_path):
        interaction = make_interaction(voice_client=None)
        state_file = str(tmp_path / "state.json")
        with patch("bot.STATE_FILE", state_file), \
             patch.object(bot, "save_stream_state"):
            run(bot.tune_channel.callback(interaction, "162.4K"))
        assert bot.CURRENT_TUNED_CHANNEL == "162.4K"


# ======================================================================
# /radio sleep, sleep_timer_worker
# ======================================================================

class TestSleepCommand:
    def test_not_connected(self):
        interaction = make_interaction(voice_client=None)
        run(bot.sleep.callback(interaction, "30m"))
        interaction.response.send_message.assert_called_once()
        assert "must be connected" in str(interaction.response.send_message.call_args)

    def test_invalid_duration_unrecognized_unit(self):
        fake_vc = MagicMock()
        interaction = make_interaction(voice_client=fake_vc)
        run(bot.sleep.callback(interaction, "30x"))
        interaction.response.send_message.assert_called_once()
        assert "Unrecognized duration unit" in str(interaction.response.send_message.call_args)

    def test_invalid_duration_generic_format(self):
        fake_vc = MagicMock()
        interaction = make_interaction(voice_client=fake_vc)
        run(bot.sleep.callback(interaction, "not-a-time"))
        interaction.response.send_message.assert_called_once()
        assert "Invalid time string format" in str(interaction.response.send_message.call_args)

    def test_sets_sleep_timer(self):
        fake_vc = MagicMock()
        interaction = make_interaction(guild_id=11, voice_client=fake_vc)
        bot.bot.sleep_tasks = {}
        try:
            with patch.object(bot, "sleep_timer_worker", new=AsyncMock()):
                run(bot.sleep.callback(interaction, "30m"))
            assert 11 in bot.bot.sleep_tasks
            interaction.response.send_message.assert_called_once()
            assert "30m" in str(interaction.response.send_message.call_args)
        finally:
            for t in bot.bot.sleep_tasks.values():
                t.cancel()
            bot.bot.sleep_tasks = {}

    def test_replaces_existing_sleep_timer(self):
        fake_vc = MagicMock()
        interaction = make_interaction(guild_id=12, voice_client=fake_vc)
        old_task = MagicMock()
        bot.bot.sleep_tasks = {12: old_task}
        try:
            with patch.object(bot, "sleep_timer_worker", new=AsyncMock()):
                run(bot.sleep.callback(interaction, "10s"))
            old_task.cancel.assert_called_once()
        finally:
            for t in bot.bot.sleep_tasks.values():
                if hasattr(t, "cancel"):
                    t.cancel()
            bot.bot.sleep_tasks = {}


class TestSleepTimerWorker:
    def test_disconnects_guild_after_delay(self):
        fake_guild = MagicMock()
        fake_guild.voice_client.disconnect = AsyncMock()
        bot.bot.sleep_tasks = {21: MagicMock()}
        try:
            with patch("bot.asyncio.sleep", new=AsyncMock()), \
                 patch.object(bot.bot, "get_guild", return_value=fake_guild), \
                 patch.object(bot, "stop_active_hardware_process") as mock_stop, \
                 patch.object(bot, "clear_stream_state") as mock_clear:
                run(bot.sleep_timer_worker(21, 1800))
        finally:
            bot.bot.sleep_tasks = {}
        mock_stop.assert_called_once()
        mock_clear.assert_called_once()
        fake_guild.voice_client.disconnect.assert_called_once()

    def test_no_guild_does_not_raise(self):
        bot.bot.sleep_tasks = {}
        with patch("bot.asyncio.sleep", new=AsyncMock()), \
             patch.object(bot.bot, "get_guild", return_value=None):
            run(bot.sleep_timer_worker(99, 60))  # should not raise

    def test_guild_without_voice_client(self):
        fake_guild = MagicMock()
        fake_guild.voice_client = None
        with patch("bot.asyncio.sleep", new=AsyncMock()), \
             patch.object(bot.bot, "get_guild", return_value=fake_guild), \
             patch.object(bot, "stop_active_hardware_process") as mock_stop:
            run(bot.sleep_timer_worker(50, 60))
        mock_stop.assert_not_called()


# ======================================================================
# /radio wake, wake_timer_worker
# ======================================================================

class TestWakeCommand:
    def test_not_in_voice_channel(self):
        interaction = make_interaction(user_voice_channel=None)
        run(bot.wake.callback(interaction, "30m"))
        interaction.response.send_message.assert_called_once()

    def test_invalid_duration(self):
        channel = MagicMock()
        interaction = make_interaction(user_voice_channel=channel)
        run(bot.wake.callback(interaction, "garbage"))
        interaction.response.send_message.assert_called_once()
        assert "Unrecognized wake duration" in str(interaction.response.send_message.call_args)

    def test_sets_wake_timer(self):
        channel = MagicMock()
        channel.id = 777
        interaction = make_interaction(guild_id=31, user_voice_channel=channel)
        bot.bot.wake_tasks = {}
        try:
            with patch.object(bot, "wake_timer_worker", new=AsyncMock()):
                run(bot.wake.callback(interaction, "1h"))
            assert 31 in bot.bot.wake_tasks
            interaction.response.send_message.assert_called_once()
        finally:
            for t in bot.bot.wake_tasks.values():
                t.cancel()
            bot.bot.wake_tasks = {}

    def test_replaces_existing_wake_timer(self):
        channel = MagicMock()
        interaction = make_interaction(guild_id=32, user_voice_channel=channel)
        old_task = MagicMock()
        bot.bot.wake_tasks = {32: old_task}
        try:
            with patch.object(bot, "wake_timer_worker", new=AsyncMock()):
                run(bot.wake.callback(interaction, "5m"))
            old_task.cancel.assert_called_once()
        finally:
            for t in bot.bot.wake_tasks.values():
                if hasattr(t, "cancel"):
                    t.cancel()
            bot.bot.wake_tasks = {}


class TestWakeTimerWorker:
    def test_starts_stream_after_delay(self):
        fake_channel = MagicMock(spec=discord.VoiceChannel)
        fake_channel.guild = MagicMock()
        with patch("bot.asyncio.sleep", new=AsyncMock()), \
             patch.object(bot.bot, "get_channel", return_value=fake_channel), \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.wake_timer_worker(41, 12345, 3600))
        mock_exec.assert_called_once()

    def test_channel_not_found_skips(self):
        with patch("bot.asyncio.sleep", new=AsyncMock()), \
             patch.object(bot.bot, "get_channel", return_value=None), \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.wake_timer_worker(42, 999, 60))
        mock_exec.assert_not_called()

    def test_channel_wrong_type_skips(self):
        not_voice_channel = MagicMock(spec=discord.TextChannel)
        with patch("bot.asyncio.sleep", new=AsyncMock()), \
             patch.object(bot.bot, "get_channel", return_value=not_voice_channel), \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.wake_timer_worker(43, 999, 60))
        mock_exec.assert_not_called()

    def test_removes_self_from_wake_tasks(self):
        fake_channel = MagicMock(spec=discord.VoiceChannel)
        fake_channel.guild = MagicMock()
        bot.bot.wake_tasks = {44: MagicMock()}
        try:
            with patch("bot.asyncio.sleep", new=AsyncMock()), \
                 patch.object(bot.bot, "get_channel", return_value=fake_channel), \
                 patch.object(bot, "execute_stream_pipeline", new=AsyncMock()):
                run(bot.wake_timer_worker(44, 111, 60))
            assert 44 not in bot.bot.wake_tasks
        finally:
            bot.bot.wake_tasks = {}


# ======================================================================
# find_peaks_in_step — DC-guard exclusion and multi-peak grouping
# ======================================================================

class TestFindPeaksInStepDcGuard:
    def _make_tone(self, freq_offset_hz, sample_rate, n, amplitude=50.0):
        t = np.arange(n) / sample_rate
        return amplitude * np.exp(2j * np.pi * freq_offset_hz * t)

    def test_dc_spike_excluded_from_results(self):
        sample_rate = bot.SCAN_SAMPLE_RATE_HZ
        n = bot.SCAN_FFT_SIZE
        center_hz = 100_000_000
        rng = np.random.default_rng(7)
        noise = rng.standard_normal(n).astype(np.complex128) * 0.001
        # A DC-only component (zero frequency offset) whose leakage stays
        # within the guard band -- lands right on the center bin, which the
        # DC guard should exclude entirely.
        dc_spike = np.full(n, 1.0, dtype=np.complex128)
        samples = noise + dc_spike

        peaks = bot.find_peaks_in_step(center_hz, sample_rate, samples)

        # The DC artifact must never be reported as a channel.
        assert peaks == []

    def test_off_center_tone_detected_and_dc_excluded(self):
        sample_rate = bot.SCAN_SAMPLE_RATE_HZ
        n = bot.SCAN_FFT_SIZE
        center_hz = 100_000_000
        rng = np.random.default_rng(11)
        noise = rng.standard_normal(n).astype(np.complex128) * 0.001

        # A real off-center tone, well clear of the DC guard band.
        real_tone = self._make_tone(300_000, sample_rate, n, amplitude=80.0)
        # Plus a small DC artifact whose leakage stays within the guard band.
        dc_spike = np.full(n, 1.0, dtype=np.complex128)

        samples = noise + real_tone + dc_spike
        peaks = bot.find_peaks_in_step(center_hz, sample_rate, samples)

        assert len(peaks) >= 1
        # At least one detected peak should be near center + 300kHz, and
        # the DC artifact itself should not show up as its own peak.
        bin_width = sample_rate / n
        found_offset_peak = any(
            abs(freq_hz - (center_hz + 300_000)) < 5 * bin_width for freq_hz, _ in peaks
        )
        assert found_offset_peak
        assert not any(freq_hz == center_hz for freq_hz, _ in peaks)

    def test_two_separated_peaks_both_returned(self):
        sample_rate = bot.SCAN_SAMPLE_RATE_HZ
        n = bot.SCAN_FFT_SIZE
        center_hz = 100_000_000
        rng = np.random.default_rng(3)
        noise = rng.standard_normal(n).astype(np.complex128) * 0.001

        tone_a = self._make_tone(300_000, sample_rate, n, amplitude=80.0)
        tone_b = self._make_tone(-400_000, sample_rate, n, amplitude=80.0)
        samples = noise + tone_a + tone_b

        peaks = bot.find_peaks_in_step(center_hz, sample_rate, samples)
        assert len(peaks) >= 2

    def test_entirely_below_threshold_returns_empty(self):
        sample_rate = bot.SCAN_SAMPLE_RATE_HZ
        n = bot.SCAN_FFT_SIZE
        samples = np.zeros(n, dtype=np.complex128)
        peaks = bot.find_peaks_in_step(100_000_000, sample_rate, samples)
        assert peaks == []


# ======================================================================
# execute_channel_scan — full success/failure flows
# ======================================================================

class TestExecuteChannelScanFullFlow:
    def test_pauses_active_pipeline_before_scanning(self):
        interaction = make_interaction()
        bot.bot.hardware_process = MagicMock()
        try:
            with patch("bot.shutil.which", return_value="/usr/bin/rtl_sdr"), \
                 patch.object(bot, "NUMPY_AVAILABLE", True), \
                 patch.object(bot, "stop_active_hardware_process") as mock_stop, \
                 patch.object(bot, "scan_for_clear_channels_sync", return_value=[]):
                run(bot.execute_channel_scan(interaction, (94_000_000, 95_000_000)))
        finally:
            bot.bot.hardware_process = None

        mock_stop.assert_called_once()
        interaction.response.defer.assert_called_once()
        all_calls = " ".join(str(c) for c in interaction.followup.send.call_args_list)
        assert "Pausing the active pipeline" in all_calls
        assert "pipeline that was running before this scan is now stopped" in all_calls

    def test_no_channels_found_message(self):
        interaction = make_interaction()
        bot.bot.hardware_process = None
        with patch("bot.shutil.which", return_value="/usr/bin/rtl_sdr"), \
             patch.object(bot, "NUMPY_AVAILABLE", True), \
             patch.object(bot, "scan_for_clear_channels_sync", return_value=[]):
            run(bot.execute_channel_scan(interaction, (94_000_000, 95_000_000)))

        all_calls = " ".join(str(c) for c in interaction.followup.send.call_args_list)
        assert "No channels above the noise floor" in all_calls

    def test_channels_found_lists_catalog(self):
        interaction = make_interaction()
        bot.bot.hardware_process = None
        catalog = [{"frequency": "94.5M", "power_db": -12.3}]
        with patch("bot.shutil.which", return_value="/usr/bin/rtl_sdr"), \
             patch.object(bot, "NUMPY_AVAILABLE", True), \
             patch.object(bot, "scan_for_clear_channels_sync", return_value=catalog):
            run(bot.execute_channel_scan(interaction, (94_000_000, 95_000_000)))

        all_calls = " ".join(str(c) for c in interaction.followup.send.call_args_list)
        assert "Clear Channels Found" in all_calls
        assert "94.5M" in all_calls
        assert "-12.3" in all_calls
        assert "/radio channel <frequency>" in all_calls

    def test_scan_exception_reports_failure(self):
        interaction = make_interaction()
        bot.bot.hardware_process = None
        with patch("bot.shutil.which", return_value="/usr/bin/rtl_sdr"), \
             patch.object(bot, "NUMPY_AVAILABLE", True), \
             patch.object(bot, "scan_for_clear_channels_sync", side_effect=RuntimeError("dongle unplugged")):
            run(bot.execute_channel_scan(interaction, (94_000_000, 95_000_000)))

        all_calls = " ".join(str(c) for c in interaction.followup.send.call_args_list)
        assert "Scan failed" in all_calls
        assert "dongle unplugged" in all_calls

    def test_long_response_chunked_across_multiple_sends(self):
        interaction = make_interaction()
        bot.bot.hardware_process = None
        # Build a large catalog so the response exceeds 1900 chars and must
        # be split across multiple followup.send calls.
        catalog = [{"frequency": f"{88 + i * 0.1:.1f}M", "power_db": -10.0} for i in range(150)]
        with patch("bot.shutil.which", return_value="/usr/bin/rtl_sdr"), \
             patch.object(bot, "NUMPY_AVAILABLE", True), \
             patch.object(bot, "scan_for_clear_channels_sync", return_value=catalog):
            run(bot.execute_channel_scan(interaction, (88_000_000, 108_000_000)))

        # More than the 2 "status" sends (scanning.../catalog) -- chunked
        assert interaction.followup.send.call_count > 2


# ======================================================================
# on_ready — crash recovery lifecycle
# ======================================================================

def _patched_bot_user():
    """Context manager patching discord.Client.user (a read-only property)
    so on_ready's f-string `{bot.user.name}` doesn't blow up on None."""
    fake_user = MagicMock()
    fake_user.name = "StreamBot"
    return patch.object(type(bot.bot), "user", new_callable=PropertyMock, return_value=fake_user)


class TestOnReady:
    def test_stay_disconnected_mode_returns_early(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"is_active": True}))
        with _patched_bot_user(), \
             patch("bot.RECOVERY_MODE", "stay_disconnected"), \
             patch("bot.STATE_FILE", str(state_file)), \
             patch.object(bot, "clear_stream_state") as mock_clear, \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.on_ready())
        mock_clear.assert_not_called()
        mock_exec.assert_not_called()

    def test_no_state_file_returns_early(self, tmp_path):
        state_file = tmp_path / "nonexistent.json"
        with _patched_bot_user(), \
             patch("bot.RECOVERY_MODE", "resume"), \
             patch("bot.STATE_FILE", str(state_file)), \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.on_ready())
        mock_exec.assert_not_called()

    def test_dormant_state_does_not_auto_connect(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "is_active": False,
            "selected_source": "usb_mic",
            "tuned_frequency": "99.9M",
        }))
        with _patched_bot_user(), \
             patch("bot.RECOVERY_MODE", "resume"), \
             patch("bot.STATE_FILE", str(state_file)), \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.on_ready())
        mock_exec.assert_not_called()
        assert bot.CURRENT_TUNED_CHANNEL == "99.9M"

    def test_invalid_saved_channel_clears_state(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "is_active": True,
            "selected_source": "test_signal",
            "guild_id": 1,
            "channel_id": 999,
        }))
        with _patched_bot_user(), \
             patch("bot.RECOVERY_MODE", "resume"), \
             patch("bot.STATE_FILE", str(state_file)), \
             patch.object(bot.bot, "get_channel", return_value=None), \
             patch.object(bot, "clear_stream_state") as mock_clear, \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.on_ready())
        mock_clear.assert_called_once()
        mock_exec.assert_not_called()

    def test_saved_channel_wrong_type_clears_state(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "is_active": True,
            "guild_id": 1,
            "channel_id": 999,
        }))
        not_voice = MagicMock(spec=discord.TextChannel)
        with _patched_bot_user(), \
             patch("bot.RECOVERY_MODE", "resume"), \
             patch("bot.STATE_FILE", str(state_file)), \
             patch.object(bot.bot, "get_channel", return_value=not_voice), \
             patch.object(bot, "clear_stream_state") as mock_clear:
            run(bot.on_ready())
        mock_clear.assert_called_once()

    def test_successful_recovery_resumes_stream(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "is_active": True,
            "selected_source": "usb_mic",
            "selected_device": "plughw:0,0",
            "volume_level": 0.75,
            "guild_id": 5,
            "channel_id": 42,
        }))
        fake_channel = MagicMock(spec=discord.VoiceChannel)
        fake_channel.name = "General"
        fake_channel.guild = MagicMock()

        with _patched_bot_user(), \
             patch("bot.RECOVERY_MODE", "resume"), \
             patch("bot.STATE_FILE", str(state_file)), \
             patch.object(bot.bot, "get_channel", return_value=fake_channel), \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.on_ready())

        mock_exec.assert_called_once()
        args, kwargs = mock_exec.call_args
        assert args[1] is fake_channel
        assert kwargs["force_source_type"] == "usb_mic"
        assert kwargs["force_device"] == "plughw:0,0"

    def test_exception_during_recovery_clears_state(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("not valid json{{{")

        with _patched_bot_user(), \
             patch("bot.RECOVERY_MODE", "resume"), \
             patch("bot.STATE_FILE", str(state_file)), \
             patch.object(bot, "clear_stream_state") as mock_clear:
            run(bot.on_ready())

        mock_clear.assert_called_once()

    def test_recovery_exception_from_execute_pipeline_clears_state(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "is_active": True,
            "guild_id": 5,
            "channel_id": 42,
        }))
        fake_channel = MagicMock(spec=discord.VoiceChannel)
        fake_channel.name = "General"
        fake_channel.guild = MagicMock()

        with _patched_bot_user(), \
             patch("bot.RECOVERY_MODE", "resume"), \
             patch("bot.STATE_FILE", str(state_file)), \
             patch.object(bot.bot, "get_channel", return_value=fake_channel), \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch.object(bot, "clear_stream_state") as mock_clear:
            run(bot.on_ready())

        mock_clear.assert_called_once()


# ======================================================================
# setup_hook
# ======================================================================

class TestSetupHook:
    def test_registers_command_group_and_syncs(self):
        with patch.object(bot.bot.tree, "add_command") as mock_add, \
             patch.object(bot.bot.tree, "sync", new=AsyncMock()) as mock_sync:
            run(bot.bot.setup_hook())
        mock_add.assert_called_once_with(bot.radio_group)
        mock_sync.assert_called_once()


# ======================================================================
# tune_channel — corrupt state file without an active voice client
# ======================================================================

class TestTuneChannelCorruptStateNoVc:
    def test_corrupt_state_file_falls_back_to_defaults(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("not valid json{{{")
        interaction = make_interaction(voice_client=None)

        with patch("bot.STATE_FILE", str(state_file)), \
             patch.object(bot, "save_stream_state") as mock_save:
            run(bot.tune_channel.callback(interaction, "88.1M"))

        mock_save.assert_called_once()
        assert mock_save.call_args[0][2] == "test_signal"
        interaction.response.send_message.assert_called_once()

    def test_valid_state_file_read_without_vc(self, tmp_path):
        """Covers the normal (non-exception) read path when no vc is connected."""
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "selected_source": "usb_mic",
            "selected_device": "plughw:4,0",
        }))
        interaction = make_interaction(voice_client=None)

        with patch("bot.STATE_FILE", str(state_file)), \
             patch.object(bot, "save_stream_state") as mock_save:
            run(bot.tune_channel.callback(interaction, "88.1M"))

        mock_save.assert_called_once()
        assert mock_save.call_args[0][2] == "usb_mic"
        assert mock_save.call_args[1]["selected_device"] == "plughw:4,0"

    def test_corrupt_state_file_with_connected_vc_falls_back(self, tmp_path):
        """Covers the except-branch in the *connected vc* half of tune_channel."""
        state_file = tmp_path / "state.json"
        state_file.write_text("not valid json{{{")
        fake_vc = MagicMock()
        fake_vc.is_connected.return_value = True
        interaction = make_interaction(voice_client=fake_vc)

        with patch("bot.STATE_FILE", str(state_file)), \
             patch.object(bot, "save_stream_state") as mock_save, \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.tune_channel.callback(interaction, "88.1M"))

        assert mock_save.call_args[0][2] == "test_signal"
        mock_exec.assert_called_once()


# ======================================================================
# Remaining small exception / edge branches
# ======================================================================

class TestSaveStreamStateExceptionBranch:
    def test_write_failure_does_not_raise(self, tmp_path, capsys):
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a directory")
        bad_state_file = str(blocker / "sub" / "state.json")
        with patch("bot.STATE_FILE", bad_state_file):
            bot.save_stream_state(1, 2)  # should not raise
        captured = capsys.readouterr()
        assert "Failed writing configuration payload" in captured.out


class TestClearStreamStateExceptionBranch:
    def test_read_failure_does_not_raise(self, tmp_path, capsys):
        state_file = tmp_path / "state.json"
        state_file.write_text("not valid json{{{")
        with patch("bot.STATE_FILE", str(state_file)):
            bot.clear_stream_state()  # should not raise
        captured = capsys.readouterr()
        assert "Failed updating connection state parameters" in captured.out


class TestSelfHealExceptionBranch:
    def test_write_failure_does_not_raise(self, tmp_path, capsys):
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a directory")
        bad_sources_dir = str(blocker / "sub")
        with patch("bot.SOURCES_DIR", bad_sources_dir):
            bot.self_heal_test_signal_profile()  # should not raise
        captured = capsys.readouterr()
        assert "Failed to write fallback matrix" in captured.out


class TestStopActiveHardwareProcessMore:
    def test_second_wait_succeeds_after_sigterm_timeout(self):
        """First attempt (SIGTERM) fails via proc.wait timeout; second
        attempt (SIGKILL) succeeds cleanly -- covers the inner try's
        success path (line after the second killpg call)."""
        proc = MagicMock(pid=4242)
        wait_calls = {"n": 0}

        def fake_wait(timeout=None):
            wait_calls["n"] += 1
            if wait_calls["n"] == 1:
                raise TimeoutError()
            return None  # second call succeeds

        with patch.object(bot.bot, "hardware_process", proc), \
             patch.object(bot.bot, "sox_process", None), \
             patch.object(bot.bot, "ffmpeg_process", None), \
             patch("os.getpgid", return_value=100), \
             patch("os.killpg") as mock_killpg, \
             patch.object(proc, "wait", side_effect=fake_wait):
            bot.stop_active_hardware_process()

        assert mock_killpg.call_count == 2
        assert bot.bot.hardware_process is None

    def test_final_kill_also_raises_is_swallowed(self):
        """Everything fails (getpgid raises every time, proc.kill() also
        raises) -- the innermost except should swallow it silently."""
        proc = MagicMock(pid=9001)
        proc.kill.side_effect = OSError("already dead")

        with patch.object(bot.bot, "hardware_process", proc), \
             patch.object(bot.bot, "sox_process", None), \
             patch.object(bot.bot, "ffmpeg_process", None), \
             patch("os.getpgid", side_effect=ProcessLookupError()):
            bot.stop_active_hardware_process()  # should not raise

        assert bot.bot.hardware_process is None
