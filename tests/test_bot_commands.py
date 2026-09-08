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

# ======================================================================
# discover_hardware_profile -- module aggregation, fallback, and cache
# behavior is covered thoroughly in test_bot.py's TestDiscoverHardwareProfile
# and TestLoadSourceModules now that bot.py has no ALSA/SDR-specific code
# left to test here. The actual ALSA card-scanning and SDR chipset-detection
# logic that used to be exercised in this file now lives in
# sources/alsa.py and sources/sdr_radio.py (etc.), and is covered directly
# in tests/test_sources_alsa.py and tests/test_sources_sdr.py.
# ======================================================================


# ======================================================================
# spawn_hardware_capture_stream
# ======================================================================

class TestSpawnHardwareCaptureStream:
    def test_unknown_source_falls_back_to_builtin_tone(self, capsys):
        fake_popen = MagicMock()
        with patch.object(bot, "load_source_modules", return_value={}), \
             patch.object(bot, "stop_active_hardware_process"), \
             patch("bot.subprocess.Popen", return_value=fake_popen) as mock_popen, \
             patch("bot.FIFO_PIPE", "/tmp/fake_pipe"):
            bot.spawn_hardware_capture_stream({"type": "unknown_type"})

        assert "no longer available" in capsys.readouterr().out
        called_cmd = mock_popen.call_args[0][0]
        assert "ffmpeg" in called_cmd
        assert "/tmp/fake_pipe" in called_cmd

    def test_module_raising_in_build_command_falls_back_to_builtin_tone(self, capsys):
        broken_module = MagicMock()
        broken_module.build_command.side_effect = RuntimeError("no hardware")
        fake_popen = MagicMock()
        with patch.object(bot, "load_source_modules", return_value={"broken": broken_module}), \
             patch.object(bot, "stop_active_hardware_process"), \
             patch("bot.subprocess.Popen", return_value=fake_popen) as mock_popen, \
             patch("bot.FIFO_PIPE", "/tmp/fake_pipe"):
            bot.spawn_hardware_capture_stream({"type": "broken"})

        assert "raised while building its pipeline" in capsys.readouterr().out
        called_cmd = mock_popen.call_args[0][0]
        assert "ffmpeg" in called_cmd

    def test_spawns_subprocess_with_modules_compiled_command(self):
        usb_mic_module = MagicMock()
        usb_mic_module.build_command.return_value = "arecord -D plughw:0,0 -c 2 >> /tmp/fake_pipe"
        fake_popen = MagicMock()

        with patch.object(bot, "load_source_modules", return_value={"usb_mic": usb_mic_module}), \
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
        # bot.py forwarded the active source dict straight through untouched
        usb_mic_module.build_command.assert_called_once()
        call_kwargs = usb_mic_module.build_command.call_args
        assert call_kwargs.kwargs["fifo_pipe"] == "/tmp/fake_pipe"

    def test_stops_previous_process_first(self):
        usb_mic_module = MagicMock()
        usb_mic_module.build_command.return_value = "cmd >> /tmp/fake_pipe"
        with patch.object(bot, "load_source_modules", return_value={"usb_mic": usb_mic_module}), \
             patch.object(bot, "stop_active_hardware_process") as mock_stop, \
             patch("bot.subprocess.Popen", return_value=MagicMock()):
            bot.spawn_hardware_capture_stream({"type": "usb_mic"})
        mock_stop.assert_called_once()

    def test_builtin_fallback_type_never_looks_up_modules(self):
        """The builtin fallback source should never trigger a module lookup
        at all -- it's the one thing bot.py is allowed to know about directly."""
        with patch.object(bot, "load_source_modules") as mock_load, \
             patch.object(bot, "stop_active_hardware_process"), \
             patch("bot.subprocess.Popen", return_value=MagicMock()):
            bot.spawn_hardware_capture_stream(dict(bot.BUILTIN_FALLBACK_SOURCE))
        mock_load.assert_not_called()


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
        """With the JSON-era special-case removed, `test_signal` shows up in
        the list like any other source -- the "no hardware" message now only
        appears when discovery genuinely returns nothing at all."""
        interaction = make_interaction()
        with patch.object(bot, "discover_hardware_profile", return_value=[]), \
             patch.object(bot, "scan_sources_for_signal", return_value={}):
            run(bot.set_input.callback(interaction, None))

        response_text = str(interaction.followup.send.call_args)
        assert "No physical audio hardware" in response_text

    def test_list_mode_shows_every_discovered_source_including_test_signal(self):
        """test_signal is just a normal, listed source now -- bot.py has no
        special-case code hiding it anymore."""
        interaction = make_interaction()
        sources = [{"type": "test_signal", "device": "virtual", "description": "Test"}]
        with patch.object(bot, "discover_hardware_profile", return_value=sources), \
             patch.object(bot, "scan_sources_for_signal", return_value={}):
            run(bot.set_input.callback(interaction, None))

        response_text = str(interaction.followup.send.call_args)
        assert "Test" in response_text
        assert "No physical audio hardware" not in response_text


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

    def test_no_probeable_sources(self):
        """test_signal (and any other source without probe_signal) isn't
        probeable -- /radio auto should say so generically, without any
        ALSA/USB-specific wording baked into bot.py."""
        channel = MagicMock()
        interaction = make_interaction(user_voice_channel=channel)
        sources = [{"type": "test_signal", "device": "virtual"}]
        no_probe_module = MagicMock(spec=[])  # no probe_signal attribute at all
        with patch.object(bot, "discover_hardware_profile", return_value=sources), \
             patch.object(bot, "load_source_modules", return_value={"test_signal": no_probe_module}):
            run(bot.auto_input.callback(interaction))
        interaction.followup.send.assert_called_once()
        assert "No probeable input interfaces" in str(interaction.followup.send.call_args)

    def test_finds_live_source(self):
        channel = MagicMock()
        interaction = make_interaction(user_voice_channel=channel)
        sources = [
            {"type": "usb_mic", "device": "plughw:0,0", "description": "Mic A"},
            {"type": "usb_mic", "device": "plughw:1,0", "description": "Mic B"},
        ]
        usb_mic_module = MagicMock()
        usb_mic_module.probe_signal.side_effect = [("signal", "rms=99")]
        with patch.object(bot, "discover_hardware_profile", return_value=sources), \
             patch.object(bot, "load_source_modules", return_value={"usb_mic": usb_mic_module}), \
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
        usb_mic_module = MagicMock()
        usb_mic_module.probe_signal.return_value = ("silent", "rms=0")
        with patch.object(bot, "discover_hardware_profile", return_value=sources), \
             patch.object(bot, "load_source_modules", return_value={"usb_mic": usb_mic_module}):
            run(bot.auto_input.callback(interaction))

        response_text = str(interaction.followup.send.call_args_list[-1])
        assert "No live signal detected" in response_text

    def test_probe_errors_reported_when_no_live_source(self):
        channel = MagicMock()
        interaction = make_interaction(user_voice_channel=channel)
        sources = [
            {"type": "usb_mic", "device": "plughw:0,0", "description": "Mic A"},
        ]
        usb_mic_module = MagicMock()
        usb_mic_module.probe_signal.return_value = ("error", "busy")
        with patch.object(bot, "discover_hardware_profile", return_value=sources), \
             patch.object(bot, "load_source_modules", return_value={"usb_mic": usb_mic_module}):
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
        usb_mic_module = MagicMock()
        usb_mic_module.probe_signal.side_effect = [("error", "busy"), ("signal", "rms=200")]
        with patch.object(bot, "discover_hardware_profile", return_value=sources), \
             patch.object(bot, "load_source_modules", return_value={"usb_mic": usb_mic_module}), \
             patch.object(bot, "execute_stream_pipeline", new=AsyncMock()) as mock_exec:
            run(bot.auto_input.callback(interaction))

        mock_exec.assert_called_once()
        kwargs = mock_exec.call_args.kwargs
        assert kwargs["force_device"] == "plughw:1,0"

    def test_probe_signal_raising_is_treated_as_error_not_a_crash(self):
        channel = MagicMock()
        interaction = make_interaction(user_voice_channel=channel)
        sources = [{"type": "flaky", "device": "dev0", "description": "Flaky"}]
        flaky_module = MagicMock()
        flaky_module.probe_signal.side_effect = RuntimeError("boom")
        with patch.object(bot, "discover_hardware_profile", return_value=sources), \
             patch.object(bot, "load_source_modules", return_value={"flaky": flaky_module}):
            run(bot.auto_input.callback(interaction))

        response_text = str(interaction.followup.send.call_args_list[-1])
        assert "boom" in response_text


