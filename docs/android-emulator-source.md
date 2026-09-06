# Android Emulator Source — Progress & Follow-up Work

Context: PR #52 (`updates` branch) introduced a Docker-Compose-driven Android
emulator audio source (`_spawn_android_emulator_stack()` in `bot.py`, backed by
`sources/android_emulator.json`, `docker-compose.yml`, and `Dockerfile`
changes). See `docs/sources.md` for user-facing setup/config/run docs, and
`memory/security-concerns.md` for the security tradeoffs this source makes
(tracked deliberately, not blocking — see that file for the reasoning).

## Status as of this pass

- ✅ `.env.example` gap analysis done (§1) — still needs reconciling against
  the real file, which hasn't been directly inspected.
- ✅ Code updated (`patches/android_emulator_updates.py` +
  `patches/INTEGRATION_NOTES.md`) — not yet applied to the real `bot.py`,
  since only the diff (not the full file) was available when this was
  written. Ready for a human or an agent with repo access to splice in.
- 🚧 Web UI: **initial version implemented** in the same patch — generates a
  per-session password, adds a `android-ui` command. Needs: permission
  gating matched to the repo's real pattern, and ideally a reverse
  proxy/TLS layer before production use.
- ⬜ App auto-provisioning config (§3): still just a plan, not implemented.

## Decisions confirmed by the project owner (don't re-litigate these)

- **The single image tag for both architectures is intentional**, not a bug.
  Goal: a user on a Raspberry Pi (arm64) and a user on a plain x86_64 box
  should both get a working source with **zero extra configuration** —
  neither should need to know or specify their architecture. The tag is
  expected to be a genuine multi-arch manifest that Docker resolves
  automatically at pull time. Architecture detection in the code is
  legitimately still useful, but repurposed for KVM passthrough and
  startup-timeout tuning rather than image selection.
