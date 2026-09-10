---
status: DONE (fixed this session)
found_during: efficiency + streaming-latency review, this session
verified_against: sources/sdr_aircraft.py, sources/sdr_satellite.py, sources/sdr_radio.py (current checkout)
---

# 3. SDR sources run a trailing ffmpeg stage that only upmixes mono to stereo

## Problem
This is the same category of waste as `01-redundant-ffmpeg-relay.md`
(a process stage that only reshapes a format that's already correct in
every other respect), just one hop further upstream - inside the source's
own capture pipeline instead of on the Discord-facing leg.

**`sources/sdr_aircraft.py`** and **`sources/sdr_satellite.py`** both build
a 3-stage shell pipeline:

```
rtl_fm ... | sox <resample to 48k, mono> | ffmpeg <mono, 48k> -> <stereo, 48k>
```

The `sox` stage already resamples to 48kHz. The `ffmpeg` stage does
nothing but duplicate that one channel into two - it doesn't touch sample
rate, bit depth, or codec. `sox` can do the resample *and* the channel
duplication in the same invocation via `remix`, making the `ffmpeg` stage
entirely removable.

**`sources/sdr_radio.py`** builds a 2-stage pipeline:

```
rtl_fm -r 48k ... | ffmpeg <mono, 48k> -> <stereo, 48k>
```

`rtl_fm` is already told to output 48k directly (`-r 48k`), so here
`ffmpeg` is doing *only* the mono-to-stereo duplication - no resample
either. This can become a single `sox` invocation doing just the remix, or
some other single-purpose tool - the point is it doesn't need a full
ffmpeg process spun up for a channel-duplication no-op.

## Why this matters for latency specifically
Every extra pipeline stage means:
- One more process fork/exec at stream start, source switch, frequency
  retune-that-restarts-the-pipeline, and `/radio restart` - all of which go
  through `spawn_hardware_capture_stream()`, which calls
  `stop_active_hardware_process()` + re-spawns the whole shell chain from
  scratch every time.
- One more pipe hop's worth of buffering between capture and Discord
  actually getting bytes - `wait_for_pipeline_ready()` is polling for the
  *first* bytes to reach the FIFO, and those bytes have to have already
  passed through every stage in the chain, so an extra stage very directly
  adds to the time before that first select() wakeup fires (and therefore
  to how long `/radio start`/`/radio restart` feels like it takes before
  audio is audible).
- One more process's ongoing CPU/memory footprint for the life of the
  stream (small per-process, but this project explicitly targets
  resource-constrained hardware like a Raspberry Pi - see README's
  "Hardware Warning" section - so it's not free there the way it might be
  on a full server).

## Suggested fix

For `sdr_aircraft.py` (current: `rtl_fm | sox (resample) | ffmpeg (remix)`):
```python
def build_command(instance: dict, frequency: str, fifo_pipe: str) -> str:
    return (
        f"rtl_fm -f {frequency} -M am -s 25k -r 24k -g 48 | "
        "sox -t raw -r 24k -e signed-integer -b 16 -c 1 - "
        f"-t raw -r 48k -e signed-integer -b 16 -c 2 - remix 1 1 >> {fifo_pipe}"
    )
```

For `sdr_satellite.py` (current: `rtl_fm | sox (resample) | ffmpeg (remix)`),
same shape, just this file's existing rates:
```python
def build_command(instance: dict, frequency: str, fifo_pipe: str) -> str:
    return (
        f"rtl_fm -f {frequency} -M fm -s 40k -r 32k -g 45 | "
        "sox -t raw -r 32k -e signed-integer -b 16 -c 1 - "
        f"-t raw -r 48k -e signed-integer -b 16 -c 2 - remix 1 1 >> {fifo_pipe}"
    )
```

For `sdr_radio.py` (current: `rtl_fm (already 48k) | ffmpeg (remix only)`):
```python
def build_command(instance: dict, frequency: str, fifo_pipe: str) -> str:
    return (
        f"rtl_fm -f {frequency} -M wbo -s 170k -r 48k -g 40 | "
        "sox -t raw -r 48k -e signed-integer -b 16 -c 1 - "
        f"-t raw -r 48k -e signed-integer -b 16 -c 2 - remix 1 1 >> {fifo_pipe}"
    )
```

`remix 1 1` tells sox to build a 2-channel output where both output
channels are copies of input channel 1 - that's the mono-to-stereo
duplication ffmpeg was doing, expressed as sox's own remix syntax instead
of a second process.