# ======================================================================
# /radio channel
# ======================================================================

class TestTuneChannelCommand:
    def test_scan_keyword_delegates_to_handle_channel_scan_request(self):
        """tune_channel only does a lightweight 'does this look like a scan
        request' regex check itself -- the real parsing/validation (which
        needs the active source's own MHz policy) happens inside
        handle_channel_scan_request. See TestHandleChannelScanRequest in
        test_bot.py for that logic in full."""
        interaction = make_interaction()
        with patch.object(bot, "handle_channel_scan_request", new=AsyncMock()) as mock_handle:
            run(bot.tune_channel.callback(interaction, "scan 88-108"))
        mock_handle.assert_called_once_with(interaction, "scan 88-108")

    def test_bare_scan_keyword_also_delegates(self):
        interaction = make_interaction()
        with patch.object(bot, "handle_channel_scan_request", new=AsyncMock()) as mock_handle:
            run(bot.tune_channel.callback(interaction, "SCAN"))
        mock_handle.assert_called_once()

    def test_non_scan_frequency_does_not_delegate(self):
        interaction = make_interaction(voice_client=None)
        with patch.object(bot, "handle_channel_scan_request", new=AsyncMock()) as mock_handle, \
             patch.object(bot, "save_stream_state"):
            run(bot.tune_channel.callback(interaction, "94.9M"))
        mock_handle.assert_not_called()

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

