---
last_updated_by: LLM session, analysis pass + fix pass (efficiency + streaming latency)
verified_against: full repo checkout, bot.py + sources/*.py + tests/
---

# Project status

## What this project is
A containerized Discord bot (`bot.py`, ~1365 lines, single file) that
captures continuous audio from a pluggable hardware/software source
(USB mic via ALSA, RTL-SDR radio/aircraft/satellite bands, or a built-in
test tone) and streams it live into a Discord voice channel. Sources are
self-contained plugins under `sources/*.py` (see the "SOURCE PLUGIN
CONTRACT" docstring at the top of `bot.py` - read that before touching
anything source-related, it's the load-bearing abstraction of the whole
project). Slash commands live under one `app_commands.Group` (`/radio ...`
by default, name configurable).

## Test suite
262 tests, all passing as of this session (`DISCORD_TOKEN=x python3 -m
pytest tests/ -q`), 97.65% coverage. Note: an earlier note (see
`01-redundant-ffmpeg-relay.md`) had flagged
`TestStopActiveHardwareProcessMore::test_second_wait_succeeds_after_sigterm_timeout`
and `::test_final_kill_also_raises_is_swallowed` as failing on that
checkout. Both pass now - either environment-specific flakiness or fixed
incidentally since. Re-verify if you see them fail again; don't assume the
old note is still accurate.

## Done
- **File 01** - Removed a redundant second ffmpeg process that was
  relaying already Discord-ready PCM straight through unchanged (pure
  passthrough). Now uses `discord.PCMAudio` directly on a buffered FIFO
  read. Also fixed a real correctness bug found while verifying it: a
  FIFO double-reader race window on `/radio restart`, closed with a new
  `close_fifo_reader()` helper called synchronously before any new reader
  opens.
- **File 02** - `stop_active_hardware_process()`/
  `spawn_hardware_capture_stream()` calls (up to ~2s of blocking
  `proc.wait()` per stopped process) were running directly on the asyncio
  event loop from five call sites (`execute_stream_pipeline()`, `stop()`,
  `restart()`, `sleep_timer_worker()`, `execute_channel_scan()`). All five
  now go through `asyncio.to_thread(...)`, matching the pattern already
  used elsewhere in the file. Verified: compiles, full test suite still
  262/262 passing at 97.65% coverage. See file 02's `## UPDATE` section
  for what's still *not* verified (no dedicated concurrency-regression
  test added, no live-gateway confirmation).
- **File 03** - All three SDR sources (`sdr_aircraft.py`,
  `sdr_satellite.py`, `sdr_radio.py`) had a trailing ffmpeg stage that did
  nothing but mono-to-stereo upmix at an already-correct sample rate.
  Merged into a single `sox ... remix 1 1` call in each, dropping ffmpeg
  from the pipeline (and from `REQUIRED_PACKAGES`/docstrings) in all
  three files. Verified: the merged `sox` command was actually run against
  synthetic PCM (not just reasoned about) and confirmed byte-exact
  mono-to-stereo duplication at the correct output rate/size with dither
  off; full test suite still 262/262 passing. See file 03's `## UPDATE`
  section for what's still not verified (no real RTL-SDR hardware/RF
  input available in this environment - only synthetic data was used).

## Nothing currently open from this session's analysis
Files 02 and 03 (the two priority items) are both fixed and verified as
far as this environment allows. File 04 remains open but was always
flagged low-priority / optional - see below.

## File 04 - open, low priority, optional
Smaller items not touched this session (deliberately - none are
performance-critical enough to prioritize over getting 02/03 verified):
a `Dockerfile` env var typo, scattered duplicate `STATE_FILE` reads, and a
couple of things worth a comment/test rather than a code change. Pick up
whenever convenient; each sub-item in that file is independent.

## Things NOT to re-litigate
- The Discord-facing PCM path (`discord.PCMAudio` direct on the FIFO,
  `close_fifo_reader()` synchronization) - see file 01, already reasoned
  through carefully and verified.
- The `killpg`-based process-group teardown in
  `stop_active_hardware_process()` - the *mechanism* is correct and
  necessary (shell pipelines fork children that `terminate()` on the
  `Popen` handle alone would orphan); file 02 changed *where it runs*
  (event loop thread vs. worker thread via `asyncio.to_thread`), not the
  mechanism itself.
- The `sox remix 1 1` mono-to-stereo upmix in the three SDR sources - see
  file 03, verified byte-exact against synthetic data with dither off.
  Don't assume it needs re-deriving; if picking this area back up, the
  open item is the *real-hardware* ear-check noted in file 03, not the
  command's correctness.

## Next session, if continuing this line of work
Nothing urgent is queued. If you want more to do here: file 04's items,
or a fresh pass looking for other places blocking work might be running
on the event loop (file 02 was found by grepping for the two specific
function names already known to be slow - a broader audit of every
`subprocess`/`os.wait`-style call in `bot.py` and `actions/scan_range.py`
against whether it's `to_thread`-wrapped hasn't been done).
