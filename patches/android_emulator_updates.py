# =========================================================================
# ANDROID EMULATOR SOURCE — updated implementation (see INTEGRATION_NOTES.md)
# =========================================================================
#
# This file is a drop-in replacement for the corresponding sections of
# bot.py added in PR #52. It is NOT a full bot.py — splice these pieces in
# at the same locations the originals occupy. See patches/INTEGRATION_NOTES.md
# for exactly what to replace and what to double-check, since this was
# written against the diff, not the full live file.
#
# Design goals driving these changes (per project direction):
#   1. Zero required configuration on EITHER architecture. A Raspberry Pi
#      (arm64) and a plain x86_64 host should both "just work" the moment
#      this source is selected — no env vars, no manual device passthrough,
#      no per-arch setup instructions for the operator to follow.
#   2. The single shared image tag for both architectures is intentional,
#      not an oversight — it's expected to be a multi-arch manifest, so
#      Docker itself resolves the correct underlying image per host at
#      pull time. Nothing in bot.py needs to choose between two tags.
#   3. Fix the healthcheck-readiness bug identified in review (the original
#      condition reported "ready" as soon as the container was `Up`,
#      independent of the healthcheck's own `healthy` status).
#   4. Lay the groundwork for the web UI: generate a per-session
#      credential and store enough state that a Discord command can hand
#      out a working link immediately once the stack is healthy.

import os
import shutil
import platform
import secrets
import subprocess
import time

# ---- Configuration (env-overridable; every default is chosen so nothing
# ---- has to be set for a working zero-config first run) -----------------
ANDROID_DEFAULT_IMAGE = os.environ.get(
    "ANDROID_EMULATOR_IMAGE", "linuxserver/android:armv7-x86_64"
)
ANDROID_DATA_VOLUME = os.environ.get("ANDROID_DATA_VOLUME", "android_output")
ANDROID_STARTUP_TIMEOUT_S_OVERRIDE = os.environ.get("ANDROID_STARTUP_TIMEOUT_S")
COMPOSE_TMP_DIR = os.environ.get("COMPOSE_TMP_DIR", "/tmp")
ANDROID_WEB_VNC = os.environ.get("ANDROID_WEB_VNC", "true").lower() in ("1", "true", "yes")
ANDROID_WEB_PORT = int(os.environ.get("ANDROID_WEB_PORT", "3000"))
# The bot has no reliable way to know its own externally-reachable
# address. Set this once per deployment; the android-ui command falls
# back to a placeholder if it isn't set, rather than guessing wrong.
ANDROID_WEB_HOST = os.environ.get("ANDROID_WEB_HOST")


# =========================================================================
# 3. HOST ARCHITECTURE DETECTION (ARM vs x86_64)
# =========================================================================
def _detect_host_architecture() -> str:
    """Return ``arm64`` or ``x86_64``.

    IMPORTANT: this does NOT select a different image per architecture.
    ``ANDROID_DEFAULT_IMAGE`` is expected to be a multi-arch manifest, so
    Docker resolves the correct underlying image for the host automatically
    at pull time — that's *why* both architectures intentionally point at
    the same tag. This function exists to tune compose-stack *behavior*
    that legitimately differs by host (KVM availability, startup timeout),
    so the source works with zero required configuration on both a
    Raspberry Pi and an x86_64 box.
    """
    try:
        machine = platform.machine().lower()
        if machine.startswith("a"):  # aarch64, armv7l, etc.
            return "arm64"
    except Exception:
        pass
    return "x86_64"


def _kvm_available() -> bool:
    """Return True if /dev/kvm exists and is read/write-accessible.

    Hardware-accelerated x86 emulation needs KVM. Most Raspberry Pi hosts
    won't have it — the emulator image is expected to fall back to
    software rendering in that case, which is slower but still functional,
    which is exactly the "works with no extra config" behavior wanted here.
    Some x86_64 hosts without virtualization enabled in BIOS/hypervisor
    settings also won't have it, so this probes directly rather than
    assuming based on architecture alone.
    """
    return os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK)


