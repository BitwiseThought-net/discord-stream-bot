# Audio Sources

`discord-stream-bot` supports two families of audio sources:

1. **Standard hardware/SDR sources** - driven by JSON profiles under `sources/*.json` and a fixed sox/ffmpeg subprocess pipeline (line-in, USB mic, RTL-SDR, etc).
2. **Docker-Compose-driven sources** - sources that need their own containerized environment. The first of these is the **Android emulator** source, added in PR #52.

This document covers setup, configuration, and operation of the Android emulator source specifically.

---

## 1. How it works

When a source profile sets `"pipeline_type": "docker_compose"`, `spawn_hardware_capture_stream()` skips the normal sox/ffmpeg dispatch and instead calls `_spawn_android_emulator_stack()`, which:

1. Detects host CPU architecture (`arm64` vs `x86_64`) - **not** to pick a different image (see §1a below), but to decide whether to pass `/dev/kvm` through and how long to wait for the container to come up.
2. Writes a temporary `docker-compose.android.<pid>.yml` to `/tmp`, defining a single `android` service (the `linuxserver/android` image) with:
   - `privileged: true` and `network_mode: host` (required for the Android emulator's virtualization and networking to work correctly)
   - a shared named volume (`android_output`) where the emulator writes its captured audio as raw PCM
   - `/dev/kvm` passed through automatically when present on the host, omitted when it isn't - no configuration required either way
   - a healthcheck that polls for the `emulator64` process
   - a freshly generated one-time password for the emulator's built-in web UI (see §7)
3. Runs `docker compose -f <tmp file> up -d --wait --wait-timeout <N>`, letting Compose itself block until the container's healthcheck actually reports **healthy** (or the wait times out) - for a timeout that scales with host capability (short on an accelerated x86_64 host, longer on unaccelerated ARM boards). This replaced an earlier hand-rolled `docker compose ps` polling loop that had a bug: it treated `"Up (health: starting)"` as ready, which isn't the same as actually healthy.
4. Spawns a bridge process: `tail -f <shared PCM file> | ffmpeg ... >> $FIFO_PIPE`, which continuously converts the emulator's raw audio into the format the bot's existing FIFO-based streaming pipeline expects, and appends it to the same FIFO everything else streams through.

### 1a. One image for both architectures - by design

The same image tag is used regardless of whether the code detects `arm64` or `x86_64`. **This is intentional, not an oversight.** The project's goal is zero-config support on both a Raspberry Pi and a plain x86_64 host - a user shouldn't have to know or care which architecture they're on. The configured tag (`linuxserver/android:armv7-x86_64` by default) is expected to be a multi-arch manifest, meaning Docker itself resolves the correct underlying image for the host at pull time - the same way official images like `python:3.12` work unmodified across amd64 and arm64. The Python-side architecture detection is repurposed instead for the things that legitimately differ by host and *aren't* solved by the image being multi-arch: whether `/dev/kvm` exists to pass through, and how long to wait for the (typically slower, unaccelerated) ARM emulation path to boot.

If you swap in a different image and it turns out not to be genuinely multi-arch, you'll see this at container-start time (wrong-architecture image failing to run) rather than anywhere in this code path - worth a quick manual check (`docker manifest inspect <image>`) the first time you change `ANDROID_EMULATOR_IMAGE`.

When the source is stopped (or another source is started), `stop_active_hardware_process()`:

1. Runs `docker compose -f <stack file> down --timeout 3` (falling back to the legacy `docker-compose` binary if the `docker compose` plugin isn't available), then deletes the temp compose file.
2. Kills the tail/ffmpeg bridge process group.
3. Clears the web UI credential generated for that session (see §7).
4. Falls through into the existing hardware/sox/ffmpeg process cleanup used by all sources.

Because the bridge writes into the same `$FIFO_PIPE` as every other source, nothing downstream of the pipe (the Discord voice send loop) needs to know or care that the audio originated from a container instead of a physical device.

### 1b. When the container actually starts and stops

Selecting the source in the bot does not itself talk to Docker — it just picks *which* source is active. The container only starts when that selection actually gets streamed:

1. **`/radio input`** (no index) lists available sources — this only discovers/lists, nothing starts here.
2. **`/radio input <index>`** for the Android emulator's index, or **`/radio start`** when it's already the saved source, calls `execute_stream_pipeline()` → `spawn_hardware_capture_stream(active_source)`. That function checks `active_source["pipeline_type"]`; when it's `"docker_compose"`, it delegates to `_spawn_android_emulator_stack()` — **this is the actual `docker compose up` moment.**
3. **On bot restart**, if `RECOVERY_MODE=resume` (the default) and the Android emulator was the last-active source, `on_ready()` re-reads `STATE_FILE` and runs the same pipeline path, starting the container again automatically.
4. **`/radio stop`**, or switching to a different source, tears the container back down via `stop_active_hardware_process()`.

So: it's on-demand, tied to when the source is actually streamed, not something that starts as soon as the bot boots or as soon as the source appears in `/radio input`'s list.

#### Two discovery bugs found and fixed while tracing this path

Both of these were bugs in the code as merged in PR #52, found by walking through "does selecting this source actually work end-to-end" rather than anything introduced afterward:

- **The source didn't appear in `/radio input`'s list at all.** `discover_hardware_profile()`'s scan loop only recognized two `discovery_trigger` values: `alsa_sound_card` and anything starting with `usb_chipset_`. `sources/android_emulator.json` uses `"discovery_trigger": "always_available"` — a value the loop never checked for. (`test_signal.json` uses that same trigger, but `test_signal` is hardcoded into slot 0 separately, which is why *it* showed up and masked the fact that `"always_available"` was otherwise dead code.) **Fixed** by adding a third branch to the loop that handles `"always_available"` generically, so any current or future no-hardware-to-probe source works the same way.
- **Even once visible, selecting it wouldn't have started the container.** The cache entries `discover_hardware_profile()` builds (and that `/radio input <index>` resolves against) only copied `type`/`device`/`channels`/`description` from each source's JSON profile — `pipeline_type` was silently dropped. Since `spawn_hardware_capture_stream()` decides whether to dispatch to `_spawn_android_emulator_stack()` based on `active_source.get("pipeline_type")`, a missing field defaults to `"default"`, meaning selection would have fallen through to the normal ffmpeg pipeline path and failed with *"Explicit template structure empty"* instead of ever starting Docker. **Fixed** by propagating `pipeline_type` into every entry `discover_hardware_profile()` builds, across all three discovery branches (ALSA, SDR, always-available).

Both are covered by the "when does it spin up" walkthrough above being accurate as written — if you ever add a new `docker_compose`-pipeline source, make sure its JSON profile's `pipeline_type` still shows up in `bot.discover_hardware_profile()`'s output before assuming selection will work.

---

## 2. Prerequisites

The Android source needs privileges the other sources don't. Before enabling it:

- **Docker Engine on the host**, with `docker compose` (v2 plugin) available. The code falls back to the standalone `docker-compose` binary if the plugin call fails, on both startup and teardown.
- **The bot container needs to control the host's Docker daemon.** This PR adds:
  - `docker.io` to the bot's own `Dockerfile` (gives the bot container a `docker` CLI)
  - `/var/run/docker.sock:/var/run/docker.sock` to `docker-compose.yml` (gives the bot container access to the host Docker daemon)

  This is a real, tracked security tradeoff, not an oversight - see `memory/security-concerns.md` for the full writeup. Short version: anything with access to `docker.sock` has root-equivalent control over the host, and this source needs that to work. The project's decision is to accept this tradeoff in exchange for a source that "just works," while documenting it clearly so operators can make an informed call about where they run it.
- Enough free disk/RAM to run an Android emulator alongside the bot - this is a full Android VM, not a lightweight process. Expect this to be noticeably slower to boot on a Raspberry Pi without KVM than on an accelerated x86_64 host - that's expected, not a bug (see §1a).
- No manual architecture-specific setup should be required on either platform - if you find yourself needing to configure something differently for ARM vs x86_64 to get this working, that's a gap worth reporting, since "works the same with zero config on both" is the explicit design target.

---

## 3. Configuration (environment variables)

All of the following are optional - every default is chosen so the source works out of the box with **no** `.env` changes required. Set any of these only if you need to override the default:

| Variable | Default | Purpose |
|---|---|---|
| `ANDROID_EMULATOR_IMAGE` | `linuxserver/android:armv7-x86_64` | Override the image (must be a genuine multi-arch manifest - see §1a). |
| `ANDROID_DATA_VOLUME` | `android_output` | Volume name; change if running multiple bot instances on one host. |
| `ANDROID_STARTUP_TIMEOUT_S` | *(auto: 30s with KVM, 90s without)* | Force a specific startup wait instead of the capability-based default. |
| `COMPOSE_TMP_DIR` | `/tmp` | Where the generated compose file is written; override on hosts that restrict `/tmp`. |
| `ANDROID_WEB_VNC` | `true` | Enable/disable the emulator's built-in web UI entirely. |
| `ANDROID_WEB_PORT` | `3000` | Port the web UI listens on (see §7). |
| `ANDROID_WEB_HOST` | *(unset)* | The address the bot should tell users to connect to for the web UI - the bot can't reliably determine this on its own. **Set this if you enable the web UI.** |

---

## 4. Rebuilding and redeploying

Because the `Dockerfile` and `docker-compose.yml` both changed, a plain restart isn't enough - you need to rebuild the bot image and recreate the stack:

```bash
docker compose build --no-cache
docker compose up -d
```

Confirm the socket mount landed:

```bash
docker exec -it discord_audio_bot ls -l /var/run/docker.sock
docker exec -it discord_audio_bot docker ps
```

If the second command fails inside the container, the socket mount or the `docker.io` package didn't take - re-check `docker-compose.yml` and rebuild.

---

## 5. Configuring the source profile

Add (or confirm) a profile at `sources/android_emulator.json`. Based on the fields the code reads (`type`, `pipeline_type`), a minimal profile looks like:

```json
{
  "type": "android_emulator",
  "pipeline_type": "docker_compose",
  "label": "Android Emulator"
}
```

> **Note:** the exact shape of `android_emulator.json` as merged in PR #52 wasn't fully visible in the diff pulled for this doc - confirm the live file in the `updates` branch matches what your matrix-profile loader (`load_matrix_source_profiles()`) expects, and add any additional fields it requires (e.g. a display name or command alias) to match your other source profiles' conventions.

Once the profile exists, the source should be selectable the same way any other hardware source is - through whatever `/​<COMMAND_NAME> start` (or equivalent) selection command the bot already exposes for choosing a source by `type`.

---

## 6. Running it

1. Make sure the bot is deployed with the updated `Dockerfile`/`docker-compose.yml` (§4).
2. Select the Android emulator source through the bot's normal source-selection command.
3. The bot will:
   - Probe the host for KVM and pass it through automatically if present - no setup needed on either ARM or x86_64.
   - Spin up the `android` container (first run may take a while - the emulator image is large, and boot time depends heavily on whether KVM is available).
   - Wait for the container to report genuinely **healthy** before bridging audio.
   - Start streaming whatever audio the Android emulator produces into your target voice channel.
4. Stop the source (or switch to a different one) the same way you'd stop any other source - this tears down the compose stack, clears the web UI credential, and kills the bridge process automatically.

### Verifying it's working

```bash
# Confirm the compose stack is up
docker compose -f /tmp/docker-compose.android.<pid>.yml ps

# Confirm audio is actually being written
docker exec -it <android_container> ls -la /data/android_output/

# Confirm the bridge process is alive on the host
ps aux | grep "tail -f"
```

---

## 7. Web UI

The emulator image ships a browser-based UI (KasmVNC) for interacting with the Android instance directly - useful for installing/launching apps manually, debugging, or just watching what's on screen.

**Status: initial version implemented** (see `patches/android_emulator_updates.py` for the code, applied against this PR's diff - not yet merged/tested in the live repo).

How it works:

- On every source start, a random one-time password is generated and baked into the generated compose YAML (`PASSWORD=...`), along with `CUSTOM_PORT=$ANDROID_WEB_PORT`.
- A new `/​<COMMAND_NAME> android-ui` command hands the URL + current password to the requesting user via an **ephemeral** reply - never posted where others in the channel can see it.
- The credential is cleared automatically when the source stops.

To use it:

1. Set `ANDROID_WEB_HOST` in your `.env` to the bot host's LAN IP or domain (the bot can't determine this reliably on its own).
2. Start the Android emulator source as normal.
3. Run the `android-ui` command to get the link + password.

**Known limitations of this first version** (tracked in `memory/security-concerns.md` and `memory/android-emulator-source.md`):

- No reverse proxy / TLS - the UI is reached directly over `network_mode: host`, protected only by the generated password.
- The command's permission gating needs to be matched to whatever check the repo's other privileged `/​<COMMAND_NAME>` subcommands already use - flagged as a required step before merge, not yet wired up in the patch.

---

## 8. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `❌ [Docker] Android emulator start failed` in logs | `docker compose up -d` failed - check `docker.sock` is mounted and the bot container's user can access it. |
| `❌ [Docker] docker CLI not found in the bot container` | `docker.io` didn't get installed, or you're running an older image - rebuild with `--no-cache`. |
| `⚠️ [Docker] Android container did not report healthy within Ns` | Emulator is slow to boot (expected without KVM - see §1a) or something is actually wrong. Increase `ANDROID_STARTUP_TIMEOUT_S` if it's just slow; check `docker logs` on the android container if it never comes up at all. |
| No audio in the voice channel, no errors | The shared volume path (`android_output` → `/data/android_output/emulator_audio.pcm`) may not match what's actually running inside the emulator - confirm the emulator (or an app on it) is actually producing PCM output at that path. |
| Old compose files piling up in `/tmp` | If the bot process crashes between writing the compose file and later cleanup, the temp file and possibly the running container can be orphaned. Periodically check `docker compose ls` / `/tmp/docker-compose.android.*.yml` for leftovers. |
| `android-ui` gives an unreachable link | `ANDROID_WEB_HOST` isn't set, or isn't reachable from where the user is connecting from (e.g. it's a LAN IP and they're off-network). |

---

## 9. Known gaps / things to verify before production use

- **`_spawn_android_emulator_stack` has direct test coverage** for the
  dispatch path (`tests/test_bot_commands.py::TestSpawnHardwareCaptureStream::
  test_docker_compose_dispatch_calls_android_stack`) - it asserts every
  docker/compose call goes through `subprocess.Popen`, never
  `subprocess.run`. Keep that invariant if you touch this function again.
- **Web UI hardening** (reverse proxy/TLS, real permission gating beyond
  Discord's own slash-command guild permissions) is a known follow-up, not
  yet done - see `memory/security-concerns.md` if present, or the
  discussion history for this feature.
- **`sources/android_apps.json` app auto-provisioning** is still just a
  plan, not implemented.
