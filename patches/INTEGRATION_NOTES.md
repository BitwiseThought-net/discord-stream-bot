# Integration notes - `android_emulator_updates.py`

This patch was written against the diff of PR #52, not the full live
`bot.py`. Apply it by hand (or have an agent with repo access apply it) -
don't blindly overwrite the file with this one, since some surrounding
context (imports, exact indentation, other command registrations) wasn't
visible when this was written.

## What to replace

1. **Imports** - add `secrets` to the existing import block near the top of
   `bot.py` (alongside the existing `subprocess`, `time`, etc.). `shutil`
   and `platform` are already imported per the original diff.

2. **`_detect_host_architecture()`** - replace the existing function with
   the version here. Behavior change: it no longer implies picking a
   different image per arch (it never actually did - both keys already
   pointed at the same string - this version just makes that intentional
   design explicit in the docstring and repurposes the detection for
   KVM/timeout tuning instead).

3. **New helpers** - add `_kvm_available()` and `_android_startup_timeout_s()`
   directly after `_detect_host_architecture()`.

4. **`_spawn_android_emulator_stack()`** - replace the existing function
   with the version here. Functional changes:
   - Fixes the healthcheck-readiness condition (was reporting ready on
     `"Up"` alone; now requires `"healthy"` in the `docker compose ps`
     output).
   - Adds a `docker` CLI preflight check with an actionable error message.
   - Adds a `docker-compose` (v1 binary) fallback on the `up` call, matching
     the fallback that already existed on the `down` call.
   - Passes `/dev/kvm` through when present, omits it when absent - no
     configuration needed either way.
   - Scales the startup timeout by host capability instead of a fixed 30s.
   - Generates a random per-session password and stores it + the web port
     on `bot` for the new `android-ui` command.
   - Reads image/volume/timeout/port from environment variables (see
     `docs/sources.md` and `.env.example` - new variables listed there)
     instead of hardcoding them.

5. **Teardown block inside `stop_active_hardware_process()`** - the
   existing `if getattr(bot, 'compose_stack_file', None): ...` block gets
   one addition: `bot.android_web_password = None` in the `finally` clause.
   The function `_teardown_android_stack_block()` in the patch file is
   **not meant to be called** - it exists only so the updated block can be
   copy-pasted as one contiguous chunk. Delete the wrapper `def` line and
   its docstring when pasting, keep only the `if getattr(...)` body.

6. **`android_ui` command** - this is a first working version, not
   necessarily matching the file's actual command-registration pattern.
   Before merging:
   - Confirm how other `/​<COMMAND_NAME>` subcommands are registered in the
     real file (direct `@radio_group.command` decorator vs. a cog vs.
     something else) and match that pattern exactly.
   - **Add the same permission/role check the other privileged subcommands
     use.** This command hands out interactive control of a full Android
     instance - it must not be open to every server member by default.
   - Set `ANDROID_WEB_HOST` in your `.env` (the bot has no reliable way to
     determine its own externally-reachable address) or extend the command
     to accept a host override at call time.

## What was intentionally left out of this pass

- **Reverse proxy in front of the web UI.** Direct `network_mode: host`
  exposure + a random password is the fast path to "it works," but has no
  TLS and no rate-limiting on the password. Treat the current version as a
  working first cut, not a production-hardened one - see
  `memory/security-concerns.md`.
- **`sources/android_apps.json` app auto-provisioning.** Still just a plan
  (see `memory/android-emulator-source.md` §3) - not implemented in this
  pass.
- **Automated tests** for any of the above. None of this has test coverage
  yet.
