---
status: DONE (fixed this session)
found_during: efficiency + streaming-latency review, this session
verified_against: bot.py (functions cited below, current checkout)
---

# 2. Blocking process-teardown calls run directly on the asyncio event loop

## Problem
`stop_active_hardware_process()` (bot.py, around line 536) tears down up to
three subprocess handles (`ffmpeg_process`, `sox_process`,
`hardware_process`). For each one it currently is, it does:

```python
os.killpg(pgid, signal.SIGTERM)
proc.wait(timeout=1.0)          # blocking, up to 1s
# on failure:
os.killpg(pgid, signal.SIGKILL)
proc.wait(timeout=1.0)          # blocking, up to another 1s
```

`proc.wait(timeout=...)` is a real blocking call (it's `subprocess.Popen`,
not `asyncio.subprocess`) - worst case this is ~2 seconds of the Python
interpreter thread doing nothing but waiting on a syscall, once per
tracked process attribute that's currently set.

This function is called from:
- `spawn_hardware_capture_stream()` (line ~588), itself called directly
  (not via `asyncio.to_thread`) from `execute_stream_pipeline()`, which is
  `async def` and runs on the bot's event loop.
- `stop()` command handler (line ~755), directly, no `to_thread`.
- `restart()` command handler (line ~775), directly, no `to_thread`.

Because this is a single-process `discord.Client` with one event loop,
blocking that thread for up to ~2 seconds blocks *everything* else the bot
is doing at that moment: the Discord gateway's heartbeat/heartbeat-ACK
handling, every other guild's slash commands, voice websocket keepalives,
all of it - not just the guild that issued the `/radio stop` or
`/radio restart`. Discord's gateway will eventually consider a
sufficiently-delayed heartbeat ACK a dead connection and force a
reconnect; even short of that threshold, this is directly-felt latency on
exactly the commands users are most likely to notice (start/stop/restart),
and it's the same story every time a source switch or frequency change
re-runs this path.

## Why this stands out
Everywhere else in `bot.py`, blocking work is deliberately wrapped in
`asyncio.to_thread(...)` before being awaited - this is a consistent,
clearly-intentional pattern in this codebase, not something the author
overlooked in general:

```python
readable, _, _ = await asyncio.to_thread(select.select, [GLOBAL_FIFO_FD], [], [], timeout)   # wait_for_pipeline_ready()
await asyncio.to_thread(discover_hardware_profile)                                            # execute_stream_pipeline()
sources, signal_map = await asyncio.to_thread(_scan_and_probe_sources_sync)                   # set_input()
allowlist = await asyncio.to_thread(load_package_allowlist)                                   # check_deps()
already_installed = await asyncio.to_thread(is_package_installed, package)                    # check_deps()
installed_ok = await asyncio.to_thread(install_package, package)                               # check_deps()
modules, probeable_sources = await asyncio.to_thread(_discover_probeable_sources_sync)         # auto_input()
live_source, probe_errors = await asyncio.to_thread(_find_live_source_sync, ...)              # auto_input()
catalog = await asyncio.to_thread(scan_fn, start_hz, end_hz)                                   # execute_channel_scan()
```

`stop_active_hardware_process()` / `spawn_hardware_capture_stream()` are
the one place with a genuinely multi-second blocking call
(`proc.wait(timeout=1.0)`, possibly twice) that *isn't* following this
pattern. Everything else being `to_thread`-wrapped makes this one omission
easy to miss by inspection (it doesn't stand out as "the async function
with the sync body" the way it would in a codebase that mixed styles
throughout) but also means it's a one-line-per-call-site fix, not an
architectural change - the pattern to copy already exists three times over
in the same file.

## Suggested fix
Wrap the three call sites:

```python
# execute_stream_pipeline(), where it currently does:
spawn_hardware_capture_stream(active_source)
# ->
await asyncio.to_thread(spawn_hardware_capture_stream, active_source)
```

```python
# stop(), where it currently does:
stop_active_hardware_process()
# ->
await asyncio.to_thread(stop_active_hardware_process)
```

```python
# restart(), where it currently does:
stop_active_hardware_process()
# ->
await asyncio.to_thread(stop_active_hardware_process)
```

