"""Covers bot.py's module-import-time guard branches (missing DISCORD_TOKEN,
FIFO pipe creation failure, pipe-open failure) by importing the module in a
*fresh* subprocess with deliberately broken environment/paths. These branches
run once at import time in the real process, before any test fixture gets a
chance to patch them, so they can't be exercised via the normal `import bot`
that conftest.py already performed for every other test module -- a clean
interpreter is required.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = str(Path(__file__).parent.parent)


def _run_bot_import(env_overrides, extra_setup="", drop_keys=None):
    """Run a tiny script in a fresh Python process that sets up the given
    env vars and then `import bot`. Returns the completed process."""
    script = f"""
import sys
sys.path.insert(0, {BASE_DIR!r})
{extra_setup}
import bot
"""
    env = dict(os.environ)
    for key in (drop_keys or []):
        env.pop(key, None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=15, env=env,
    )


class TestMissingDiscordToken:
    def test_exits_with_critical_error(self, tmp_path):
        env = {
            "DATA_DIR": str(tmp_path / "data"),
            "SOURCES_DIR": str(tmp_path / "sources"),
        }
        result = _run_bot_import(env, drop_keys=["DISCORD_TOKEN"])
        assert result.returncode == 1
        assert "DISCORD_TOKEN environment variable is missing" in result.stdout


class TestFifoPipeCreationFailure:
    def test_mkfifo_failure_then_pipe_open_failure_exits(self, tmp_path):
        """Point FIFO_PIPE's parent directory at a path that's actually a
        *file*, so os.makedirs(dirname) raises inside the mkfifo try/except
        (covering the 'Failed to construct' warning branch) -- and because
        the FIFO never gets created, the subsequent os.open() also fails,
        covering the final sys.exit(1) branch too."""
        blocker = tmp_path / "blocker_file"
        blocker.write_text("not a directory")
        fifo_path = str(blocker / "sub" / "audio_pipe")

        env = {
            "DISCORD_TOKEN": "fake-token",
            "DATA_DIR": str(tmp_path / "data"),
            "SOURCES_DIR": str(tmp_path / "sources"),
            "FIFO_PIPE": fifo_path,
            "STATE_FILE": str(tmp_path / "data" / "state.json"),
            "SOURCES_CACHE_FILE": str(tmp_path / "data" / "sources_cache.json"),
        }
        result = _run_bot_import(env)

        assert "Failed to construct native FIFO audio stream buffer" in result.stdout
        assert "Failed to secure persistent global pipe handles" in result.stdout
        assert result.returncode == 1
