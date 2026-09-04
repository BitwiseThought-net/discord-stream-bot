"""Global pytest fixtures to let bot.py import cleanly.

bot.py executes top-level code at import time:
  - os.makedirs(SOURCES_DIR, exist_ok=True)
  - mkfifo(FIFO_PIPE)
  - os.open(FIFO_PIPE, O_RDWR|O_NONBLOCK) → PIPE_WRITE_HANDLE

We set DATA_DIR / SOURCES_DIR / FIFO_PIPE env vars *here* at module load
time so they are visible when test_bot.py does `import bot`.
"""

import os
import sys
from pathlib import Path

BASE_DIR = str(Path(__file__).parent.parent)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# -- redirect bot-level globals to a temp dir BEFORE importing any test code ----
_TMP_DIR = Path("/tmp/pytest-discord-stream-bot-data")
_TMP_DIR.mkdir(parents=True, exist_ok=True)
_DATA_DIR = _TMP_DIR / "data"
_DATA_DIR.mkdir(exist_ok=True)
_FIFO_PIPE = str(_DATA_DIR / "audio_pipe")
_SOURCES_DIR = str(_TMP_DIR / "sources")
os.makedirs(_SOURCES_DIR, exist_ok=True)

# FIFO pipe required by bot.py's import-time os.mkfifo()
if not os.path.exists(_FIFO_PIPE):
    os.mkfifo(_FIFO_PIPE)

os.environ.setdefault("DATA_DIR", str(_DATA_DIR))
os.environ.setdefault("FIFO_PIPE", _FIFO_PIPE)
os.environ.setdefault("SOURCES_DIR", _SOURCES_DIR)
os.environ.setdefault("SOURCES_CACHE_FILE", str(_DATA_DIR / "sources_cache.json"))
os.environ.setdefault("STATE_FILE", str(_DATA_DIR / "state.json"))
