"""
Pytest configuration.

bot.py writes to a handful of absolute, production-only paths
(/data, /sources) at import time (state file, sources cache, the audio
FIFO pipe, and the sources directory). Those paths only exist/are
writable inside the project's Docker container. To let the test suite
run in any environment (local machine, CI runner, etc.) without root
or a container, point those paths at a temporary, writable directory
before bot is imported.
"""
import os
import tempfile

_TEST_DATA_ROOT = tempfile.mkdtemp(prefix="bot_test_")

os.environ.setdefault("DATA_DIR", os.path.join(_TEST_DATA_ROOT, "data"))
os.environ.setdefault("SOURCES_DIR", os.path.join(_TEST_DATA_ROOT, "sources"))
os.environ.setdefault(
    "STATE_FILE", os.path.join(os.environ["DATA_DIR"], "state.json")
)
os.environ.setdefault(
    "SOURCES_CACHE_FILE", os.path.join(os.environ["DATA_DIR"], "sources_cache.json")
)
os.environ.setdefault(
    "FIFO_PIPE", os.path.join(os.environ["DATA_DIR"], "audio_pipe")
)