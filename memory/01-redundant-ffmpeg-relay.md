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