- **Security concerns should be documented, not used to block
  functionality.** The Docker-socket / privileged-container tradeoffs are
  real and are written up in `memory/security-concerns.md`, but the project
  has chosen to accept them in exchange for a source that works seamlessly.
  Don't reintroduce these as blockers in future passes — extend the
  mitigations instead (see that file's "possible mitigations" section).

---

## 1. `.env.example` gap analysis

**Status: still unverified against the live file.** The following are
env-overridable as of the code update in this pass (see
`patches/android_emulator_updates.py`), with defaults chosen so nothing
*needs* to be set:

| Variable | Default | Notes |
|---|---|---|
| `ANDROID_EMULATOR_IMAGE` | `linuxserver/android:armv7-x86_64` | Must be genuinely multi-arch if overridden |
| `ANDROID_DATA_VOLUME` | `android_output` | For multi-instance hosts |
| `ANDROID_STARTUP_TIMEOUT_S` | auto (30s w/ KVM, 90s without) | Manual override |
| `COMPOSE_TMP_DIR` | `/tmp` | For hosts restricting `/tmp` |
| `ANDROID_WEB_VNC` | `true` | Toggles the web UI entirely |
| `ANDROID_WEB_PORT` | `3000` | Web UI port |
| `ANDROID_WEB_HOST` | *(unset — must be set to use the web UI)* | Bot can't self-detect its reachable address |

**Action item:** once the real `.env.example` is available, add whichever of
these are genuinely missing and confirm none of the names collide with
existing variables.

---

## 2. Web UI — implementation notes

Implemented in this pass (`patches/android_emulator_updates.py`):

- `_spawn_android_emulator_stack()` now generates a random password
  (`secrets.token_urlsafe(12)`) per source-start, bakes it into the
  generated compose YAML as `PASSWORD=...` along with `CUSTOM_PORT`, and
  stores the password + port on `bot` (`bot.android_web_password`,
  `bot.android_web_port`).
- New `android_ui` command (bound to the existing `radio_group`) replies
  **ephemerally** with the URL (built from `ANDROID_WEB_HOST` +
  `ANDROID_WEB_PORT`) and the current password.
- Teardown (`stop_active_hardware_process()`) clears
  `bot.android_web_password` when the compose stack goes down, so a stale
  password can't be reused after the source stops.

**Still open (see `memory/security-concerns.md` for the reasoning, not
listed here as a blocker):**

- No reverse proxy / TLS in front of the UI yet — it's reached directly over
  `network_mode: host`.
- The `android_ui` command needs the same role/owner permission check the
  repo's other privileged `/​<COMMAND_NAME>` subcommands use — the patch
  flags this with a `TODO` rather than guessing at the wrong check.
- `ANDROID_WEB_HOST` must be set manually; the bot has no reliable way to
  self-detect a LAN IP or public hostname. A future pass could attempt
  auto-detection with manual override as fallback, if that's judged worth
  the added complexity.

---

## 3. Plan: config file for auto-provisioned apps (not yet implemented)

Goal: let operators declare a list of Android apps that get installed
automatically every time the emulator stack spins up, instead of manually
sideloading each time.

Steps:

1. **New config file**: `sources/android_apps.json` — a dedicated JSON file
   (preferred over cramming a JSON list into `.env`) so entries are easy to
   review/diff in PRs:
   ```json
   {
     "apps": [
       {
         "name": "Example Radio App",
         "apk_url": "https://example.com/app.apk",
         "package": "com.example.radio"
       }
     ]
   }
   ```
2. **Provisioning step**: add a call after the "wait for healthy" loop in
   `_spawn_android_emulator_stack()`. For each entry:
   - Download the APK from `apk_url` (or pick it up from the shared volume
     if pre-staged).
   - `adb connect <host>:5555` — reachable directly since the container uses
     `network_mode: host`.
   - `adb install <path-to-apk>`.
3. **Idempotency**: check `adb shell pm list packages | grep <package>`
   before installing, so restarts don't redundantly reinstall.
4. **Failure isolation**: log and continue past a single app's install
   failure rather than aborting the whole source startup.
5. **Docs**: once finalized, replace the placeholder note in
   `docs/sources.md` §5 with the confirmed schema and a short walkthrough.

---

## Corrections logged during this pass

- The command-group env var is **`COMMAND_BASE`**, not `COMMAND_NAME` as
  earlier drafts of `.env.example` had it — confirmed directly against
  `bot.py`: `COMMAND_NAME = os.getenv('COMMAND_BASE', 'radio')`.
  `COMMAND_NAME` is the in-code variable name; `COMMAND_BASE` is the env
  key. Fixed in `.env.example`.
- `RECOVERY_MODE` (`os.getenv('RECOVERY_MODE', 'resume')`) exists in
  `bot.py` and was missing from earlier `.env.example` drafts — added,
  default `resume`. Its full set of accepted values beyond the default
  wasn't visible in what's been shared so far; confirm against `bot.py`
  before documenting anything beyond `resume` as valid.
- Takeaway: `DISCORD_GUILD_ID`, `DISCORD_VOICE_CHANNEL_ID`, and `FIFO_PIPE`
  in `.env.example` are still inferred, not confirmed the same way
  `COMMAND_BASE` and `RECOVERY_MODE` now are — worth pasting their actual
  `os.getenv(...)` lines the same way before treating them as settled.
- **CI regression, found and fixed:** the first version of
  `patches/android_emulator_updates.py` called `subprocess.run()` directly
  for the `docker compose up`/`ps` calls. The existing test suite
  (`test_docker_compose_dispatch_calls_android_stack`) asserts
  `subprocess.run` is never called when this source is dispatched — all
  docker interaction is expected to go through `subprocess.Popen` instead.
  Fixed by routing every docker/compose call through a single
  `_run_docker_cmd()` Popen-based helper. If a future edit reintroduces a
  direct `subprocess.run(...)` call anywhere in this feature, that test
  will catch it again — treat a failure there as a real regression, not a
  flaky test.

## Open questions for next pass

- Confirm the real shape of `sources/android_emulator.json` as merged (not
  fully visible during the original analysis) and reconcile with the example
  in `docs/sources.md`.
- Decide whether `ANDROID_WEB_HOST` should get an auto-detection attempt
  (e.g. via the Docker-reported host IP) with manual override as a fallback,
  to keep the "zero config" goal intact for the web UI specifically.
- Once repo access is available: apply `patches/android_emulator_updates.py`
  per `patches/INTEGRATION_NOTES.md`, matching the real command-registration
  and permission-check patterns rather than the placeholders in the patch.
