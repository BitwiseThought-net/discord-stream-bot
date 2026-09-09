---
status: DONE (verified)
verified_against: bot.py, set_input(), auto_input(), _scan_and_probe_sources_sync(), _discover_probeable_sources_sync(), _find_live_source_sync()
---

# 3. Hardware discovery blocked the whole bot's event loop

## Problem
`discover_hardware_profile()` and `scan_sources_for_signal()` call straight
into blocking `subprocess.run()` calls (e.g. each ALSA card's
`probe_signal()` runs `arecord` for ~0.3s+ plus process overhead). These
were called directly inside `async def set_input()` / `async def
auto_input()` with **no** `asyncio.to_thread()` wrapping — unlike
`check_deps()`, which already correctly wraps every blocking call
(`await asyncio.to_thread(is_package_installed, package)` etc.). Since this
ran directly on the asyncio event loop, `/radio input` (list mode) and
`/radio auto` froze the *entire bot process* — every guild, every other
command, Discord gateway heartbeats — for the full scan duration (over a
second with 4 mics).

## Fix
Blocking work extracted into plain sync helper functions, each called via
a single `await asyncio.to_thread(...)`:

- `_scan_and_probe_sources_sync()` — used by `set_input()`'s list mode.
  Loads modules once, runs `discover_hardware_profile(modules=modules)`
  then `scan_sources_for_signal(sources, modules=modules)` against that
  same load (also fixes item #4 — see that file).
- `_discover_probeable_sources_sync()` and `_find_live_source_sync()` —
  used by `auto_input()`, same pattern.

Call sites now read like:
```python
sources, signal_map = await asyncio.to_thread(_scan_and_probe_sources_sync)
```
and
```python
modules, probeable_sources = await asyncio.to_thread(_discover_probeable_sources_sync)
...
live_source, probe_errors = await asyncio.to_thread(_find_live_source_sync, probeable_sources, modules)
```

## Verified location
`bot.py`, section "5. DEVICE CATALOG SELECTION & DYNAMIC TUNING COMMANDS" —
`_scan_and_probe_sources_sync()`, `set_input()`, `_discover_probeable_sources_sync()`,
`_find_live_source_sync()`, `auto_input()`. Each helper's docstring states
the to_thread rationale explicitly.

## Not yet done
`execute_stream_pipeline()`'s cold-start path (`if not
os.path.exists(SOURCES_CACHE_FILE): await
asyncio.to_thread(discover_hardware_profile)`) is already wrapped too —
confirmed, nothing outstanding here either.
