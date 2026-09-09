---
status: DONE (verified)
verified_against: bot.py, load_source_modules(), discover_hardware_profile(), scan_sources_for_signal()
---

# 4. Redundant `load_source_modules()` reloads within a single command

## Problem
`load_source_modules()` deliberately re-reads and re-`exec`s every
`sources/*.py` file from scratch on every call (intentional, for hot-reload
— not itself the bug). But `set_input()`'s list mode called
`discover_hardware_profile()` (which loads all modules internally), then
separately called `scan_sources_for_signal(sources)` with no `modules=`
argument, forcing a *second* full reload of every source file within the
same command. Same pattern in `auto_input()`.

## Fix
`discover_hardware_profile()` and `scan_sources_for_signal()` both now
accept an optional `modules` param and only call `load_source_modules()`
themselves if it's omitted:

```python
def discover_hardware_profile(modules=None):
    ...
    if modules is None:
        modules = load_source_modules()
    ...

def scan_sources_for_signal(sources, modules=None):
    if modules is None:
        modules = load_source_modules()
    ...
```

Callers now load once and thread the same dict through both calls — see
`_scan_and_probe_sources_sync()` and `_discover_probeable_sources_sync()`
in item #3's file (same helpers implement both fixes together).

## Verified location
`bot.py` — `discover_hardware_profile(modules=None)` and
`scan_sources_for_signal(sources, modules=None)` signatures, both with
docstrings explicitly stating this is to avoid the double-load. Confirmed
call sites (`_scan_and_probe_sources_sync`, `_discover_probeable_sources_sync`)
pass `modules=modules` through to both.

## Not yet done
Nothing outstanding — this was a narrow, mechanical fix and it's fully
applied everywhere `discover_hardware_profile`/`scan_sources_for_signal`
are called together. (The deliberate "reload from disk every top-level
call" behavior for hot-reload purposes was intentionally left alone, per
the original analysis — that's a feature, not part of this fix.)
