---
status: OPEN (analysis only, low priority)
found_during: efficiency + streaming-latency review, this session
verified_against: Dockerfile, bot.py (current checkout)
---

# 4. Smaller efficiency/hygiene items

Grouped here because each is small enough not to need its own file. None
of these are likely to be the cause of a noticeable latency problem on
their own - file 02 is the one worth prioritizing.

## 4a. Dockerfile env var typo makes the bytecode-cache setting a no-op
`Dockerfile` has:
```dockerfile
ENV PYTHONTONTWRITEBYTECODE=1
```
The real variable is `PYTHONDONTWRITEBYTECODE`. As written, this sets an
env var Python never looks at, so `.pyc` bytecode caching stays *enabled*
inside the image despite the comment above it ("Prevent Python from
writing cached compiled .pyc tracks onto the container image") saying the
opposite is intended. Not a correctness bug and not meaningfully affecting
runtime latency (bytecode caching is generally a minor win, if anything,
for a long-running process) - flagged because it's a one-character fix
that makes the code match its own stated intent, and worth doing
opportunistically rather than as its own task.

## 4b. `STATE_FILE` is read independently in at least four places
`execute_stream_pipeline()`, `on_ready()`, `get_current_source_type()`, and
`volume()` each independently do their own `os.path.exists(STATE_FILE)` +
`open(...)` + `json.load(...)` + `try/except Exception: pass`-style
handling, with slightly different subsets of fields pulled out each time.
Not a latency problem (it's a tiny local JSON file, read rarely relative
to the audio streaming hot path), but it's duplicated logic that could
drift - e.g. if the default fallback for `selected_source` ever needs to
change from `"test_signal"`, that string literal would need updating in
multiple places rather than one. Worth consolidating into a single
`load_state() -> dict` helper (with sensible defaults baked in) next time
someone is touching state-file handling for another reason - not urgent
enough to justify a standalone change.

## 4c. `GLOBAL_FIFO_FD` is opened `O_NONBLOCK` but never itself written to
Near the top of `bot.py`:
```python
GLOBAL_FIFO_FD = os.open(FIFO_PIPE, os.O_RDWR | os.O_NONBLOCK)
PIPE_WRITE_HANDLE = os.fdopen(GLOBAL_FIFO_FD, "wb")
```
`PIPE_WRITE_HANDLE`'s only job is to exist - it's the "keep a permanent
writer open so the FIFO never sees real EOF" handle referenced in file 01
- nothing in the current codebase ever calls `.write()` on it. That's
fine as-is, but the `O_NONBLOCK` flag only matters *if* something writes
to it under backpressure (it would raise `BlockingIOError` on a full pipe
instead of blocking) - so this is a latent trap for whoever adds a future
feature that does write to `PIPE_WRITE_HANDLE` without realizing it's
non-blocking. Not a change to make now - just worth a comment at the
`PIPE_WRITE_HANDLE` definition noting "never write to this from a
non-recovery code path without handling BlockingIOError", so the next
person modifying this area has the context this file does.

## 4d. FIFO capacity is left at the OS default (64KB on Linux)
Nothing in `bot.py` sets `fcntl.F_SETPIPE_SZ` on `FIFO_PIPE`. At
48kHz/stereo/16-bit, 64KB is roughly a third of a second of audio buffer
headroom between a capture-side write and the Discord-side read. This is
probably fine in the common case (both sides are running close to
real-time), but if the aircraft/satellite/radio sources ever show audible
glitches under bursty scheduling (e.g. a heavily loaded Raspberry Pi),
increasing the pipe buffer via `fcntl.fcntl(GLOBAL_FIFO_FD,
fcntl.F_SETPIPE_SZ, <bytes>)` at startup is a cheap thing to try. Not
suggesting this as a change to make speculatively - only worth doing if
someone actually reports glitching, since a bigger buffer also means more
latency between "audio captured" and "audio heard" if the two sides ever
do drift apart, which cuts against the project's stated "minimal latency"
goal (see README's opening line) if applied without a real symptom to fix.

## 4e. Not investigated this session, flagged for awareness only
- Whether `discord.py`'s own opus encoding path (invoked per-frame from
  `PCMVolumeTransformer`/`PCMAudio`) is CPU-bound in a way that matters on
  a Raspberry Pi - this is downstream of discord.py itself, not something
  this project's code controls, so it wasn't investigated. Would only be
  worth revisiting if a specific host reports high CPU from the bot
  process itself (not from the capture pipeline's own tools) while
  streaming.
