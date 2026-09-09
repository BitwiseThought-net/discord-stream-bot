---
status: DONE (verified)
verified_against: sources/sdr_aircraft.py, sources/sdr_satellite.py, sources/sdr_radio.py, sources/alsa.py, sources/test_signal.py
---

# 2. SDR pipeline simplification: drop `sox`, add low-delay ffmpeg flags

## Problem (two related findings, tracked together)

**a) Redundant `sox` stage.** `sdr_aircraft.py` and `sdr_satellite.py`
piped `rtl_fm | sox (resample only) | ffmpeg (reformat + resample again)`.
ffmpeg's own resampler (swresample) already does rate conversion *and*
mono→stereo conversion in the same step it has to run anyway to write the
FIFO's required format — `sox` was a whole extra process doing a subset of
work ffmpeg was already doing downstream.

**b) No low-latency ffmpeg flags anywhere.** None of the `build_command()`
implementations set `-fflags nobuffer -flags low_delay`, which reduces
ffmpeg's internal stream buffering on a live/continuous pipe feed.

## Fix

`sox` removed from both aircraft and satellite pipelines:
```python
# sdr_aircraft.py
f"rtl_fm -f {frequency} -M am -s 25k -r 24k -g 48 | "
f"ffmpeg -y -fflags nobuffer -flags low_delay -f s16le -ar 24k -ac 1 -i pipe:0 -f s16le -ar 48k -ac 2 pipe:1 >> {fifo_pipe}"

# sdr_satellite.py — same pattern, 32k source rate instead of 24k
```

`-fflags nobuffer -flags low_delay` added to **every** source's
`build_command()` ffmpeg invocation — confirmed present in all five:
`sdr_radio.py`, `sdr_aircraft.py`, `sdr_satellite.py`, `alsa.py`,
`test_signal.py`.

## Verified location
`sources/sdr_aircraft.py` and `sources/sdr_satellite.py` `build_command()`
each carry a comment explaining the `sox` removal reasoning explicitly.
Flags confirmed via direct grep of all five `build_command()` return
strings.

## Not yet done
Nothing outstanding on this item specifically. (General open item, not
specific to this fix: `rtl_fm`'s own internal buffering/gain settings
weren't tuned — that's a tool-level knob, not something wrong in this
code, just unexplored.)
