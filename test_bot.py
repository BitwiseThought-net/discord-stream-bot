import os
import sys
import json
import asyncio
import unittest
from unittest.mock import patch, mock_open, MagicMock, AsyncMock
from datetime import datetime, timedelta

# Inject required environment stubs before module evaluation
os.environ['DISCORD_TOKEN'] = 'mock_valid_token_xyz'
os.environ['COMMAND_BASE'] = 'radio'
os.environ['RECOVERY_MODE'] = 'resume'

import discord
import stream_bot

class TestDiscordStreamBotFullCoverage(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        """Prepares a pure, mocked bot state environment prior to executing tests."""
        self.bot = stream_bot.bot
        self.bot.sleep_tasks = {}
        self.bot.wake_tasks = {}
        stream_bot.STATE_FILE = "/data/state.json"

    def _create_mock_interaction(self, in_voice=True, streaming=True, is_transformer=True):
        """Generates dynamic asynchronous mock structures modeling Discord Interactions."""
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.guild = MagicMock(spec=discord.Guild)
        interaction.guild.id = 999
        interaction.user = MagicMock(spec=discord.User)
        
        if in_voice:
            interaction.user.voice = MagicMock()
            interaction.user.voice.channel = MagicMock(spec=discord.VoiceChannel)
            interaction.user.voice.channel.id = 555
            interaction.user.voice.channel.name = "Test Voice"
            interaction.user.voice.channel.connect = AsyncMock()
        else:
            interaction.user.voice = None

        vc = AsyncMock()
        if streaming:
            if is_transformer:
                vc.source = MagicMock(spec=discord.PCMVolumeTransformer)
            else:
                vc.source = MagicMock()
            vc.is_connected = MagicMock(return_value=True)
            vc.disconnect = AsyncMock()
            interaction.guild.voice_client = vc
        else:
            interaction.guild.voice_client = None

        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()
        return interaction, vc

    # =========================================================================
    # CORE UTILITY & COMPONENT FUNCTION MODULE TESTING
    # =========================================================================

    def test_environment_configurations(self):
        """Verifies environment variables load and parse accurately."""
        self.assertEqual(stream_bot.COMMAND_NAME, 'radio')
        self.assertEqual(stream_bot.RECOVERY_MODE, 'resume')

    def test_save_stream_state_success(self):
        """Ensures state records can serialize safely to local mount layouts."""
        m = mock_open()
        with patch('os.makedirs') as mock_make, patch('builtins.open', m):
            stream_bot.save_stream_state(111, 222)
            mock_make.assert_called_once_with('/data', exist_ok=True)
            m.assert_called_once_with('/data/state.json', 'w')

    def test_save_stream_state_exception(self):
        """Validates that internal exceptions during state saves catch gracefully."""
        with patch('os.makedirs', side_effect=Exception("Disk Error")):
            try:
                stream_bot.save_stream_state(111, 222)
            except Exception as e:
                self.fail(f"save_stream_state raised an unhandled exception: {e}")

    def test_clear_stream_state_exists(self):
        """Validates absolute removal sweeps when a file is discovered on disk."""
        with patch('os.path.exists', return_value=True), patch('os.remove') as mock_rm:
            stream_bot.clear_stream_state()
            mock_rm.assert_called_once_with('/data/state.json')

    def test_clear_stream_state_exception(self):
        """Verifies clear failures catch gracefully without terminating runtime processes."""
        with patch('os.path.exists', side_effect=Exception("File System Failure")):
            try:
                stream_bot.clear_stream_state()
            except Exception as e:
                self.fail(f"clear_stream_state raised an unhandled exception: {e}")
    # =========================================================================
    # AUDIO HARDWARE LOGIC PROFILES
    # =========================================================================

    def test_hardware_discovery_missing_base_dir(self):
        """Verifies standard safe device fallback properties occur on empty mounts."""
        with patch('os.path.exists', return_value=False):
            dev, ch = stream_bot.discover_hardware_profile()
            self.assertEqual(dev, 'plughw:1,0')
            self.assertEqual(ch, '2')

    def test_hardware_discovery_mono_profile(self):
        """Verifies profile configuration logic can match clean single channel devices."""
        mock_data = "interface: USB Audio\nchannels: 1 channel\n"
        with patch('os.path.exists', return_value=True), \
             patch('os.listdir', return_value=['card3']), \
             patch('os.path.isdir', return_value=True), \
             patch('builtins.open', mock_open(read_data=mock_data)):
            dev, ch = stream_bot.discover_hardware_profile()
            self.assertEqual(dev, 'plughw:3,0')
            self.assertEqual(ch, '1')

    def test_hardware_discovery_stereo_profile(self):
        """Verifies tracking variables adjust correctly when multi-channel inputs exist."""
        mock_data = "interface: USB Audio High\nchannels: 2 channels\n"
        with patch('os.path.exists', return_value=True), \
             patch('os.listdir', return_value=['card2']), \
             patch('os.path.isdir', return_value=True), \
             patch('builtins.open', mock_open(read_data=mock_data)):
            dev, ch = stream_bot.discover_hardware_profile()
            self.assertEqual(dev, 'plughw:2,0')
            self.assertEqual(ch, '2')

    def test_hardware_discovery_exception_handling(self):
        """Ensures bad runtime files loop safely to standard base fallbacks."""
        with patch('os.path.exists', return_value=True), \
             patch('os.listdir', return_value=['card5']), \
             patch('os.path.isdir', return_value=True), \
             patch('builtins.open', side_effect=Exception("Device error stream")):
            dev, ch = stream_bot.discover_hardware_profile()
            self.assertEqual(dev, 'plughw:1,0')
            self.assertEqual(ch, '2')

    # =========================================================================
    # BROADCAST CORE SUBCOMMAND EXECUTIONS (DIRECT TESTING NAMESPACES)
    # =========================================================================

    async def test_subcommand_start_not_in_voice(self):
        """Blocks initialization instantly if tracking caller reports clear of voice grids."""
        interaction, _ = self._create_mock_interaction(in_voice=False)
        await stream_bot.start(interaction)
        interaction.response.send_message.assert_called_once_with(
            "You must be in a voice channel to start streaming!", ephemeral=True
        )

    async def test_subcommand_start_execution(self):
        """Exercises complete successful direct pipeline connection sequences."""
        interaction, vc = self._create_mock_interaction(in_voice=True)
        with patch('stream_bot.discover_hardware_profile', return_value=('plughw:1,0', '2')), \
             patch('stream_bot.save_stream_state') as mock_save, \
             patch('discord.FFmpegPCMAudio'), \
             patch('discord.PCMVolumeTransformer'):
            await stream_bot.start(interaction)
            mock_save.assert_called_once_with(999, 555)
            vc.play.assert_called_once()

    async def test_subcommand_stop_active(self):
        """Ensures direct execution path triggers drops and terminates sleep loops."""
        interaction, vc = self._create_mock_interaction(streaming=True)
        mock_task = MagicMock()
        self.bot.sleep_tasks = {999: mock_task}
        with patch('stream_bot.clear_stream_state') as mock_clear:
            await stream_bot.stop(interaction)
            mock_task.cancel.assert_called_once()
            mock_clear.assert_called_once()
            vc.disconnect.assert_called_once()

    async def test_subcommand_stop_inactive(self):
        """Returns warnings quickly if execution parameters track zero current connections."""
        interaction, _ = self._create_mock_interaction(streaming=False)
        await stream_bot.stop(interaction)
        interaction.response.send_message.assert_called_once_with(
            "I am not currently connected to a voice channel.", ephemeral=True
        )

    async def test_subcommand_volume_not_streaming(self):
        """Ensures boundary parameters reject modifications if system is fully down."""
        interaction, _ = self._create_mock_interaction(streaming=False)
        await stream_bot.volume(interaction, percentage=50)
        interaction.response.send_message.assert_called_once_with(
            "The bot is not currently streaming!", ephemeral=True
        )

    async def test_subcommand_volume_invalid_transformer(self):
        """Validates clean failure processing if device layout wrappers do not match."""
        interaction, _ = self._create_mock_interaction(streaming=True, is_transformer=False)
        await stream_bot.volume(interaction, percentage=75)
        interaction.response.send_message.assert_called_once_with(
            "Volume control wrapper not ready on this stream layout.", ephemeral=True
        )

    async def test_subcommand_volume_success(self):
        """Ensures exact modifier scaling registers against current tracking hardware properties."""
        interaction, vc = self._create_mock_interaction(streaming=True, is_transformer=True)
        await stream_bot.volume(interaction, percentage=80)
        self.assertEqual(vc.source.volume, 0.8)
    # =========================================================================
    # TIMED MATRIX OPERATIONS (SLEEP / WAKE MATRIX ENGINE)
    # =========================================================================

    async def test_subcommand_sleep_disconnected(self):
        """Ensures scheduling fails instantly if target context reports dark."""
        interaction, _ = self._create_mock_interaction(streaming=False)
        await stream_bot.sleep(interaction, duration="15m")
        interaction.response.send_message.assert_called_once_with(
            "The bot must be connected to a voice channel to set a sleep timer!", ephemeral=True
        )

    async def test_subcommand_sleep_invalid_unit(self):
        """Forces handling exceptions when user injects unparsed suffix designations."""
        interaction, _ = self._create_mock_interaction(streaming=True)
        await stream_bot.sleep(interaction, duration="10x")
        interaction.response.send_message.assert_called_once_with(
            "⚠️ Unrecognized duration unit. Please use seconds, minutes, or hours.", ephemeral=True
        )

    async def test_subcommand_sleep_invalid_format(self):
        """Ensures complete trash validation values map clearly to warning sequences."""
        interaction, _ = self._create_mock_interaction(streaming=True)
        await stream_bot.sleep(interaction, duration="garbage_string")
        interaction.response.send_message.assert_called_once_with(
            "⚠️ Invalid time string format. Try inputs like `30m`, `1.5h`, or `11:45pm`.", ephemeral=True
        )

    async def test_subcommand_sleep_units_matrix(self):
        """Executes relative conversion calculation components directly across every scale format."""
        interaction, vc = self._create_mock_interaction(streaming=True)
        with patch('asyncio.sleep', AsyncMock()), patch('asyncio.create_task'):
            for duration in ["45s", "15m", "1.5h"]:
                await stream_bot.sleep(interaction, duration=duration)
                self.assertIn(999, self.bot.sleep_tasks)

    async def test_subcommand_sleep_absolute_formats(self):
        """Validates that parsing functions interpret AM/PM variations and 24h syntax maps."""
        interaction, vc = self._create_mock_interaction(streaming=True)
        with patch('asyncio.sleep', AsyncMock()), patch('asyncio.create_task'):
            for clock in ["3:45pm", "08:15 am", "22:10"]:
                await stream_bot.sleep(interaction, duration=clock)
                self.assertIn(999, self.bot.sleep_tasks)

    async def test_subcommand_sleep_absolute_rollover(self):
        """Forces exact handling pathways covering calculations into the next calendar day boundary."""
        interaction, _ = self._create_mock_interaction(streaming=True)
        frozen_now = datetime.now().replace(hour=23, minute=0, second=0, microsecond=0)
        with patch('stream_bot.datetime') as mock_dt, patch('asyncio.create_task'):
            mock_dt.now.return_value = frozen_now
            mock_dt.strptime.return_value = datetime.strptime("08:00AM", "%I:%M%p")
            await stream_bot.sleep(interaction, duration="8:00am")
            mock_dt.now.assert_called()

    async def test_subcommand_wake_not_in_voice(self):
        """Rejects background activation workflows cleanly if target space context is missing."""
        interaction, _ = self._create_mock_interaction(in_voice=False)
        await stream_bot.wake(interaction, duration="1h")
        interaction.response.send_message.assert_called_once_with(
            "⚠️ You must be inside a voice channel when running this command so the bot knows where to connect!", ephemeral=True
        )

    async def test_subcommand_wake_invalid_unit(self):
        """Validates conversion safety blocks flag non-standard parameters on wake loops."""
        interaction, _ = self._create_mock_interaction(in_voice=True)
        await stream_bot.wake(interaction, duration="50z")
        interaction.response.send_message.assert_called_once_with(
            "⚠️ Unrecognized wake duration unit. Use seconds, minutes, or hours.", ephemeral=True
        )

    async def test_subcommand_wake_units_matrix(self):
        """Verifies wake operations initialize background tasks using correct intervals."""
        interaction, _ = self._create_mock_interaction(in_voice=True)
        with patch('asyncio.sleep', AsyncMock()), patch('asyncio.create_task'):
            for duration in ["10s", "5m", "2h"]:
                await stream_bot.wake(interaction, duration=duration)
                self.assertIn(999, self.bot.wake_tasks)

    # =========================================================================
    # SYSTEM ENGINE / RECOVERY SUBSTATIONS
    # =========================================================================

    @patch('stream_bot.discover_hardware_profile', return_value=('plughw:1,0', '2'))
    async def test_on_ready_stay_disconnected(self, mock_discover):
        """Ensures recovery operations stand down if configurations mandate clear skips."""
        stream_bot.RECOVERY_MODE = "stay_disconnected"
        with patch('builtins.print') as mock_print:
            await stream_bot.on_ready()
            mock_print.assert_any_call("🔄 [Recovery] Stay disconnected policy enforced. Skipping historical trace parsing loops.")

    @patch('stream_bot.discover_hardware_profile', return_value=('plughw:1,0', '2'))
    async def test_on_ready_resume_no_file(self, mock_discover):
        """Validates boot routines exit smoothly if no historical files are found on disk."""
        stream_bot.RECOVERY_MODE = "resume"
        with patch('os.path.exists', return_value=False), patch('builtins.print') as mock_print:
            await stream_bot.on_ready()
            mock_print.assert_any_call("🔄 [Recovery] Clean boot pipeline detected. No data traces saved to disk.")

    @patch('stream_bot.discover_hardware_profile', return_value=('plughw:1,0', '2'))
    async def test_on_ready_resume_successful_reconnect(self, mock_discover):
        """Forces true extraction logic execution to test file reconstruction pipelines."""
        stream_bot.RECOVERY_MODE = "resume"
        mock_json_payload = '{"guild_id": 111, "channel_id": 222}'
        mock_channel = AsyncMock(spec=discord.VoiceChannel)
        mock_channel.name = "Recovered Channel"
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data=mock_json_payload)), \
             patch.object(self.bot, 'get_channel', return_value=mock_channel), \
             patch('discord.FFmpegPCMAudio'), \
             patch('discord.PCMVolumeTransformer'), \
             patch('builtins.print') as mock_print:
            await stream_bot.on_ready()
            mock_channel.connect.assert_called_once()
            mock_print.assert_any_call("🔄 [Recovery] State resume completed successfully.")

    @patch('stream_bot.discover_hardware_profile', return_value=('plughw:1,0', '2'))
    async def test_on_ready_resume_missing_channel_context(self, mock_discover):
        """Cleans disk space traces instantly if historical destinations are deleted."""
        stream_bot.RECOVERY_MODE = "resume"
        mock_json_payload = '{"guild_id": 111, "channel_id": 9999}'
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data=mock_json_payload)), \
             patch.object(self.bot, 'get_channel', return_value=None), \
             patch('stream_bot.clear_stream_state') as mock_clear:
            await stream_bot.on_ready()
            mock_clear.assert_called_once()

    @patch('stream_bot.discover_hardware_profile', return_value=('plughw:1,0', '2'))
    async def test_on_ready_resume_corrupt_json_trapping(self, mock_discover):
        """Ensures corrupted format layouts catch cleanly and wipe safely."""
        stream_bot.RECOVERY_MODE = "resume"
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data="{invalid_json_format")), \
             patch('stream_bot.clear_stream_state') as mock_clear:
            await stream_bot.on_ready()
            mock_clear.assert_called_once()

if __name__ == '__main__':
    unittest.main()

