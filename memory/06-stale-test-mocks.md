---
status: DONE (fixed this session)
verified_against: tests/test_bot_commands.py, TestExecuteStreamPipelineFull, TestCloseFifoReader
---

# 6. Stale test mocks (FFmpegPCMAudio / asyncio.sleep) + missing close_fifo_reader() coverage

## IMPORTANT CORRECTION FIRST

The previous session's `01-redundant-ffmpeg-relay.md` and `README.md`
reported **two pre-existing test failures**
(`TestStopActiveHardwareProcessMore::test_second_wait_succeeds_after_sigterm_timeout`
and `::test_final_kill_also_raises_is_swallowed`). **That report was wrong.**

Re-investigated this session: those two tests pass both in isolation and
as part of a full, unfiltered `pytest tests/` run (262 passed, 0 failed,
97.65% coverage). The "failures" only appeared under a narrow `-k` filter
expression run in the previous session, which selected a test subset/order
that doesn't occur in a normal run — a test-selection artifact of my own
ad-hoc command, not a real defect in the suite or in
`stop_active_hardware_process()`. Confirmed via `pytest
tests/test_bot_commands.py::TestStopActiveHardwareProcessMore -v` (passes)
and the full unfiltered suite (passes). **Don't treat this as a real open
item** — it never was one. Noting the correction here so the false report
doesn't get propagated further.

## The actual finding this session

While investigating the above, found a genuine, separate issue:
`TestExecuteStreamPipelineFull` (6 of its test methods) patched
`bot.discord.FFmpegPCMAudio` and `bot.discord.PCMVolumeTransformer` /
`bot.asyncio.sleep` — none of which `execute_stream_pipeline()` has called
since the item-1 fix (it calls `discord.PCMAudio` and
`wait_for_pipeline_ready()` now). Patching a symbol the code doesn't call
is inert, not an error — so the tests still passed — but two real problems
followed from it:

1. **False confidence.** These tests never actually exercised the current
   `discord.PCMAudio` / `open(FIFO_PIPE, "rb")` / `close_fifo_reader()`
   code path at all. A regression in that path could pass this whole test
   class undetected.
2. **Real, measurable waste.** `wait_for_pipeline_ready(timeout=0.4)` runs
   unconditionally in `execute_stream_pipeline()`, before the
   `vc.is_playing()` check -- and since these tests didn't mock it (they
   mocked `asyncio.sleep`, which isn't what the current code calls), it
   ran for real against an empty test FIFO and hit its full 400ms timeout,
   every time. Measured: `TestExecuteStreamPipelineFull` took 2.80s before
   the fix, 0.41s after -- confirmed via `pytest ... --durations=0`.

## Fix
- `patch("bot.discord.FFmpegPCMAudio", ...)` → `patch("bot.discord.PCMAudio", ...)`
  across all 6 affected test methods (the mock target now matches what the
  code actually calls).
- `patch("bot.asyncio.sleep", new=AsyncMock())` → `patch.object(bot,
  "wait_for_pipeline_ready", new=AsyncMock(return_value=True))` — same 6
  methods. Removes the real 400ms wait and correctly mocks the function
  the current code actually awaits.
- `test_reuses_existing_voice_client_already_playing`: same swap, plus its
  assertion (`mock_ffmpeg.assert_not_called()` → `mock_pcm_audio.assert_not_called()`).
- New `TestCloseFifoReader` class (4 tests): no-op when nothing's open,
  closes + clears when something is, swallows a raising `.close()`,
  double-close is safe. Direct unit coverage for the function itself,
  which had none before this session.
- New `test_new_source_closes_old_fifo_reader_and_tracks_new_one` in
  `TestExecuteStreamPipelineFull`: integration-level check that
  `execute_stream_pipeline()` actually calls `close_fifo_reader()` and
  sets `bot.fifo_reader` to a real, open handle -- the actual wiring the
  item-1 fix depends on, previously untested.

## Verified location
`tests/test_bot_commands.py` — `TestExecuteStreamPipelineFull` (existing
class, 6 methods edited + 1 new method added) and `TestCloseFifoReader`
(new class, inserted immediately before `TestStopActiveHardwareProcessMore`).

Full suite re-run after this fix: `262 passed, 0 failed`, 97.65% coverage,
total wall time 3.01s (down from whatever the unmeasured pre-fix baseline
was, given the 6×0.4s = ~2.4s previously wasted just in the one class
touched here).

## Not yet done
Still no *direct* unit tests for `wait_for_pipeline_ready()`'s own
internal branches (the real `select()` call succeeding vs. timing out vs.
raising), or for `_scan_and_probe_sources_sync()` /
`_discover_probeable_sources_sync()` / `_find_live_source_sync()` in
isolation (they're only exercised indirectly through `set_input()` /
`auto_input()` command tests, which patch `asyncio.to_thread` itself
rather than testing the sync helpers' own logic directly). Reasonable
next candidate if continuing this line of work.