`REQUIRED_PACKAGES` in `sdr_radio.py` currently lists `["usbutils",
"rtl-sdr", "ffmpeg"]` and would need `sox`/`libsox-fmt-all` added (and
`ffmpeg` could arguably be dropped from that list if nothing else in the
file still needs it - check before removing, don't assume).
`sdr_aircraft.py`/`sdr_satellite.py` already list `sox` so no change needed
there beyond possibly dropping `ffmpeg` if unused elsewhere in the file
(it isn't, in the current checkout - `build_command()` is the only user).

## Verification needed before trusting this (not done this session)
This was derived from reading the shell command strings and sox's
documented `remix`/rate-conversion behavior, not from running it against
real RTL-SDR hardware or even a dry-run with synthetic input. Before
merging:
- Confirm sox's default resampler quality at these rate-conversion ratios
  (24k->48k, 32k->48k) sounds equivalent to what it was already doing
  when the same `sox` invocation was the last stage (it wasn't - `ffmpeg`
  was previously downstream of `sox` doing a no-op passthrough on rate, so
  this isn't a *new* resampling step, just relocating where the remix
  happens - but still worth an ear-check on real audio, not just trusting
  the pipeline arithmetic on paper).
- Confirm exit-code/error propagation through the shell pipe (`|`) still
  behaves the way `stop_active_hardware_process()`'s `killpg()` expects -
  removing a stage shouldn't change this (it's still one shell forking N
  children either way) but confirm rather than assume.
- No existing test in `tests/test_actions_scan_range.py` or elsewhere
  appears to assert on the literal `build_command()` output strings for
  these three files (worth double-checking) - if such a test exists, it
  will need updating alongside the source change, not as a surprise
  failure afterward.

---

## UPDATE - fixed

**status: DONE (fixed this session)**

Applied the exact commands proposed above to all three files, with one
addition: `sdr_radio.py`'s `REQUIRED_PACKAGES` was `["usbutils", "rtl-sdr",
"ffmpeg"]` and ffmpeg is no longer used anywhere in that file once its
`build_command()` switched to `sox` - updated the declaration to
`["usbutils", "rtl-sdr", "sox", "libsox-fmt-all"]` and touched up the
module docstring/comment to match (they still said "ffmpeg for
resampling"). Confirmed `ffmpeg` genuinely has no other use in
`sdr_radio.py` before removing it from the list, and confirmed
`tests/test_dependency_guard.py`'s reference to `sdr_radio`/`ffmpeg` (in
`TestCollectRequiredPackages`) uses a synthetic `fake_module(...)`, not
the real `sources/sdr_radio.py` file, so it was correctly left untouched.

`sdr_aircraft.py` and `sdr_satellite.py` already declared `sox` in
`REQUIRED_PACKAGES`. Checked (rather than leaving it as a follow-up) and
confirmed `build_command()` was the only user of `ffmpeg` in both files,
same as `sdr_radio.py` - removed `ffmpeg` from both `REQUIRED_PACKAGES`
lists and updated both module docstrings/comments to match, for the same
reason as the `sdr_radio.py` change above.

### Verification (addresses the "not done this session" gap noted above)
This time actually verified, not just reasoned about on paper:
- Installed `sox` and ran the literal merged command
  (`sox -t raw -r 24k -e signed-integer -b 16 -c 1 <mono input> -t raw -r
  48k -e signed-integer -b 16 -c 2 <output> remix 1 1`) against a
  synthesized mono 24kHz s16le test file.
- Confirmed output byte count matches exactly what's expected for
  48kHz/stereo/16-bit at the resampled duration (192000 bytes for 1s of
  input -> 1s of 48k stereo 16-bit output).
- Confirmed, with sox's default dither, left/right samples differ by at
  most 1 LSB frame-to-frame (expected - sox dithers each channel's
  rounding independently even when the source is duplicated) - so also
  reran with `-D` (dither off) and confirmed 0 mismatches across all
  48000 output frames, proving `remix 1 1` is genuinely duplicating the
  channel losslessly and the ±1 differences seen with dither on are
  intentional noise-shaping, not a bug in the command.
- Did not verify against a live RTL-SDR dongle or real aircraft/satellite
  RF input (no hardware available in this environment) - the resample
  ratios (24k->48k, 32k->48k) and dither behavior were verified with
  synthetic data, not real-world audio quality by ear. If anyone with the
  actual hardware picks this up, an ear-check against the pre-fix
  behavior would be the last piece of verification this item is still
  missing.
- `python3 -m py_compile` on the changed source files passes.
- Full test suite: 262 passed, 97.65% coverage, unchanged (confirms no
  test was silently asserting on the old three-stage command strings, as
  suspected above).
