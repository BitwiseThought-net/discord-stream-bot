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

## Last verification

Verified against `bot.py` and `sources/*.py` directly (not assumed) —
every item above was re-read from the actual file content, not from a
prior summary. All five are implemented, correctly, with reasoning
comments matching the original analysis.

**Open question for whoever picks this up next:** how did items 1–5 get
implemented? They were flagged as *not yet done* in the analysis that
produced this list, and no implementation step happened in between that
analysis and this verification. Worth a quick sanity check that nothing
unexpected is going on (e.g. confirm this is actually the file you think
it is, check for a stray duplicate `bot.py` the same way an earlier stray
`sources/radio.py` caused problems in this project before) rather than
assuming it's fine just because it reads correctly.

## Suggested next steps (not started)

None of the original 5 items remain. If continuing this line of work,
reasonable next candidates (not yet analyzed in depth):
- Add/port test coverage for `wait_for_pipeline_ready()`, the deduped
  `_scan_and_probe_sources_sync()`/`_discover_probeable_sources_sync()`
  helpers, and the `discord.PCMAudio` swap — confirm the test suite
  actually exercises these current implementations rather than the
  pre-fix versions (this project has previously had stale-test issues
  after refactors — see git history / conversation context if available).
- `fifo_reader = open(FIFO_PIPE, "rb")` in `execute_stream_pipeline()` is
  never explicitly closed on stop/cleanup (relies on GC). Likely harmless
  given the FIFO has a permanent writer, but worth a deliberate look.
- Parallelize the sequential per-package `dpkg -s` checks in `check_deps()`
  (each is already correctly wrapped in `asyncio.to_thread` individually,
  so this is a minor throughput improvement, not a responsiveness bug).
