"""Global pytest fixtures to let bot.py import cleanly.

bot.py executes *top-level* code at import time:
  - mkfifo(FIFO_PIPE)
  - os.open(FIFO_PIPE, O_RDWR|O_NONBLOCK) → PIPE_WRITE_HANDLE

The test suite sets DISCORD_TOKEN before importing, but the filesystem calls
still need stubbing or the tests crash.  This file is automatically loaded by
pytest for every test file in this directory.
"""

import os
import sys
from pathlib import Path

BASE_DIR = str(Path(__file__).parent.parent)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pytest


@pytest.fixture(autouse=True)
def _bot_stub(tmp_path):
    """Create a temp FIFO pipe so bot.py's import-time code does not crash.

    This fixture runs for every test.  It creates a temporary directory that
    mimics DATA_DIR (/data), creates the FIFO, and patches SOURCES_DIR so no
    real profile files interfere with tests.
    """
    # Redirect bot-level globals to temp locations
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    fifo = os.path.join(data_dir, "audio_pipe")
    if not os.path.exists(fifo):
        os.mkfifo(fifo)

    old_data_dir = os.environ.get("DATA_DIR")
    old_sources_dir = os.environ.get("SOURCES_DIR")
    old_fifo = os.environ.get("FIFO_PIPE")
    old_sources_cache = os.environ.get("SOURCES_CACHE_FILE")
    old_state_file = os.environ.get("STATE_FILE")

    os.environ["DATA_DIR"] = data_dir
    os.environ["FIFO_PIPE"] = fifo
    os.environ["SOURCES_DIR"] = str(tmp_path / "sources")
    os.makedirs(os.environ["SOURCES_DIR"], exist_ok=True)
    os.environ["SOURCES_CACHE_FILE"] = os.path.join(data_dir, "sources_cache.json")
    os.environ["STATE_FILE"] = os.path.join(data_dir, "state.json")

    yield

    # Restore environment after test
    if old_data_dir is not None:
        os.environ["DATA_DIR"] = old_data_dir
    elif "DATA_DIR" in os.environ:
        del os.environ["DATA_DIR"]

    if old_sources_dir is not None:
        os.environ["SOURCES_DIR"] = old_sources_dir
    elif "SOURCES_DIR" in os.environ:
        del os.environ["SOURCES_DIR"]

    if old_fifo is not None:
        os.environ["FIFO_PIPE"] = old_fifo
    elif "FIFO_PIPE" in os.environ:
        del os.environ["FIFO_PIPE"]

    if old_sources_cache is not None:
        os.environ["SOURCES_CACHE_FILE"] = old_sources_cache
    elif "SOURCES_CACHE_FILE" in os.environ:
        del os.environ["SOURCES_CACHE_FILE"]

    if old_state_file is not None:
        os.environ["STATE_FILE"] = old_state_file
    elif "STATE_FILE" in os.environ:
        del os.environ["STATE_FILE"]
