# memory/ - session continuity log for LLM assistants

This folder is a working log for LLM sessions on this repo. It is not
end-user documentation (see `docs/` for that, currently just a stub prompt
at `docs/LLM_DOCS_INIT.prompt.md`) and not a design spec - it is notes one
session leaves for the next so work does not need to be re-discovered from
scratch or accidentally re-litigated.

## How to use this folder

- Read `STATUS.md` first. It says what is done, what is open, and what to
  look at next.
- Each other file is one topic: a fix that was made, or a finding that is
  still open. Files are numbered in the rough order they were written, not
  by priority - check `STATUS.md` for priority.
- Each file has a `status:` line at the top: `DONE (verified)`, `DONE (fixed
  this session)`, or `OPEN`. Treat `OPEN` items as unverified analysis, not
  code that has been changed yet - confirm still-current behavior against
  `bot.py`/`sources/` before acting, since the file may describe a snapshot
  from an earlier commit.
- When you pick up an `OPEN` item and fix it, update that file in place
  (add a dated `## UPDATE` section like `01-redundant-ffmpeg-relay.md`
  does) rather than deleting it - the reasoning trail is the point.
- When you finish a new investigation, add a new numbered file rather than
  cramming it into an existing one, and update `STATUS.md`'s index.

## File index

- `STATUS.md` - current state, priorities, what to look at next.
- `01-redundant-ffmpeg-relay.md` - DONE. Removed a redundant second ffmpeg
  process on the Discord-facing leg of the pipeline; also fixed a related
  FIFO-reader race on `/radio restart`. (Migrated here from the repo root,
  where it was originally written - it belongs in this log.)
- `02-blocking-subprocess-calls-in-event-loop.md` - DONE. The biggest
  latency-relevant finding from this session: process-teardown code with
  multi-second blocking waits was running directly on the asyncio event
  loop instead of via `asyncio.to_thread`, unlike everything else blocking
  in this file. Fixed at all five call sites, verified against the full
  test suite.
- `03-sdr-pipeline-extra-ffmpeg-hop.md` - DONE. The three SDR sources each
  ran a trailing ffmpeg stage that did nothing but mono-to-stereo upmix at
  a sample rate the previous stage already produced - the same class of
  waste as the already-fixed item in file 01, just one hop further
  upstream (source-side, not Discord-side). Fixed by merging into a single
  `sox remix` call per source; verified against synthetic PCM.
- `04-misc-efficiency-notes.md` - OPEN, lower priority. Smaller items:
  a Dockerfile env var typo, scattered duplicate `STATE_FILE` reads, and a
  couple of things worth a comment/test rather than a code change.