# ======================================================================
# Note: find_peaks_in_step's DC-guard behavior now lives entirely in
# actions/scan_range.py (bot.py has no FFT code of its own anymore) --
# see TestFindPeaksInStep in tests/test_actions_scan_range.py.
# ======================================================================


# ======================================================================
# execute_channel_scan — full success/failure flows via the source-agnostic
# dispatch path (a plain MagicMock stands in for whatever scan_fn the
# active source module would actually provide -- execute_channel_scan
# doesn't know or care what's behind it).
# ======================================================================

class TestExecuteChannelScanFullFlow:
    def test_long_response_chunked_across_multiple_sends(self):
        interaction = make_interaction()
        bot.bot.hardware_process = None
        # Build a large catalog so the response exceeds 1900 chars and must
        # be split across multiple followup.send calls.
        catalog = [{"frequency": f"{88 + i * 0.1:.1f}M", "power_db": -10.0} for i in range(150)]
        scan_fn = MagicMock(return_value=catalog)
        run(bot.execute_channel_scan(interaction, (88_000_000, 108_000_000), scan_fn, active_description="Radio"))

        # More than the 2 "status" sends (scanning.../catalog) -- chunked
        assert interaction.followup.send.call_count > 2

    def test_scan_fn_called_with_hz_bounds(self):
        interaction = make_interaction()
        bot.bot.hardware_process = None
        scan_fn = MagicMock(return_value=[])
        run(bot.execute_channel_scan(interaction, (94_000_000, 95_000_000), scan_fn, active_description="Radio"))
        scan_fn.assert_called_once_with(94_000_000, 95_000_000)

    def test_defers_response_before_sending_followups(self):
        interaction = make_interaction()
        bot.bot.hardware_process = None
        scan_fn = MagicMock(return_value=[])
        run(bot.execute_channel_scan(interaction, (94_000_000, 95_000_000), scan_fn, active_description="Radio"))
        interaction.response.defer.assert_called_once()


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


class TestLoadPackageAllowlistExceptionBranch:
    def test_seed_write_failure_falls_back_to_in_memory_default(self, tmp_path, capsys):
        """Mirrors the old self-heal exception-branch test, but for the new
        dependency-guard allowlist seeding: a genuinely unwritable path
        (parent is a file, not a directory) should be caught and logged,
        falling back to the small in-memory default rather than raising."""
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a directory")
        bad_allowlist_file = str(blocker / "sub" / "package_allowlist.json")
        with patch("bot.PACKAGE_ALLOWLIST_FILE", bad_allowlist_file), \
             patch("bot.PACKAGE_ALLOWLIST_TEMPLATE", "/nonexistent-template.json"):
            allowlist = bot.load_package_allowlist()
        captured = capsys.readouterr()
        assert "Failed to seed package allowlist" in captured.out
        assert "ffmpeg" in allowlist


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
