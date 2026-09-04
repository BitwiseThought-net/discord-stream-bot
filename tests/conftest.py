"""Global pytest fixtures to let bot.py import cleanly.

bot.py executes top-level code at import time:
  - os.makedirs(SOURCES_DIR, exist_ok=True)
  - mkfifo(FIFO_PIPE)
  - os.open(FIFO_PIPE, O_RDWR|O_NONBLOCK) → PIPE_WRITE_HANDLE

We provide ``pytest_fixture_bot_env`` so every test gets its own
isolated temp dir with all required env vars set.
"""

import os
import shutil
import tempfile
from pathlib import Path

# Module-level constant used at module load to build the fake FIFO pipe.
# The fixture below *also* sets these env vars, so both paths work:
# 1. Tests that ``import bot`` directly (module load runs first)
# 2. Tests that use ``pytest_fixture_bot_env`` before importing.

# -- temporary directory created at conftest load time ------------------------
_TMP_DIR = tempfile.mkdtemp(prefix="pytest-discord-stream-bot-")

_DATA_DIR = Path(_TMP_DIR) / "data"
_DATA_DIR.mkdir()

_FIFO_PIPE = str(_DATA_DIR / "audio_pipe")
_SOURCES_DIR = str(Path(_TMP_DIR) / "sources")
os.makedirs(_SOURCES_DIR, exist_ok=True)

# -- create the FIFO pipe bot.py expects at import time -----------------------
if not os.path.exists(_FIFO_PIPE):
    os.mkfifo(_FIFO_PIPE)

# -- set env vars so bot.py's os.getenv() picks up our temp paths -----------
os.environ.setdefault("DATA_DIR", str(_DATA_DIR))
os.environ.setdefault("FIFO_PIPE", _FIFO_PIPE)
os.environ.setdefault("SOURCES_DIR", _SOURCES_DIR)
os.environ.setdefault("SOURCES_CACHE_FILE", str(_DATA_DIR / "sources_cache.json"))
os.environ.setdefault("STATE_FILE", str(_DATA_DIR / "state.json"))

# -- bot.py requires DISCORD_TOKEN at import time -----------------------------
os.environ.setdefault("DISCORD_TOKEN", "fake-token-for-testing")


# --------------------------------------------------------------------------- #
#  Session-scoped fixture: every test gets its own temp directory             #
# --------------------------------------------------------------------------- #

import pytest


@pytest.fixture(scope="session", autouse=True)
def _pytest_bot_env() -> None:
    """Set env vars to isolated temp paths so bot.py doesn't clobber host dirs.

    Because conftest runs *before* any test import, the module-level globals
    above already set these values. This fixture is a safety net for tests that
    ``unset`` them and re-import (e.g. after patching).  It also ensures the
    FIFO pipe exists in the temp dir even if pytest reruns collection.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pytest-discord-stream-bot-")
    data_dir = Path(tmp_dir) / "data"
    data_dir.mkdir()

    fifo = str(data_dir / "audio_pipe")
    sources_dir = str(Path(tmp_dir) / "sources")
    os.makedirs(sources_dir, exist_ok=True)

    if not os.path.exists(fifo):
        os.mkfifo(fifo)

    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["FIFO_PIPE"] = fifo
    os.environ["SOURCES_DIR"] = sources_dir
    os.environ["SOURCES_CACHE_FILE"] = str(data_dir / "sources_cache.json")
    os.environ["STATE_FILE"] = str(data_dir / "state.json")
    os.environ["DISCORD_TOKEN"] = "fake-token-for-testing"

    yield

    # cleanup at session end
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
#  Helper for tests that need yet *another* isolated env (per-test scope).    #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def bot_env(tmp_path: Path):
    """Return a context-manager-like object that patches env vars to *tmp_path*.

    Usage::

        def test_something(bot_env):
            with bot_env():
                importlib.reload(bot)  # works!
    """
    tmp_data = tmp_path / "bot_data"
    tmp_data.mkdir()
    fifo = str(tmp_data / "audio_pipe")
    sources_dir = str(tmp_data / "sources")
    os.makedirs(sources_dir, exist_ok=True)

    if not os.path.exists(fifo):
        os.mkfifo(fifo)

    old_env: dict[str, str | None] = {
        "DATA_DIR": os.environ.get("DATA_DIR"),
        "FIFO_PIPE": os.environ.get("FIFO_PIPE"),
        "SOURCES_DIR": os.environ.get("SOURCES_DIR"),
        "SOURCES_CACHE_FILE": os.environ.get("SOURCES_CACHE_FILE"),
        "STATE_FILE": os.environ.get("STATE_FILE"),
    }

    os.environ["DATA_DIR"] = str(tmp_data)
    os.environ["FIFO_PIPE"] = fifo
    os.environ["SOURCES_DIR"] = sources_dir
    os.environ["SOURCES_CACHE_FILE"] = str(tmp_data / "sources_cache.json")
    os.environ["STATE_FILE"] = str(tmp_data / "state.json")

    yield

    # restore
    for k, v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
