# Latency/Efficiency Work — Status Tracker

This folder tracks a set of efficiency/latency findings from an audit of the
streaming pipeline (`bot.py` + `sources/*.py`). Each file is one finding:
what the problem was, the fix, and **verified status as of the last check**.

Read `status` at the top of each file before doing anything — this list
exists specifically so a session picking this up doesn't redo work that's
already landed, or skip something that's still open.

## Index

| # | File | Status |
|---|------|--------|
| 1 | [01-redundant-ffmpeg-relay.md](01-redundant-ffmpeg-relay.md) | ✅ DONE |
| 2 | [02-sdr-pipeline-simplification.md](02-sdr-pipeline-simplification.md) | ✅ DONE |
| 3 | [03-async-safe-hardware-discovery.md](03-async-safe-hardware-discovery.md) | ✅ DONE |
| 4 | [04-dedupe-source-module-reloads.md](04-dedupe-source-module-reloads.md) | ✅ DONE |
| 5 | [05-pipeline-ready-polling.md](05-pipeline-ready-polling.md) | ✅ DONE |
| 6 | [06-stale-test-mocks.md](06-stale-test-mocks.md) | ✅ DONE |

## Last verification

Full unfiltered suite: `pytest tests/` → **262 passed, 0 failed, 97.65%
coverage**, 3.01s wall time. Re-run at the end of this session.

## ⚠️ Correction to a previous report

An earlier version of this README (and of `01-redundant-ffmpeg-relay.md`)
claimed two pre-existing test failures in `TestStopActiveHardwareProcessMore`.
**That was wrong.** See `06-stale-test-mocks.md` for the full correction —
those two tests pass both in isolation and in a full unfiltered run; the
"failure" was an artifact of a narrow `-k` filter expression, not a real
defect. Don't propagate that report further.

## Open question, still unresolved

How did items 1–5 get implemented in the first place? They were flagged as
*not yet done* in the analysis that produced this list, and no
implementation step happened in between that analysis and first
verification. Still worth a sanity check on your end that nothing
unexpected is going on (confirm this is actually the file/repo you think
it is — this project previously had a stray `sources/radio.py` cause the
wrong code to run silently) rather than assuming it's fine because it
reads correctly.

## Suggested next steps (not started)

- Direct unit tests for `wait_for_pipeline_ready()`'s own internal
  branches (real `select()` success / timeout / exception), and for
  `_scan_and_probe_sources_sync()` / `_discover_probeable_sources_sync()` /
  `_find_live_source_sync()` in isolation — currently only exercised
  indirectly through `set_input()`/`auto_input()` command tests. See
  `06-stale-test-mocks.md`'s "Not yet done" for detail.
- Parallelize the sequential per-package `dpkg -s` checks in `check_deps()`
  (each is already correctly wrapped in `asyncio.to_thread` individually,
  so this is a minor throughput improvement, not a responsiveness bug).

## Follow-up fixes landed, by session

**Session 2:** `fifo_reader` explicit-close fix (`close_fifo_reader()`) —
see "UPDATE — fixed" in `01-redundant-ffmpeg-relay.md`.

**Session 3:** Stale test mocks in `TestExecuteStreamPipelineFull` fixed
(were patching `FFmpegPCMAudio`/`asyncio.sleep`, neither of which the
current code calls — silently gave false confidence AND wasted ~2.4s of
real 400ms timeouts per test run), plus new direct test coverage for
`close_fifo_reader()` and its integration into `execute_stream_pipeline()`.
Also corrected the false test-failure report from session 2. See
`06-stale-test-mocks.md`.