`spawn_hardware_capture_stream()` itself calls `stop_active_hardware_process()`
internally too (line ~588) - wrapping the outer call in
`execute_stream_pipeline()` covers that inner call for free since it all
runs in the same worker thread. No need to also wrap it separately inside
`spawn_hardware_capture_stream()`.

## Things to check before/while fixing
- `bot.hardware_process` / `sox_process` / `ffmpeg_process` /
  `bot.fifo_reader` are read and written from both the event loop
  (elsewhere) and would now also be written from a thread-pool worker
  thread during the `to_thread` call. This is very likely fine in
  practice - `asyncio.to_thread` runs the callable in a `ThreadPoolExecutor`
  and nothing else touches these specific attributes concurrently with the
  teardown (the whole point of `stop_active_hardware_process` is that nothing
  should be using them while it runs) - but worth a second look if a race
  seems possible, since this file otherwise has zero threading and this
  would be the first place two threads share mutable bot state.
- Existing tests around `stop_active_hardware_process()` (see
  `tests/test_bot.py`, `TestStopActiveHardwareProcessMore` and friends)
  call it directly/synchronously today. If the call sites move to
  `to_thread`, the function itself doesn't need to change (it stays a
  plain sync function), so those tests should keep working unmodified -
  confirm that's actually true once the change is made, don't assume.
- No test currently exercises "does `stop()`/`restart()` actually await
  the thread instead of blocking" - if adding coverage for this fix,
  that's the behavior to target (e.g. assert the coroutine yields control
  around the call), not just that `stop_active_hardware_process()` still
  gets called.

## Not investigated this session
Whether Discord's gateway has actually dropped a session because of this
in production - this is inferred from reading the code against known
asyncio/gateway heartbeat semantics, not from a captured incident. Worth
noting in a commit message as "should reduce X" rather than "fixes
observed disconnects" unless someone has logs showing the latter.

---

## UPDATE - fixed

**status: DONE (fixed this session)**

Wrapped every call site in `asyncio.to_thread(...)`, matching the pattern
already used elsewhere in `bot.py`. Ended up being five call sites, not
the three originally scoped above - a second pass over the file (grepping
for `stop_active_hardware_process()` and `spawn_hardware_capture_stream()`)
turned up two more with the identical problem that weren't caught in the
first read-through:

- `execute_stream_pipeline()` - `spawn_hardware_capture_stream(active_source)`
  -> `await asyncio.to_thread(spawn_hardware_capture_stream, active_source)`.
  This covers `spawn_hardware_capture_stream()`'s own internal call to
  `stop_active_hardware_process()` for free, since it now all runs in the
  same worker thread - no separate wrap needed inside that function.
- `stop()` command handler - same pattern.
- `restart()` command handler - same pattern.
- `sleep_timer_worker()` - **not in the original scope above**, found on
  the second pass. Fires from a background `asyncio` task when a sleep
  timer expires, on the same event loop as everything else, so the same
  ~2s stall risk applied here too.
- `execute_channel_scan()` - **not in the original scope above**, found on
  the second pass. Pauses an active pipeline before a frequency scan;
  same risk.

Each site got a short comment pointing back at this file rather than
re-explaining the reasoning inline every time.

### Verification
- `python3 -m py_compile bot.py` passes.
- Full test suite: 262 passed, 97.65% coverage, unchanged from before the
  fix (`DISCORD_TOKEN=x python3 -m pytest tests/ -q`). Confirms the
  concern noted above - that `stop_active_hardware_process()` itself
  didn't need to change, only its call sites - held up in practice, since
  `TestStopActiveHardwareProcessMore` and friends (which call the function
  directly/synchronously) still pass unmodified.
- Did not add a new test asserting "the event loop stays responsive during
  teardown" (e.g. a test that schedules a concurrent coroutine and checks
  it runs while `stop_active_hardware_process` is mid-flight in the
  thread pool) - the existing suite verifies behavior is unchanged, not
  that the concurrency property this fix is *for* now holds. Worth adding
  if someone wants stronger regression protection against this specific
  issue creeping back in.
- Did not verify against a live Discord gateway connection or real
  subprocess teardown taking the full ~1-2s (all teardown in the test
  suite uses mocked/fast-exiting processes) - the fix is a mechanical,
  well-understood asyncio pattern (`to_thread` around a blocking call)
  applied consistently with eight other existing uses in this same file,
  so this was treated as low-risk enough not to require that, but flagging
  the gap in verification depth for transparency.
