---
status: DONE (verified)
verified_against: bot.py, wait_for_pipeline_ready(), execute_stream_pipeline()
---

# 5. Fixed `asyncio.sleep(0.4)` replaced with actual readiness polling

## Problem
After spawning the capture pipeline, the code did a flat
`await asyncio.sleep(0.4)` before checking whether to start playback — a
400ms tax on *every* stream start/restart/source-switch regardless of how
fast that particular source actually was to start producing audio
(`test_signal`'s ffmpeg sine generator is ready almost instantly), while
simultaneously not being a robust guarantee for a genuinely slow-starting
source (rtl_fm claiming the USB device, etc.).

## Fix
`wait_for_pipeline_ready(timeout: float = 0.4)` — polls whether the FIFO
actually has data ready via `select()` on the permanently-open
`GLOBAL_FIFO_FD`, returning as soon as data shows up instead of always
waiting the full duration:

```python
async def wait_for_pipeline_ready(timeout: float = 0.4) -> bool:
    try:
        readable, _, _ = await asyncio.to_thread(select.select, [GLOBAL_FIFO_FD], [], [], timeout)
        return bool(readable)
    except Exception:
        return False
```

Important design point already reasoned through: `select()` only asks the
kernel "is there unconsumed data queued" — it never reads/consumes bytes
itself — so this can't race with or steal data from the real reader
(`discord.PCMAudio`'s file handle) that gets attached moments later.
`timeout=0.4` is kept as an upper bound (same value as the old fixed
sleep), so worst-case latency is unchanged, but typical-case latency drops
to however long the pipeline actually takes to produce its first bytes.

## Verified location
`bot.py`, `wait_for_pipeline_ready()` (defined just above
`execute_stream_pipeline()`), called as
`await wait_for_pipeline_ready(timeout=0.4)` right after
`spawn_hardware_capture_stream(active_source)`. `import select` present
at the top of `bot.py`.

## Not yet done
Nothing outstanding on this item.