def _android_startup_timeout_s() -> float:
    """Arch/capability-aware wait time for the container to report healthy.

    A host without KVM (most Pi boards, and some unaccelerated x86 hosts)
    will boot the emulator meaningfully slower than an accelerated x86_64
    host. Scaling the default avoids the two failure modes of a single
    fixed value: giving up too early on slow hosts, or making fast hosts
    wait needlessly long on the rare genuine failure.

    Override with ANDROID_STARTUP_TIMEOUT_S for a specific deployment.
    """
    if ANDROID_STARTUP_TIMEOUT_S_OVERRIDE:
        try:
            return float(ANDROID_STARTUP_TIMEOUT_S_OVERRIDE)
        except ValueError:
            pass
    return 30.0 if _kvm_available() else 90.0


# =========================================================================
# 4. DOCKER-COMPOSE-DRIVEN SOURCES (Android Emulator)
# =========================================================================
def _spawn_android_emulator_stack(active_source):
    """Start a docker-compose stack for the Android emulator and bridge its
    audio to the FIFO pipe.

    Works by:
      1. Probing host capability (KVM presence) to decide whether to pass
         /dev/kvm through, and to size the startup timeout accordingly —
         no architecture-specific user configuration required.
      2. Generating a fresh per-session web UI password.
      3. Writing a temporary compose YAML with a shared volume, VNC/web UI
         enabled, and (when available) KVM passthrough.
      4. Launching ``docker compose up -d`` in detached mode (falling back
         to the standalone ``docker-compose`` binary if the v2 plugin isn't
         present).
      5. Waiting for the healthcheck to actually report healthy (fixed —
         see notes above) before starting the audio bridge.
      6. Spawning a tail+ffmpeg bridge process that reads audio PCM from
         the shared volume and appends it to {FIFO_PIPE}.
    """
    global CURRENT_TUNED_CHANNEL

    arch = _detect_host_architecture()
    has_kvm = _kvm_available()
    image = ANDROID_DEFAULT_IMAGE

    android_data_vol = ANDROID_DATA_VOLUME
    # The bridge reads from this path; Android writes here via a named pipe
    # in the shared volume.
    reader_input = "/data/android_output/emulator_audio.pcm"

    # Fresh per-session credential for the web UI (KasmVNC-based image).
    # Stored on `bot` so a Discord command can hand this out once ready,
    # and cleared automatically on teardown (see stop_active_hardware_process).
    web_password = secrets.token_urlsafe(12)
    bot.android_web_password = web_password
    bot.android_web_port = ANDROID_WEB_PORT

    kvm_block = "\n    devices:\n      - /dev/kvm:/dev/kvm" if has_kvm else ""

    compose_yaml = f"""services:
  android:
    image: {image}
    privileged: true
    network_mode: host
    environment:
      - WEB_VNC={"true" if ANDROID_WEB_VNC else "false"}
      - ENABLE_VNC=no
      - CUSTOM_PORT={ANDROID_WEB_PORT}
      - PASSWORD={web_password}
    volumes:
      - {android_data_vol}:/data/android_output{kvm_block}
    tmpfs:
      - /tmp
    healthcheck:
      test: ["CMD", "pgrep", "-f", "emulator64"]
      interval: 10s
      timeout: 5s
      retries: 30
"""

    # Write temporary compose file
    stack_path = os.path.join(COMPOSE_TMP_DIR, f"docker-compose.android.{os.getpid()}.yml")
    with open(stack_path, "w") as f:
        f.write(compose_yaml)

    # Preflight: fail fast with an actionable message instead of quietly
    # burning the whole startup timeout when Docker access is missing.
    if shutil.which("docker") is None:
        print(
            "❌ [Docker] `docker` CLI not found in the bot container. "
            "Confirm the Dockerfile installs `docker.io` and that "
            "/var/run/docker.sock is mounted in docker-compose.yml."
        )
        return

    # Start the compose stack (v2 plugin first, standalone binary fallback —
    # this fallback previously only existed on the teardown path; adding it
    # here too means a host with only the legacy binary still "just works").
    result = subprocess.run(
        ["docker", "compose", "-f", stack_path, "up", "-d"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        result = subprocess.run(
            ["docker-compose", "-f", stack_path, "up", "-d"],
            capture_output=True, text=True
        )
    if result.returncode != 0:
        print(f"❌ [Docker] Android emulator start failed: {result.stderr.strip()}")
        return

    bot.compose_stack_file = stack_path

    # Wait for the container to report genuinely healthy before bridging
    # audio, scaled to host capability (see _android_startup_timeout_s).
    #
    # FIX: the original condition here was
    #   "healthy" not in result.stdout and "Up" in result.stdout
    # which reports ready as soon as the container is merely "Up",
    # independent of whether the healthcheck has actually passed. Corrected
    # to check for "healthy" directly.
    timeout_s = _android_startup_timeout_s()
    ready = False
    elapsed = 0.0
    poll_interval = 0.5
    while elapsed < timeout_s:
        result = subprocess.run(
            ["docker", "compose", "-f", stack_path, "ps", "--format", "{{.Status}}"],
            capture_output=True, text=True
        )
        if result.returncode == 0 and "healthy" in result.stdout:
            ready = True
            break
        time.sleep(poll_interval)
        elapsed += poll_interval

    if not ready:
        print(
            f"⚠️ [Docker] Android container did not report healthy within "
            f"{timeout_s:.0f}s (arch={arch}, kvm={'yes' if has_kvm else 'no'}), "
            f"proceeding anyway"
        )

    # Start the audio bridge: read from shared volume → convert → pipe to FIFO
    bridge_cmd = (
        f"tail -f {reader_input} 2>/dev/null "
        f"| ffmpeg -y -f s16le -ar 48000 -ac 1 -i pipe:0 "
        f"-filter:a \"aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo\" "
        f"-f s16le -ar 48k -ac 2 pipe:1 >> {FIFO_PIPE}"
    )
    bot.compose_reader_process = subprocess.Popen(
        bridge_cmd,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# =========================================================================
# TEARDOWN — extend the existing compose-stack cleanup block inside
# stop_active_hardware_process() with credential cleanup. Splice this in
# place of the equivalent `if getattr(bot, 'compose_stack_file', None):`
# block that already exists there (logic is otherwise unchanged; only the
# `finally` clause gained one line).
# =========================================================================
def _teardown_android_stack_block():
    """Not a standalone function — this is the updated body to paste into
    stop_active_hardware_process() in place of the existing compose-stack
    teardown block. Included here as its own function only so it can be
    pasted as one contiguous chunk; do not call this directly."""
    if getattr(bot, 'compose_stack_file', None):
        try:
            subprocess.run(
                ["docker", "compose", "-f", bot.compose_stack_file, "down", "--timeout", "3"],
                capture_output=True
            )
        except Exception:
            try:
                subprocess.run(
                    ["docker-compose", "-f", bot.compose_stack_file, "down", "--timeout", "3"],
                    capture_output=True
                )
            except Exception:
                pass
        finally:
            try:
                os.unlink(bot.compose_stack_file)
            except FileNotFoundError:
                pass
            bot.compose_stack_file = None
            # NEW: the web UI credential dies with the stack it belongs to.
            bot.android_web_password = None


# =========================================================================
# WEB UI — first working version of the `android-ui` command.
#
# INTEGRATION NOTE: this is written as a bare app_commands.command bound to
# `radio_group` by decorator, matching the group already created in
# bot.py (`radio_group = app_commands.Group(name=COMMAND_NAME, ...)`).
# The actual file may register subcommands differently (e.g. via a cog, or
# with an existing permission-check decorator used by other privileged
# /radio subcommands) — mirror whatever pattern the real file uses for its
# other subcommands rather than pasting this verbatim. See
# INTEGRATION_NOTES.md.
# =========================================================================
@radio_group.command(
    name="android-ui",
    description="Get a link and one-time password for the running Android emulator's web UI",
)
async def android_ui(interaction: discord.Interaction):
    # TODO: gate this behind whatever role/owner check already guards the
    # other privileged /radio subcommands in this file — this command
    # hands out interactive control of a full Android instance.
    if not getattr(bot, "compose_stack_file", None) or not getattr(bot, "android_web_password", None):
        await interaction.response.send_message(
            "The Android emulator source isn't currently running.",
            ephemeral=True,
        )
        return

    host = ANDROID_WEB_HOST or "<set ANDROID_WEB_HOST to your host's LAN IP or domain>"
    port = getattr(bot, "android_web_port", ANDROID_WEB_PORT)
    url = f"http://{host}:{port}"

    # Ephemeral / DM only — never post this in a channel anyone else can read.
    await interaction.response.send_message(
        f"🖥️ Android emulator web UI: {url}\n"
        f"🔑 Password: `{bot.android_web_password}`\n"
        f"_Valid until this source is stopped._",
        ephemeral=True,
    )
