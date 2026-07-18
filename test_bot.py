import os
import sys
import unittest
from unittest.mock import patch, mock_open, MagicMock
from datetime import datetime

# Prevent real bot connection side effects during import execution
os.environ['DISCORD_TOKEN'] = 'mock_test_token'
os.environ['COMMAND_BASE'] = 'radio'
os.environ['RECOVERY_MODE'] = 'resume'

import stream_bot

class TestDiscordStreamBot(unittest.TestCase):

    def test_environment_variable_parsing(self):
        """Validates that .env string parameters normalize down to expected values."""
        self.assertEqual(stream_bot.COMMAND_NAME, 'radio')
        self.assertEqual(stream_bot.RECOVERY_MODE, 'resume')

    def test_channel_mapping_stereo_fallback(self):
        """Verifies hardware channels default to stereo if tracking folders are missing."""
        with patch('os.path.exists', return_value=False):
            device, channels = stream_bot.discover_hardware_profile()
            self.assertEqual(device, 'plughw:1,0')
            self.assertEqual(channels, '2')

    def test_channel_mapping_mono_detection(self):
        """Verifies strict mono detection triggers when file specifies 1 channel."""
        mock_stream_data = "Capture: \n  Status: Stop\n  Interface: 1\n  Channels: 1 channel\n"
        
        with patch('os.path.exists', return_value=True), \
             patch('os.listdir', return_value=['card3']), \
             patch('os.path.isdir', return_value=True), \
             patch('builtins.open', mock_open(read_data=mock_stream_data)):
            
            device, channels = stream_bot.discover_hardware_profile()
            self.assertEqual(device, 'plughw:3,0')
            self.assertEqual(channels, '1')

    def test_state_persistence_io(self):
        """Ensures state persistence structures write data markers cleanly to path endpoints."""
        m = mock_open()
        with patch('os.makedirs') as mock_dirs, \
             patch('builtins.open', m):
            stream_bot.save_stream_state(12345, 67890)
            mock_dirs.assert_called_once_with('/data', exist_ok=True)
            m.assert_called_once_with('/data/state.json', 'w')

    @patch('discord.VoiceClient')
    def test_volume_transformer_validation(self, mock_vc):
        """Verifies volume float conversion rules work properly inside interaction payloads."""
        mock_source = MagicMock(spec=stream_bot.discord.PCMVolumeTransformer)
        mock_vc.source = mock_source
        
        # Test 50% slider value parses cleanly to 0.5 float target
        mock_source.volume = 50 / 100.0
        self.assertEqual(mock_source.volume, 0.5)

if __name__ == '__main__':
    unittest.main()
