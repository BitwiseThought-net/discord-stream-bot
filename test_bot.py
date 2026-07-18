import os
import io
import sys
import json
import asyncio
import unittest
from unittest.mock import patch, mock_open, MagicMock, AsyncMock
from datetime import datetime, timedelta

# Lock fake environment variables securely before stream_bot imports
os.environ['DISCORD_TOKEN'] = 'mock_valid_token_xyz'
os.environ['COMMAND_BASE'] = 'radio'
os.environ['RECOVERY_MODE'] = 'resume'

import discord
import stream_bot

class TestDiscordStreamBotFullCoverage(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.intents = discord.Intents.default()
        self.bot = stream_bot.StreamBot(intents=self.intents)
        stream_bot.STATE_FILE = "/data/state.json"

    # =========================================================================
    # 1. CORE UTILITY LAYER TESTS
    # =========================================================================

    def test_environment_configurations(self):
        """Validates environment profile normalization."""
        self.assertEqual(stream_bot.COMMAND_NAME, 'radio')
        self.assertEqual(stream_bot.RECOVERY_MODE, 'resume')

    def test_save_stream_state_success(self):
        """Validates successful file I/O operations for state persistence."""
        m = mock_open()
        with patch('os.makedirs') as mock_make, patch('builtins.open', m):
            stream_bot.save_stream_state(111, 222)
            mock_make.assert_called_once_with('/data', exist_ok=True)
            m.assert_called_once_with('/data/state.json', 'w')

    def test_save_stream_state_exception(self):
        """Ensures exceptions inside save_stream_state are trapped safely."""
        with patch('os.makedirs', side_effect=Exception("Disk Error")):
            try:
                stream_bot.save_stream_state(111, 222)
            except Exception as e:
                self.fail(f"save_stream_state raised an unhandled exception: {e}")

    def test_clear_stream_state_exists(self):
        """Validates complete removal execution loops when historical file exists."""
        with patch('os.path.exists', return_value=True), patch('os.remove') as mock_rm:
            stream_bot.clear_stream_state()
            mock_rm.assert_called_once_with('/data/state.json')

    def test_clear_stream_state_exception(self):
        """Ensures exceptions inside clear_stream_state are safely caught."""
        with patch('os.path.exists', side_effect=Exception("File Error")):
            try:
                stream_bot.clear_stream_state()
            except Exception as e:
                self.fail(f"clear_stream_state raised an unhandled exception: {e}")

    # =========================================================================
    # 2. AUTOMATED HARDWARE DISCOVERY PATHWAY TESTS
    # =========================================================================

    def test_hardware_discovery_missing_base_dir(self):
        """Ensures fallback defaults map correctly if mount path is physically missing."""
        with patch('os.path.exists', return_value=False):
            dev, ch = stream_bot.discover_hardware_profile()
            self.assertEqual(dev, 'plughw:1,0')
            self.assertEqual(ch, '2')

    def test_hardware_discovery_mono_profile(self):
        """Forces true extraction path matching strict mono device configurations."""
        mock_data = "interface: usb audio\nchannels: 1 channel\n"
        with patch('os.path.exists', return_value=True), \
             patch('os.listdir', return_value=['card3']), \
             patch('os.path.isdir', return_value=True), \
             patch('builtins.open', mock_open(read_data=mock_data)):
            dev, ch = stream_bot.discover_hardware_profile()
            self.assertEqual(dev, 'plughw:3,0')
            self.assertEqual(ch, '1')

    def test_hardware_discovery_stereo_profile(self):
        """Forces true extraction path matching standard stereo capabilities."""
        mock_data = "interface: high-end usb card\nchannels: 2 channels\n"
        with patch('os.path.exists', return_value=True), \
             patch('os.listdir', return_value=['card4']), \
             patch('os.path.isdir', return_value=True), \
             patch('builtins.open', mock_open(read_data=mock_data)):
            dev, ch = stream_bot.discover_hardware_profile()
            self.assertEqual(dev, 'plughw:4,0')
            self.assertEqual(ch, '2')

    def test_hardware_discovery_exception_handling(self):
        """Ensures parse failures trigger structural fallbacks cleanly inside loops."""
        with patch('os.path.exists', return_value=True), \
             patch('os.listdir', return_value=['card5']), \
             patch('os.path.isdir', return_value=True), \
             patch('builtins.open', side_effect=Exception("Read Failure")):
            dev, ch = stream_bot.discover_hardware_profile()
            self.assertEqual(dev, 'plughw:1,0')
            self.assertEqual(ch, '2')


    # =========================================================================
    # 3. INTERACTION INTERFACE / SUBCOMMAND APPLICATION SLICES
    # =========================================================================

    def _create_mock_interaction(self, in_voice=True, streaming=True, is_transformer=True):
        """Helper matrix generating complex async mock interaction maps."""
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

    async def _get_subcommands(self):
        """Helper to safely isolate inner dynamic subcommand function pointers."""
        await self.bot.setup_hook()
        # Extract command tree objects manually from the synced group list
        cmd_group = self.bot.tree.get_commands()[0]
        return {cmd.name: cmd for cmd in cmd_group.commands}

    async def test_subcommand_start_not_in_voice(self):
        """Validates immediate block parameters if requesting user is out of voice."""
        cmds = await self._get_subcommands()
        interaction, _ = self._create_mock_interaction(in_voice=False)
        await cmds['start']._callback(interaction)
        interaction.response.send_message.assert_called_once_with(
            "You must be in a voice channel to start streaming!", ephemeral=True
        )

    async def test_subcommand_start_execution(self):
        """Traces the direct line startup path, verification saves, and encoder connections."""
        cmds = await self._get_subcommands()
        interaction, vc = self._create_mock_interaction(in_voice=True)
        
        with patch('stream_bot.discover_hardware_profile', return_value=('plughw:3,0', '1')), \
             patch('stream_bot.save_stream_state') as mock_save, \
             patch('discord.FFmpegPCMAudio'), \
             patch('discord.PCMVolumeTransformer'):
            
            await cmds['start']._callback(interaction)
            mock_save.assert_called_once_with(999, 555)
            vc.play.assert_called_once()

    async def test_subcommand_stop_active(self):
        """Validates clean teardowns, sleep cancellations, and manual footprint drops."""
        cmds = await self._get_subcommands()
        interaction, vc = self._create_mock_interaction(streaming=True)
        
        mock_task = MagicMock()
        self.bot.sleep_tasks[999] = mock_task
        
        with patch('stream_bot.clear_stream_state') as mock_clear:
            await cmds['stop']._callback(interaction)
            mock_task.cancel.assert_called_once()
            mock_clear.assert_called_once()
            vc.disconnect.assert_called_once()

    async def test_subcommand_stop_inactive(self):
        """Ensures safe execution returns if stop is called while client is completely idle."""
        cmds = await self._get_subcommands()
        interaction, _ = self._create_mock_interaction(streaming=False)
        await cmds['stop']._callback(interaction)
        interaction.response.send_message.assert_called_once_with(
            "I am not currently connected to a voice channel.", ephemeral=True
        )


    async def test_subcommand_volume_not_streaming(self):
        """Validates baseline locks if volume manipulation is issued when stream is dark."""
        cmds = await self._get_subcommands()
        interaction, _ = self._create_mock_interaction(streaming=False)
        await cmds['volume']._callback(interaction, percentage=50)
        interaction.response.send_message.assert_called_once_with(
            "The bot is not currently streaming!", ephemeral=True
        )

    async def test_subcommand_volume_invalid_transformer(self):
        """Ensures safety errors report cleanly if stream wrapper properties change."""
        cmds = await self._get_subcommands()
        interaction, _ = self._create_mock_interaction(streaming=True, is_transformer=False)
        await cmds['volume']._callback(interaction, percentage=75)
        interaction.response.send_message.assert_called_once_with(
            "Volume control wrapper not ready on this stream layout.", ephemeral=True
        )

    async def test_subcommand_volume_success(self):
        """Validates exact modifier floating scale mathematics inside operational ranges."""
        cmds = await self._get_subcommands()
        interaction, vc = self._create_mock_interaction(streaming=True, is_transformer=True)
        await cmds['volume']._callback(interaction, percentage=80)
        self.assertEqual(vc.source.volume, 0.8)

    # =========================================================================
    # 4. CLOCK CLUSTER / SLEEP & WAKE TASK SCHEDULERS COVERAGE
    # =========================================================================

    async def test_subcommand_sleep_disconnected(self):
        """Validates restriction if sleep parameters are target when idle."""
        cmds = await self._get_subcommands()
        interaction, _ = self._create_mock_interaction(streaming=False)
        await cmds['sleep']._callback(interaction, duration="15m")
        interaction.response.send_message.assert_called_once_with(
            "The bot must be connected to a voice channel to set a sleep timer!", ephemeral=True
        )

    async def test_subcommand_sleep_invalid_unit(self):
        """Verifies parsing rejections on unknown relative time designator structures."""
        cmds = await self._get_subcommands()
        interaction, _ = self._create_mock_interaction(streaming=True)
        await cmds['sleep']._callback(interaction, duration="10x")
        interaction.response.send_message.assert_called_once_with(
            "⚠️ Unrecognized duration unit. Please use seconds, minutes, or hours.", ephemeral=True
        )

    async def test_subcommand_sleep_invalid_format(self):
        """Verifies fallback protections match and flag complete garbage format metrics."""
        cmds = await self._get_subcommands()
        interaction, _ = self._create_mock_interaction(streaming=True)
        await cmds['sleep']._callback(interaction, duration="garbage_string")
        interaction.response.send_message.assert_called_once_with(
            "⚠️ Invalid time string format. Try inputs like `30m`, `1.5h`, or `11:45pm`.", ephemeral=True
        )

    async def test_subcommand_sleep_relative_execution(self):
        """Traces complete sleep scheduling, previous task overrides, and worker processing."""
        cmds = await self._get_subcommands()
        interaction, vc = self._create_mock_interaction(streaming=True)
        
        mock_old_task = MagicMock()
        self.bot.sleep_tasks[999] = mock_old_task
        
        with patch('asyncio.sleep', AsyncMock()) as mock_async_sleep, \
             patch('stream_bot.clear_stream_state') as mock_clear:
            
            await cmds['sleep']._callback(interaction, duration="2s")
            mock_old_task.cancel.assert_called_once()
            
            worker_task = self.bot.sleep_tasks[999]
            await worker_task
            
            mock_async_sleep.assert_called_with(2.0)
            mock_clear.assert_called_once()
            vc.disconnect.assert_called_once()

    async def test_subcommand_sleep_absolute_rollover(self):
        """Forces time math testing on timelines that cross boundaries into tomorrow."""
        cmds = await self._get_subcommands()
        interaction, _ = self._create_mock_interaction(streaming=True)
        
        frozen_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        with patch('stream_bot.datetime') as mock_dt, patch('asyncio.create_task'):
            mock_dt.now.return_value = frozen_now
            mock_dt.strptime.return_value = datetime.strptime("11:00AM", "%I:%M%p")
            
            await cmds['sleep']._callback(interaction, duration="11:00am")
            mock_dt.now.assert_called()


    async def test_subcommand_wake_not_in_voice(self):
        """Validates wake targeting blocking configurations if tracking target is out of voice."""
        cmds = await self._get_subcommands()
        interaction, _ = self._create_mock_interaction(in_voice=False)
        await cmds['wake']._callback(interaction, duration="1h")
        interaction.response.send_message.assert_called_once_with(
            "⚠️ You must be inside a voice channel when running this command so the bot knows where to connect!", ephemeral=True
        )

    async def test_subcommand_wake_invalid_unit(self):
        """Validates relative parsing filters on wake context frameworks."""
        cmds = await self._get_subcommands()
        interaction, _ = self._create_mock_interaction(in_voice=True)
        await cmds['wake']._callback(interaction, duration="50z")
        interaction.response.send_message.assert_called_once_with(
            "⚠️ Unrecognized wake duration unit. Use seconds, minutes, or hours.", ephemeral=True
        )

    async def test_subcommand_wake_invalid_format(self):
        """Validates structural fallback error mapping loops on absolute wake parameters."""
        cmds = await self._get_subcommands()
        interaction, _ = self._create_mock_interaction(in_voice=True)
        await cmds['wake']._callback(interaction, duration="bad_time_string")
        interaction.response.send_message.assert_called_once_with(
            "⚠️ Invalid wake time format. Try inputs like `10m`, `1h`, or `7:30am`.", ephemeral=True
        )

    async def test_subcommand_wake_execution_with_active_disconnect(self):
        """Traces wake loops, ensures pre-existing channels clear, and saves recovery states."""
        cmds = await self._get_subcommands()
        interaction, vc = self._create_mock_interaction(in_voice=True, streaming=True)
        
        mock_old_wake = MagicMock()
        self.bot.wake_tasks[999] = mock_old_wake

        with patch('asyncio.sleep', AsyncMock()), \
             patch('stream_bot.discover_hardware_profile', return_value=('plughw:1,0', '2')), \
             patch('stream_bot.save_stream_state') as mock_save, \
             patch('discord.FFmpegPCMAudio'), \
             patch('discord.PCMVolumeTransformer'):
            
            await cmds['wake']._callback(interaction, duration="1s")
            mock_old_wake.cancel.assert_called_once()
            
            worker = self.bot.wake_tasks[999]
            await worker
            
            vc.disconnect.assert_called_once()
            mock_save.assert_with(999, 555)

    async def test_subcommand_wake_worker_exception_catch(self):
        """Ensures that if the hardware worker fails inside wake loops, errors trap without crashing."""
        cmds = await self._get_subcommands()
        interaction, _ = self._create_mock_interaction(in_voice=True, streaming=False)
        target_channel = interaction.user.voice.channel
        target_channel.connect = AsyncMock(side_effect=Exception("Connection Crash Exception"))

        with patch('asyncio.sleep', AsyncMock()):
            await cmds['wake']._callback(interaction, duration="1s")
            worker = self.bot.wake_tasks[999]
            try:
                await worker
            except Exception as e:
                self.fail(f"Wake worker leaked a nested asynchronous crash line: {e}")


    # =========================================================================
    # 5. OS ENGINE/ EVENT RECOVERY AGENT HANDLER TESTS
    # =========================================================================

    @patch('stream_bot.discover_hardware_profile', return_value=('plughw:1,0', '2'))
    async def test_on_ready_stay_disconnected(self, mock_discover):
        """Validates boot configurations map cleanly when stay_disconnected is active."""
        stream_bot.RECOVERY_MODE = "stay_disconnected"
        with patch('builtins.print') as mock_print:
            await stream_bot.on_ready()
            mock_print.assert_any_call("🔄 [Recovery] Stay disconnected policy enforced. Skipping historical trace parsing loops.")

    @patch('stream_bot.discover_hardware_profile', return_value=('plughw:1,0', '2'))
    async def test_on_ready_resume_no_file(self, mock_discover):
        """Validates initialization loops when resume policy matches missing data states."""
        stream_bot.RECOVERY_MODE = "resume"
        with patch('os.path.exists', return_value=False), patch('builtins.print') as mock_print:
            await stream_bot.on_ready()
            mock_print.assert_any_call("🔄 [Recovery] Clean boot pipeline detected. No data traces saved to disk.")

    @patch('stream_bot.discover_hardware_profile', return_value=('plughw:1,0', '2'))
    async def test_on_ready_resume_successful_reconnect(self, mock_discover):
        """Validates direct file reconstruction loops and automatic hardware connection maps."""
        stream_bot.RECOVERY_MODE = "resume"
        mock_json_payload = '{"guild_id": 111, "channel_id": 222}'
        
        mock_channel = AsyncMock(spec=discord.VoiceChannel)
        mock_channel.name = "Recovered Channel"
        
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data=mock_json_payload)), \
             patch.object(stream_bot.bot, 'get_channel', return_value=mock_channel), \
             patch('discord.FFmpegPCMAudio'), \
             patch('discord.PCMVolumeTransformer'), \
             patch('builtins.print') as mock_print:
                 
            await stream_bot.on_ready()
            mock_channel.connect.assert_called_once()
            mock_print.assert_any_call("🔄 [Recovery] State resume completed successfully.")

    @patch('stream_bot.discover_hardware_profile', return_value=('plughw:1,0', '2'))
    async def test_on_ready_resume_missing_channel_context(self, mock_discover):
        """Validates target cleanup routines if old saved server profiles no longer exist."""
        stream_bot.RECOVERY_MODE = "resume"
        mock_json_payload = '{"guild_id": 111, "channel_id": 9999}'
        
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data=mock_json_payload)), \
             patch.object(stream_bot.bot, 'get_channel', return_value=None), \
             patch('stream_bot.clear_stream_state') as mock_clear:
                 
            await stream_bot.on_ready()
            mock_clear.assert_called_once()

    @patch('stream_bot.discover_hardware_profile', return_value=('plughw:1,0', '2'))
    async def test_on_ready_resume_corrupt_json_trapping(self, mock_discover):
        """Ensures corrupted formatting profiles within recovery storage clear safely."""
        stream_bot.RECOVERY_MODE = "resume"
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data="{invalid_json_format")), \
             patch('stream_bot.clear_stream_state') as mock_clear:
                 
            await stream_bot.on_ready()
            mock_clear.assert_called_once()

if __name__ == '__main__':
    unittest.main()

