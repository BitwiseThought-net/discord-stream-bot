---
status: DONE (verified)
verified_against: bot.py, execute_stream_pipeline()
---

# 1. Redundant second ffmpeg process on the Discord-facing leg

## Problem
Every source's `build_command()` already writes raw s16le/48kHz/stereo PCM
into `FIFO_PIPE` (that's the plugin contract). `execute_stream_pipeline()`
was spawning a *second* ffmpeg process (`discord.FFmpegPCMAudio`) whose
input and output formats were identical — a pure passthrough, existing
only because that was the API being used. Cost: one subprocess spawn per
stream start, its own internal buffering as an extra hop, and ongoing
CPU/memory for the life of the stream, for every single active source.

## Fix
Use `discord.PCMAudio` directly on a buffered file handle to the FIFO —
no subprocess, discord.py's own libopus bindings encode straight from the
Python-side read.

```python
fifo_reader = open(FIFO_PIPE, "rb")
audio_stream = discord.PCMAudio(fifo_reader)
transformer = discord.PCMVolumeTransformer(audio_stream, volume=CURRENT_VOLUME_LEVEL)
vc.play(transformer)
```

Safety notes (already reasoned through, don't re-litigate unless something
changed): `PCMAudio.read()` treats a short read as end-of-stream, which
would be dangerous on a raw pipe read *except* that `open(path, "rb")`
returns a `BufferedReader`, whose `.read(n)` blocks until it actually has
`n` bytes or hits real EOF. Real EOF never happens here because
`PIPE_WRITE_HANDLE` keeps a permanent writer open on the FIFO. Also
preserves the existing "same long-lived reader survives source switches"
behavior, since a FIFO read isn't tied to which upstream process is
currently writing.

## Verified location
`bot.py`, inside `execute_stream_pipeline()`, right after
`spawn_hardware_capture_stream(active_source)` / `wait_for_pipeline_ready()`.
Comment block above the `open(FIFO_PIPE, "rb")` line explains the same
reasoning as above.

## Not yet done
`fifo_reader` is never explicitly closed on stop/playback-end — relies on
garbage collection. Probably fine (the FIFO has a permanent writer so
nothing depends on prompt closure) but wasn't deliberately decided either
way. Worth a look if picking this area back up.

---

## UPDATE — fixed

**status: DONE (fixed this session)**

Traced this further than "probably fine" and found a real correctness
issue, not just a leak: `vc.stop()` only *signals* the old `AudioPlayer`
thread to end and returns immediately — the thread's actual teardown (the
point where its last reference to the old `PCMAudio`/file object drops)
happens asynchronously. `/radio restart` calls `stop_active_hardware_process()`
+ `vc.stop()` then immediately calls `execute_stream_pipeline()` again,
which used to open a brand-new `open(FIFO_PIPE, "rb")` with no coordination
with the old one. That leaves a real window where two readers can be
attached to the same FIFO at once — and POSIX doesn't guarantee which
reader gets which bytes when there's more than one, so audio could get
split between the old (dying) and new reader during a restart.

Fixed with a new `close_fifo_reader()` helper (next to
`stop_active_hardware_process()`) that synchronously closes and clears
`bot.fifo_reader`. Called in two places:
- `execute_stream_pipeline()`, immediately before opening a new
  `fifo_reader`, inside the `if not vc.is_playing():` block.
- `stop()` command handler, alongside `stop_active_hardware_process()`,
  for full cleanup on disconnect.

`bot.fifo_reader` (new attribute, initialized `None` in
`StreamBotClient.__init__` alongside `hardware_process`/`sox_process`/
`ffmpeg_process`) tracks the currently-open handle so it can be found and
closed deterministically instead of waiting on GC.

Verified directly (not via the existing pytest suite, which wasn't
targeted this round): opened a real handle on an actual FIFO, confirmed
`close_fifo_reader()` actually closes it and resets the tracking attribute
to `None`, and confirmed calling it with nothing open (or twice in a row)
is a safe no-op. `python3 -m py_compile bot.py` passes.

**Not covered by this fix, still open:** no dedicated pytest coverage was
added for `close_fifo_reader()` itself or for the two call sites — this
ties into the broader "test coverage hasn't caught up with recent changes"
item already listed in `README.md`'s suggested next steps. Also noticed
in passing (unrelated to this fix, pre-existing, did not investigate):
`TestStopActiveHardwareProcessMore::test_second_wait_succeeds_after_sigterm_timeout`
and `::test_final_kill_also_raises_is_swallowed` fail on this checkout —
confirmed `stop_active_hardware_process()` itself is byte-for-byte
unchanged by this fix, so these are pre-existing failures, not a
regression from this change, but worth someone's attention.

