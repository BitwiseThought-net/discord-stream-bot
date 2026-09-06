import os
import sys
import json
import asyncio
import re
import signal
import shutil
import array
import subprocess
import secrets
import time
from typing import Optional
from datetime import datetime, timedelta
import discord
from discord import app_commands
from discord.ext import commands

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

# =========================================================================
# 1. ENVIRONMENT CONFIGURATION & DATA INSTANTIATIONS
# =========================================================================
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
COMMAND_NAME = os.getenv('COMMAND_BASE', 'radio')
RECOVERY_MODE = os.getenv('RECOVERY_MODE', 'resume')

DATA_DIR = os.getenv('DATA_DIR', '/data')
STATE_FILE = os.getenv('STATE_FILE', os.path.join(DATA_DIR, 'state.json'))
SOURCES_CACHE_FILE = os.getenv('SOURCES_CACHE_FILE', os.path.join(DATA_DIR, 'sources_cache.json'))
FIFO_PIPE = os.getenv('FIFO_PIPE', os.path.join(DATA_DIR, 'audio_pipe'))      # Continuous shared audio stream buffer
SOURCES_DIR = os.getenv('SOURCES_DIR', '/sources')            # Configuration directory holding isolated profiles

# --- Android emulator source (docker_compose pipeline_type) ---
# All optional; defaults are chosen so the source works with zero config
# on both ARM (e.g. Raspberry Pi) and x86_64 hosts. See docs/sources.md.
ANDROID_EMULATOR_IMAGE = os.getenv('ANDROID_EMULATOR_IMAGE', 'shmayro/dockerify-android:latest')
ANDROID_EMULATOR_IMAGE_WEB = os.getenv('ANDROID_EMULATOR_IMAGE', 'shmayro/scrcpy-web:latest')
ANDROID_DATA_VOLUME = os.getenv('ANDROID_DATA_VOLUME', 'android_output')
ANDROID_STARTUP_TIMEOUT_S_OVERRIDE = os.getenv('ANDROID_STARTUP_TIMEOUT_S')
COMPOSE_TMP_DIR = os.getenv('COMPOSE_TMP_DIR', '/tmp')
ANDROID_WEB_VNC = os.getenv('ANDROID_WEB_VNC', 'true').lower() in ('1', 'true', 'yes')
ANDROID_WEB_PORT = int(os.getenv('ANDROID_WEB_PORT', '3000'))
ANDROID_WEB_HOST = os.getenv('ANDROID_WEB_HOST')  # bot can't self-detect this; set if using the web UI

CURRENT_TUNED_CHANNEL = "94.9M"
CURRENT_VOLUME_LEVEL = 1.0          # Global persistent tracking memory register for volume level

if not DISCORD_TOKEN:
    print("❌ Critical Error: DISCORD_TOKEN environment variable is missing.")
    sys.exit(1)

# Ensure core operational folders and the Named Pipe exist immediately
os.makedirs(SOURCES_DIR, exist_ok=True)
if not os.path.exists(FIFO_PIPE):
    try:
        os.makedirs(os.path.dirname(FIFO_PIPE), exist_ok=True)
        os.mkfifo(FIFO_PIPE)
    except Exception as e:
        print(f"❌ Failed to construct native FIFO audio stream buffer: {e}")

# Open a permanent, global Read/Write file descriptor to prevent EOF stream closures
try:
    GLOBAL_FIFO_FD = os.open(FIFO_PIPE, os.O_RDWR | os.O_NONBLOCK)
    PIPE_WRITE_HANDLE = os.fdopen(GLOBAL_FIFO_FD, "wb")
except Exception as e:
    print(f"❌ Failed to secure persistent global pipe handles: {e}")
    sys.exit(1)

class StreamBotClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.sleep_tasks = {}  # guild_id -> active sleep worker task
        self.wake_tasks = {}   # guild_id -> active wake worker task
        self.hardware_process = None
        self.sox_process = None
        self.ffmpeg_process = None
        self.compose_stack_file: str | None = None    # temp docker-compose YAML path
        self.compose_reader_process: subprocess.Popen | None = None  # tail|ffmpeg bridge process
        self.android_web_password: str | None = None  # per-session Android web UI password
        self.android_web_port: int | None = None

    async def setup_hook(self):
        self.tree.add_command(radio_group)
        await self.tree.sync()

bot = StreamBotClient()
radio_group = app_commands.Group(name=COMMAND_NAME, description="Audio hardware and SDR streaming matrix controls")


# =========================================================================
# 3. HOST ARCHITECTURE DETECTION (ARM vs x86_64)
# =========================================================================
def _detect_host_architecture() -> str:
    """Return ``arm64`` or ``x86_64``.

    IMPORTANT: this does NOT select a different Android image per
    architecture -- ANDROID_EMULATOR_IMAGE intentionally uses the same tag
    for both, on the assumption that it's a genuine multi-arch manifest
    that Docker itself resolves correctly at pull time (the same way
    official images like ``python:3.12`` work unmodified across amd64 and
    arm64). The goal is a Raspberry Pi and a plain x86_64 host both working
    with zero required configuration. This function exists to tune compose
    *behavior* that legitimately differs by host -- KVM passthrough and
    startup timeout -- not to pick an image.
    """
    import platform
    try:
        machine = platform.machine().lower()
        if machine in ("aarch64", "armv7l", "armv8l"):
            return "arm64"
    except Exception:
        pass
    return "x86_64"


def _kvm_available() -> bool:
    """Return True if /dev/kvm exists and is read/write-accessible.

    Hardware-accelerated x86 emulation needs KVM. Most Raspberry Pi hosts
    won't have it -- the emulator image falls back to software rendering
    in that case, which is slower but still functional. Some x86_64 hosts
    without virtualization enabled also won't have it, so this probes
    directly rather than assuming based on architecture alone.
    """
    return os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK)


def _android_startup_timeout_s() -> float:
    """Capability-aware wait time for the Android stack to report healthy.

    A host without KVM (most Pi boards, and some unaccelerated x86 hosts)
    boots the emulator meaningfully slower than an accelerated x86_64
    host. Override with ANDROID_STARTUP_TIMEOUT_S to force a specific
    value regardless of host capability.
    """
    if ANDROID_STARTUP_TIMEOUT_S_OVERRIDE:
        try:
            return float(ANDROID_STARTUP_TIMEOUT_S_OVERRIDE)
        except ValueError:
            pass
    return 30.0 if _kvm_available() else 90.0


# =========================================================================
# 2. PERSISTENT LOCAL FILE STATE WRAPPERS
# =========================================================================
def save_stream_state(guild_id: int, channel_id: int, selected_source: str = "test_signal",
                       selected_device: str = None, is_active: bool = True):
    """Serializes absolute tracking boundaries using explicit string tokens instead of indices.

    NOTE: `selected_source` (the profile "type", e.g. "usb_mic") is NOT unique when a
    single profile fans out into multiple discovered hardware entries (e.g. 4 USB mics
    all share type "usb_mic" but differ by `device`, e.g. plughw:0,0 vs plughw:3,0).
    We must also persist `selected_device` so the exact hardware entry the user picked
    can be recovered later, instead of always resolving to the first entry with a
    matching type.
    """
    global CURRENT_TUNED_CHANNEL, CURRENT_VOLUME_LEVEL
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        payload = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "selected_source": selected_source,
            "selected_device": selected_device,
            "tuned_frequency": CURRENT_TUNED_CHANNEL,
            "volume_level": CURRENT_VOLUME_LEVEL,
            "is_active": is_active
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"⚠️ [State Storage] Failed writing configuration payload: {e}")

def clear_stream_state():
    """Toggles active connection tracking keys to false instead of dropping the file layout."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
            data["is_active"] = False
            with open(STATE_FILE, 'w') as f:
                json.dump(data, f)
    except Exception as e:
        print(f"⚠️ [State Storage] Failed updating connection state parameters: {e}")
# =========================================================================
# 3. SELF-HEALING CONFIGURATION-DRIVEN HARDWARE DISCOVERY PROTOCOLS
# =========================================================================
def self_heal_test_signal_profile():
    """Enforces absolute baseline system stability by auto-healing the fallback matrix file if deleted."""
    target_path = os.path.join(SOURCES_DIR, "test_signal.json")
    if not os.path.exists(target_path):
        print("📁 [Self-Healing] test_signal.json missing from profiles folder. Re-seeding baseline code...")
        payload = {
            "type": "test_signal",
            "description": "🛠️ Diagnostic Test Signal (Analog Calibration Tone)",
            "discovery_trigger": "always_available",
            "pipeline_template": "ffmpeg -y -f lavfi -i \"sine=frequency=440:sample_rate=48000\" -f s16le -ar 48k -ac 2 pipe:1 >> {fifo_pipe}"
        }
        try:
            with open(target_path, 'w') as f:
                json.dump(payload, f, indent=4)
        except Exception as e:
            print(f"⚠️ [Self-Healing] Failed to write fallback matrix file layout profile: {e}")

def load_matrix_source_profiles():
    """Reads profile files from /sources with purely read-only permissions, executing self-healing first."""
    self_heal_test_signal_profile()
    profiles = {}

    for filename in sorted(os.listdir(SOURCES_DIR)):
        if filename.endswith(".json"):
            try:
                with open(os.path.join(SOURCES_DIR, filename), 'r') as f:
                    data = json.load(f)
                    if "type" in data:
                        profiles[data["type"]] = data
            except Exception as e:
                print(f"⚠️ [Matrix Loader] Failed parsing file profile {filename}: {e}")
    return profiles

def discover_hardware_profile():
    """Scans hardware registers and guarantees index 0 is locked exclusively to the diagnostic backdoor."""
    available_sources = []
    matrix_profiles = load_matrix_source_profiles()
    base_dir = "/proc/asound"

    # FIXED: Index 0 is explicitly locked to the virtual test engine baseline entry first
    test_config = matrix_profiles.get("test_signal", {
        "type": "test_signal",
        "description": "🛠️ Diagnostic Test Signal (Analog Calibration Tone)"
    })
    available_sources.append({
        "type": "test_signal",
        "device": "virtual",
        "channels": "2",
        "description": test_config.get("description"),
        "pipeline_type": test_config.get("pipeline_type", "default")
    })

    try:
        usb_check = subprocess.run(["lsusb"], capture_output=True, text=True)
        usb_output = usb_check.stdout.lower()
    except Exception:
        usb_output = ""

    # Scan and append detected hardware profiles behind index 0
    for s_type, config in matrix_profiles.items():
        if s_type == "test_signal":
            continue  # Already locked to position 0

        trigger = config.get("discovery_trigger", "")

        # 1. ALSA PROBE GATES
        if trigger == "alsa_sound_card" and os.path.exists(base_dir):
            try:
                cards = [d for d in os.listdir(base_dir) if d.startswith("card") and os.path.isdir(os.path.join(base_dir, d))]
                for card in sorted(cards):
                    card_index = card.replace("card", "")
                    device_string = f"plughw:{card_index},0"
                    channels = "2"
                    label_template = config.get("description", "USB Microphone ({device})")

                    stream_info = os.path.join(base_dir, card, "usbstream")
                    if not os.path.exists(stream_info):
                        stream_info = os.path.join(base_dir, card, "stream0")
                    if os.path.exists(stream_info):
                        with open(stream_info, 'r') as f:
                            if "1 channel" in f.read().lower():
                                channels = "1"
                                label_template = config.get("mono_description", "USB Mono Microphone ({device})")

                    available_sources.append({
                        "type": s_type,
                        "device": device_string,
                        "channels": channels,
                        "description": label_template.format(device=device_string),
                        "pipeline_type": config.get("pipeline_type", "default")
                    })
            except Exception as e:
                print(f"⚠️ ALSA file matrix scan exception: {e}")

        # 2. SDR PROBE GATES
        elif trigger.startswith("usb_chipset_"):
            target_id = trigger.replace("usb_chipset_", "").lower()
            if target_id in usb_output or "rtl2832" in usb_output:
                available_sources.append({
                    "type": s_type,
                    "device": "rtlsdr",
                    "channels": "1",
                    "description": config.get("description", f"SDR Module Channel Capture ({s_type})"),
                    "pipeline_type": config.get("pipeline_type", "default")
                })

        # 3. ALWAYS-AVAILABLE SOURCES (no physical hardware to probe --
        # e.g. the Android emulator, which is backed by a container, not a
        # device node). test_signal also uses this trigger but is already
        # locked to index 0 above and skipped here via the `continue` at
        # the top of this loop.
        elif trigger == "always_available":
            available_sources.append({
                "type": s_type,
                "device": config.get("device", ""),
                "channels": config.get("channels", "2"),
                "description": config.get("description", s_type),
                "pipeline_type": config.get("pipeline_type", "default")
            })

    try:
        os.makedirs(os.path.dirname(SOURCES_CACHE_FILE), exist_ok=True)
        with open(SOURCES_CACHE_FILE, 'w') as f:
            json.dump(available_sources, f, indent=4)
    except Exception as e:
        print(f"⚠️ Failed writing data cache map layout properties: {e}")

    return available_sources

def probe_device_has_signal(device: str, duration: float = 0.3, rms_threshold: float = 50.0):
    """Records a short raw snippet directly from an ALSA capture device and checks for
    non-silence via RMS amplitude. Used to auto-detect which of several identical
    USB microphone entries is actually the one receiving live audio, since vendor
    ID / card index alone can't distinguish between otherwise-identical hardware.

    Returns a (status, detail) tuple instead of a bare bool:
      status == "signal" : audio captured, RMS above threshold
      status == "silent" : device opened and captured fine, RMS below threshold
      status == "error"  : could not get a real reading at all -- device busy,
                            arecord missing, unsupported format/rate, permission
                            denied on /dev/snd, timeout, etc. This is NOT the same
                            as silence: a previous version of this function caught
                            every one of these failure modes with a blanket
                            `except Exception: return False`, which made a busy
                            or misconfigured device look identical to a genuinely
                            silent one in `/radio input` (list mode) output. Surfacing the
                            failure mode separately lets you tell "nothing plugged
                            in" apart from "the probe itself couldn't run."

    NOTE: if the bot is *currently* streaming from this exact device via the FIFO
    pipeline, arecord will typically fail to open it (device busy) -- that will
    now show up explicitly as an "error" with a busy/in-use detail rather than
    silently reporting "silent".
    """
    if not device or not device.startswith("plughw"):
        return ("error", "not a probeable ALSA device")

    if shutil.which("arecord") is None:
        return ("error", "arecord not found on PATH -- install alsa-utils in the container image")

    try:
        result = subprocess.run(
            ["arecord", "-D", device, "-f", "S16_LE", "-r", "48000",
             "-c", "1", "-d", str(duration), "-t", "raw"],
            capture_output=True, timeout=duration + 2.0
        )
    except subprocess.TimeoutExpired:
        return ("error", "arecord timed out opening the device")
    except Exception as e:
        return ("error", f"failed to launch arecord: {e}")

    if result.returncode != 0:
        stderr_text = result.stderr.decode(errors="ignore").strip()
        last_line = stderr_text.splitlines()[-1] if stderr_text else f"exit code {result.returncode}"
        return ("error", last_line)

    raw = result.stdout
    if len(raw) < 2:
        return ("error", "arecord exited cleanly but returned no audio bytes")

    samples = array.array('h', raw[: len(raw) - (len(raw) % 2)])
    if not samples:
        return ("error", "empty sample buffer after capture")

    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
    if rms > rms_threshold:
        return ("signal", f"rms={rms:.1f}")
    return ("silent", f"rms={rms:.1f}")

def scan_sources_for_signal(sources):
    """Probes every plughw-backed source entry and returns a dict of
    device -> (status, detail), see probe_device_has_signal for status meanings."""
    signal_map = {}
    for src in sources:
        device = src.get("device", "")
        if device.startswith("plughw"):
            signal_map[device] = probe_device_has_signal(device)
    return signal_map
# =========================================================================
# 4. BROADCAST CORE PIPELINE HANDLERS
# =========================================================================
def stop_active_hardware_process():
    """Explicitly terminates all running hardware pipeline process layers completely.

    NOTE: our pipeline_templates are shell strings joined with `|` (e.g.
    "rtl_fm ... | ffmpeg ... >> {fifo_pipe}"). Because they're launched with
    shell=True, the Popen object we hold is a handle to the *shell*, not to
    rtl_fm/sox/ffmpeg themselves. Since there's a pipe involved, the shell
    can't exec() directly into one command -- it forks children for each
    stage and waits on them. Calling proc.terminate()/kill() only signals
    that shell wrapper; the forked children get orphaned and keep running,
    continuing to write audio into the shared FIFO. That's what caused the
    "previous station still playing" / interlaced-audio bug when swapping
    sources or frequencies.

    Fix: spawn_hardware_capture_stream() starts the shell in its own process
    group (start_new_session=True). Here we signal the whole group with
    os.killpg(), which reaches the shell AND every child it forked.
    """
    # Tear down any docker-compose stack first (Android emulator source).
    # Uses subprocess.Popen (never subprocess.run) for consistency with
    # _spawn_android_emulator_stack -- see that function's docstring.
    if getattr(bot, 'compose_stack_file', None):
        try:
            down_proc = subprocess.Popen(
                ["docker", "compose", "-f", bot.compose_stack_file, "down", "--timeout", "3"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            down_proc.wait(timeout=10)
            if down_proc.returncode != 0:
                fallback_proc = subprocess.Popen(
                    ["docker-compose", "-f", bot.compose_stack_file, "down", "--timeout", "3"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                fallback_proc.wait(timeout=10)
        except Exception as e:
            print(f"⚠️ [Docker] docker-compose down failed: {e}")
        finally:
            try:
                os.unlink(bot.compose_stack_file)
            except FileNotFoundError:
                pass
            bot.compose_stack_file = None
            # The web UI credential dies with the stack it belongs to.
            bot.android_web_password = None
            bot.android_web_port = None

    # Also kill any compose reader (tail+ffmpeg bridge) process
    reader = getattr(bot, 'compose_reader_process', None)
    if reader is not None:
        try:
            pgid = os.getpgid(reader.pid)
            os.killpg(pgid, signal.SIGTERM)
            reader.wait(timeout=1.0)
        except Exception:
            try:
                os.killpg(os.getpgid(reader.pid), signal.SIGKILL)
                reader.wait(timeout=1.0)
            except Exception:
                try:
                    reader.kill()
                except Exception:
                    pass
        finally:
            bot.compose_reader_process = None

    for proc_attr in ['ffmpeg_process', 'sox_process', 'hardware_process']:
        proc = getattr(bot, proc_attr)
        if proc is not None:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait(timeout=1.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            setattr(bot, proc_attr, None)


# =========================================================================
# 4. DOCKER-COMPOSE-DRIVEN SOURCES (Android Emulator)
# =========================================================================

def _spawn_android_emulator_stack(active_source):
    """Start a docker-compose stack for the Android emulator and bridge its audio to the FIFO pipe.

    Works by:
    1. Probing host capability (KVM presence) to decide whether to pass
       /dev/kvm through and how long to wait for boot -- no architecture-
       specific configuration required on either ARM or x86_64.
    2. Generating a fresh per-session web UI password.
    3. Writing a temporary compose YAML with shared volume + web UI enabled.
    4. Launching ``docker compose up -d --wait`` so Compose itself blocks
       until the container's own healthcheck passes (or the wait times
       out) -- this is what the container being genuinely "healthy" means,
       rather than a hand-rolled ``docker compose ps`` polling loop.
    5. Spawning a tail+ffmpeg bridge process that reads audio PCM from the
       shared volume and appends it to {fifo_pipe}.

    NOTE: every docker/compose call in this function goes through
    ``subprocess.Popen`` -- never ``subprocess.run``. This is deliberate:
    ``tests/test_bot_commands.py::TestSpawnHardwareCaptureStream::
    test_docker_compose_dispatch_calls_android_stack`` asserts
    ``subprocess.run`` is never called when this source is dispatched. If a
    future edit reintroduces a ``subprocess.run`` call anywhere in this
    function, that test failing is a real regression, not flakiness.
    """
    arch = _detect_host_architecture()
    has_kvm = _kvm_available()
    image = ANDROID_EMULATOR_IMAGE
    image_web = ANDROID_EMULATOR_IMAGE_WEB

    # Shared volume mount point inside the container where Android audio is written
    android_data_vol = ANDROID_DATA_VOLUME
    reader_input = "/data/android_output/emulator_audio.pcm"

    # Fresh per-session credential for the built-in web UI (KasmVNC-based
    # image). Stored on `bot` so the `android-ui` command can hand it out,
    # and cleared automatically on teardown.
    web_password = secrets.token_urlsafe(12)
    bot.android_web_password = web_password
    bot.android_web_port = ANDROID_WEB_PORT

    # Pass /dev/kvm through automatically when present, omit it when it
    # isn't -- no configuration needed on either side.
    kvm_block = "\n    devices:\n      - /dev/kvm:/dev/kvm" if has_kvm else ""

    compose_yaml = f"""services:
  android:
    image: {image}
    container_name: android
    privileged: true
    network_mode: host
    environment:
      - WEB_VNC={"true" if ANDROID_WEB_VNC else "false"}
      - ENABLE_VNC=no
      - CUSTOM_PORT={ANDROID_WEB_PORT}
      - PASSWORD={web_password}
      - RAM_SIZE=2048
      - SCREEN_RESOLUTION=1280x720
      - SCREEN_DENSITY=227
      - ROOT_SETUP=1
      - GAPPS_SETUP=1
      - ARM_TRANSLATION=1
    volumes:
      - {android_data_vol}:/data/android_output{kvm_block}
      - ./data:/data
      - ./extras:/extras
    ports:
      - "5555:5555"
    devices:
      - /dev/kvm
    tmpfs:
      - /tmp
    healthcheck:
      test: ["CMD", "pgrep", "-f", "emulator64"]
      interval: 10s
      timeout: 5s
      retries: 30
  scrcpy-web:
    image: {image_web}
    container_name: android-web
    privileged: true
    ports:
      - "8000:8000"
    depends_on:
      android:
        condition: service_healthy
    command: >
      sh -c "adb connect android:5555 && npm start"
    restart: unless-stopped
"""

    # Write temporary compose file
    stack_path = os.path.join(COMPOSE_TMP_DIR, f"docker-compose.android.{os.getpid()}.yml")
    with open(stack_path, "w") as f:
        f.write(compose_yaml)

    # Register the stack file immediately, before we know whether startup
    # below succeeds -- so a partially-started stack still gets torn down
    # correctly by stop_active_hardware_process() rather than being orphaned.
    bot.compose_stack_file = stack_path

    timeout_s = _android_startup_timeout_s()

    # `--wait` makes Compose itself block until the healthcheck passes (or
    # the wait times out), replacing the previous hand-rolled polling loop
    # -- and its bug, where a container merely "Up (health: starting)" was
    # incorrectly treated as ready.
    up_proc = subprocess.Popen(
        ["docker", "compose", "-f", stack_path, "up", "-d",
         "--wait", "--wait-timeout", str(int(timeout_s))],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        up_proc.wait(timeout=timeout_s + 10)
    except subprocess.TimeoutExpired:
        up_proc.kill()

    if up_proc.returncode != 0:
        stderr_out = ""
        try:
            if up_proc.stderr:
                stderr_out = up_proc.stderr.read().strip()
        except Exception:
            pass
        print(
            f"⚠️ [Docker] Android container did not report healthy within "
            f"{timeout_s:.0f}s (arch={arch}, kvm={'yes' if has_kvm else 'no'})"
            f"{': ' + stderr_out if stderr_out else ''}; proceeding anyway"
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


def spawn_hardware_capture_stream(active_source):
    """Parses shell parameters dynamically from separate JSON profiles and spawns arrays.

    Sources with ``pipeline_type: "docker_compose"`` (e.g. Android emulator) are
    delegated to ``_spawn_android_emulator_stack`` instead of the standard pipeline path.
    """
    global CURRENT_TUNED_CHANNEL
    s_type = active_source["type"]
    pipeline_type = active_source.get("pipeline_type", "default")

    stop_active_hardware_process()

    # Dispatch docker-compose-based sources early
    if pipeline_type == "docker_compose":
        _spawn_android_emulator_stack(active_source)
        return

    matrix_profiles = load_matrix_source_profiles()
    if s_type not in matrix_profiles:
        print(f"❌ [Pipeline Lock] Configuration map profile missing for type: {s_type}")
        return

    raw_template = matrix_profiles[s_type].get("pipeline_template", "")
    if not raw_template:
        print(f"❌ [Pipeline Lock] Explicit template structure empty inside configuration profile: {s_type}")
        return

    compiled_pipeline = raw_template.format(
        frequency=CURRENT_TUNED_CHANNEL,
        device=active_source.get("device", ""),
        channels=active_source.get("channels", "2"),
        fifo_pipe=FIFO_PIPE
    )

    bot.hardware_process = subprocess.Popen(
        compiled_pipeline,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # own process group, so stop_active_hardware_process() can killpg() every stage of the shell pipeline
    )

def resolve_active_source(detected_sources, target_source_type, target_device=None):
    """Resolves a specific hardware entry from the cache.

    A profile `type` (e.g. "usb_mic") can fan out into several distinct hardware
    entries that only differ by `device` (e.g. plughw:0,0 vs plughw:3,0). Matching
    on `type` alone always returns the *first* entry with that type, silently
    collapsing every USB microphone selection onto mic 0. We match on the
    (type, device) pair first, and only fall back to a type-only match when no
    device was specified (or the previously-selected device is no longer present).
    """
    active_source = None
    if target_device is not None:
        active_source = next(
            (s for s in detected_sources
             if s["type"] == target_source_type and s.get("device") == target_device),
            None
        )
    if active_source is None:
        active_source = next((s for s in detected_sources if s["type"] == target_source_type), None)
    if active_source is None:
        active_source = detected_sources[0] if detected_sources else {"type": "test_signal", "description": "Diagnostic Fallback"}
    return active_source

async def execute_stream_pipeline(interaction: discord.Interaction, channel: discord.VoiceChannel,
                                   force_source_type: str = None, force_device: str = None):
    """Binds the voice client loop to our continuous filesystem FIFO stream handle, using string keys."""
    global CURRENT_TUNED_CHANNEL, CURRENT_VOLUME_LEVEL
    target_source_type = "test_signal"
    target_device = None

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                saved_data = json.load(f)
            if "selected_source" in saved_data:
                target_source_type = saved_data["selected_source"]
            target_device = saved_data.get("selected_device")
            if "tuned_frequency" in saved_data:
                CURRENT_TUNED_CHANNEL = saved_data["tuned_frequency"]
            if "volume_level" in saved_data:
                CURRENT_VOLUME_LEVEL = saved_data["volume_level"]
        except Exception:
            pass

    if force_source_type is not None:
        target_source_type = force_source_type
        target_device = force_device

    if not os.path.exists(SOURCES_CACHE_FILE):
        discover_hardware_profile()

    try:
        with open(SOURCES_CACHE_FILE, 'r') as f:
            detected_sources = json.load(f)

        active_source = resolve_active_source(detected_sources, target_source_type, target_device)
    except Exception:
        await interaction.followup.send("❌ Data engine error. Rebuild profiles using `/radio input` with no index.")
        return

    try:
        vc = interaction.guild.voice_client or await channel.connect()

        spawn_hardware_capture_stream(active_source)
        await asyncio.sleep(0.4)

        if not vc.is_playing():
            audio_stream = discord.FFmpegPCMAudio(
                source=FIFO_PIPE,
                before_options="-f s16le -ar 48k -ac 2",
                pipe=False
            )
            transformer = discord.PCMVolumeTransformer(audio_stream, volume=CURRENT_VOLUME_LEVEL)
            vc.play(transformer)

        save_stream_state(interaction.guild.id, channel.id, active_source["type"],
                           selected_device=active_source.get("device"), is_active=True)
        await interaction.followup.send(f"🎙️ Connected! Stream type: **{active_source['description']}**.")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed initializing device link pipeline: {e}")

@radio_group.command(name="start", description="Initialize the active hardware pipeline stream loop")
async def start(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("You must be in a voice channel to start streaming!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    await execute_stream_pipeline(interaction, interaction.user.voice.channel)
@radio_group.command(name="stop", description="Terminate audio capture channels and disconnect voice maps")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message("I am not currently connected to a voice channel.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    if guild_id in bot.sleep_tasks:
        bot.sleep_tasks[guild_id].cancel()
        del bot.sleep_tasks[guild_id]

    stop_active_hardware_process()
    clear_stream_state()

    await vc.disconnect()
    await interaction.response.send_message("🛑 Audio pipeline disconnected and device loops flushed.")

@radio_group.command(name="restart", description="Power-cycle the active hardware pipeline without leaving the voice channel")
async def restart(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("I'm not currently connected to a voice channel. Use `/radio start` instead.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    # Kill the underlying capture pipeline (ffmpeg/rtl_fm/sox and any orphaned
    # children via killpg -- see stop_active_hardware_process) but deliberately
    # leave the voice connection itself alone, so listeners aren't kicked out
    # of the channel for what's meant to be a quick pipeline bounce.
    stop_active_hardware_process()
    if vc.is_playing() or vc.is_paused():
        vc.stop()

    # execute_stream_pipeline re-reads the last saved source/device/frequency
    # from STATE_FILE and re-spawns the hardware process against the voice
    # client we already hold, so this resumes the same station rather than
    # falling back to the test signal.
    await execute_stream_pipeline(interaction, vc.channel)

@radio_group.command(name="volume", description="Scale the volume parameters of the live stream transformer")
async def volume(interaction: discord.Interaction, percentage: int):
    global CURRENT_VOLUME_LEVEL
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("The bot is not currently streaming!", ephemeral=True)
        return

    if not vc.source or not hasattr(vc.source, "volume"):
        await interaction.response.send_message("Volume control wrapper not ready on this stream layout.", ephemeral=True)
        return

    target_volume = max(0.0, min(float(percentage) / 100.0, 2.0))
    vc.source.volume = target_volume
    CURRENT_VOLUME_LEVEL = target_volume

    current_source_type = "test_signal"
    current_device = None
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                saved_data = json.load(f)
            current_source_type = saved_data.get("selected_source", "test_signal")
            current_device = saved_data.get("selected_device")
    except Exception: pass

    save_stream_state(interaction.guild.id, vc.channel.id, current_source_type,
                       selected_device=current_device, is_active=True)
    await interaction.response.send_message(f"🔊 Dynamic playback volume adjusted and saved to **{percentage}%**.")

# =========================================================================
# 5. DEVICE CATALOG SELECTION & DYNAMIC TUNING COMMANDS
# =========================================================================
@radio_group.command(name="input", description="List available sources (no index), or switch the active capture interface by catalog index")
@app_commands.describe(index="Catalog index to switch to. Omit this to re-scan and list all available sources instead.")
async def set_input(interaction: discord.Interaction, index: Optional[int] = None):
    if index is None:
        # ---- LIST MODE: re-scan hardware and display the catalog (formerly /radio list) ----
        await interaction.response.defer(ephemeral=False)
        sources = discover_hardware_profile()
        signal_map = scan_sources_for_signal(sources)

        response = "📡 **Available Hardware Capture Interfaces:**\n"
        visible_count = 0
        error_count = 0

        for idx, src in enumerate(sources):
            if src["type"] == "test_signal":
                continue

            visible_count += 1
            device = src.get("device", "")
            line = f"`{idx}` : {src['description']}"
            if device in signal_map:
                status, detail = signal_map[device]
                if status == "signal":
                    line += f": 🟢 signal detected ({detail})"
                elif status == "silent":
                    line += f": ⚪ no signal ({detail})"
                elif status == "error":
                    line += f": 🟡 probe error: {detail}"
                    error_count += 1
            response += line + "\n"

        if visible_count == 0:
            response += "⚠️ *No physical audio hardware interfaces detected on this station. Falling back to internal system loops.*\n"
        elif error_count > 0:
            response += f"\n⚠️ *{error_count} probe(s) failed to get a real reading, treat those as unknown, not confirmed silent. See the error detail per line.*"
        response += "\n*Run `/radio input <index>` to switch to one of these sources.*"
        await interaction.followup.send(response)
        return

    # ---- SWITCH MODE: an index was supplied, so pick that source (formerly /radio input <index>) ----
    if not os.path.exists(SOURCES_CACHE_FILE):
        await interaction.response.send_message("❌ Error: Device catalog not initialized. Run `/radio input` with no index to scan first.", ephemeral=True)
        return

    try:
        with open(SOURCES_CACHE_FILE, 'r') as f:
            sources = json.load(f)
    except Exception:
        await interaction.response.send_message("❌ Error: Failed to evaluate source registry mapping rules on disk.", ephemeral=True)
        return

    if index < 0 or index >= len(sources):
        await interaction.response.send_message(f"❌ Error: Index must be a valid target between `0` and `{len(sources) - 1}`.", ephemeral=True)
        return

    target_source_type = sources[index]["type"]
    target_device = sources[index].get("device")
    vc = interaction.guild.voice_client
    guild_id = interaction.guild.id if vc else 0
    channel_id = vc.channel.id if vc else 0

    save_stream_state(guild_id, channel_id, target_source_type,
                       selected_device=target_device, is_active=(vc is not None))

    if vc and vc.is_connected():
        await interaction.response.defer(ephemeral=True)
        await execute_stream_pipeline(interaction, vc.channel, force_source_type=target_source_type,
                                       force_device=target_device)
    else:
        await interaction.response.send_message(f"✅ Target capture source locked to configuration file token: **{sources[index]['description']}**.")

@radio_group.command(name="auto", description="Auto-detect and connect to whichever USB microphone is receiving live signal")
async def auto_input(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("You must be in a voice channel to auto-detect and start streaming!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    sources = discover_hardware_profile()
    mic_sources = [s for s in sources if s.get("device", "").startswith("plughw")]

    if not mic_sources:
        await interaction.followup.send("⚠️ No USB microphone interfaces detected to scan.")
        return

    await interaction.followup.send(f"🔎 Probing {len(mic_sources)} microphone interface(s) for live signal...")

    live_source = None
    probe_errors = []
    for src in mic_sources:
        status, detail = probe_device_has_signal(src["device"])
        if status == "signal":
            live_source = src
            break
        if status == "error":
            probe_errors.append(f"{src['device']}: {detail}")

    if live_source is None:
        msg = "⚪ No live signal detected on any USB microphone. Leaving current source unchanged."
        if probe_errors:
            msg += "\n⚠️ Some probes couldn't get a real reading (treat as unknown, not silent):\n" + "\n".join(probe_errors)
        await interaction.followup.send(msg)
        return

    channel = interaction.user.voice.channel
    fake_followup = interaction.followup

    class AutoInteractionProxy:
        """Reuses execute_stream_pipeline's followup.send without deferring twice."""
        def __init__(self, guild, followup):
            self.guild = guild
            self.followup = followup

    proxy = AutoInteractionProxy(interaction.guild, fake_followup)
    await execute_stream_pipeline(proxy, channel, force_source_type=live_source["type"],
                                   force_device=live_source["device"])

@radio_group.command(name="channel", description="Tune the receiver, or scan a band for clear channels ('scan' or 'scan <start>-<end>' in MHz)")
async def tune_channel(interaction: discord.Interaction, frequency: str):
    global CURRENT_TUNED_CHANNEL

    try:
        scan_range = parse_scan_range(frequency)
    except ValueError as e:
        await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
        return

    if scan_range is not None:
        await execute_channel_scan(interaction, scan_range)
        return

    clean_freq = frequency.strip().upper()

    if clean_freq.isdigit() or re.match(r'^\d+\.\d+$', clean_freq):
        clean_freq += "M"

    if not re.match(r'^\d+(\.\d+)?[MK]?$', clean_freq):
        await interaction.response.send_message("⚠️ Invalid format syntax profile. Try layout parameters like `94.9M`, `118.1M`, or `162.4M`.", ephemeral=True)
        return

    CURRENT_TUNED_CHANNEL = clean_freq
    vc = interaction.guild.voice_client

    if vc and vc.is_connected():
        await interaction.response.defer(ephemeral=True)

        current_source_type = "test_signal"
        current_device = None
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r') as f:
                    saved_data = json.load(f)
                current_source_type = saved_data.get("selected_source", "test_signal")
                current_device = saved_data.get("selected_device")
        except Exception:
            pass

        save_stream_state(interaction.guild.id, vc.channel.id, current_source_type,
                           selected_device=current_device, is_active=True)
        await execute_stream_pipeline(interaction, vc.channel, force_source_type=current_source_type,
                                       force_device=current_device)
    else:
        current_source_type = "test_signal"
        current_device = None
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    saved_data = json.load(f)
                current_source_type = saved_data.get("selected_source", "test_signal")
                current_device = saved_data.get("selected_device")
            except Exception: pass
        save_stream_state(0, 0, current_source_type, selected_device=current_device, is_active=False)
        await interaction.response.send_message(f"📡 Tuner frequency baseline channel set to **{clean_freq}** for next SDR stream run.")
# =========================================================================
# 6. TIMED OPERATION SCHEDULERS (SLEEP / WAKE ENGINE)
# =========================================================================
def parse_duration_to_seconds(duration_str: str) -> int:
    """Parses relative time codes or clean absolute AM/PM positional strings."""
    clean_str = duration_str.strip().lower()

    rel_match = re.match(r'^([\d.]+)\s*([smh])$', clean_str)
    if rel_match:
        val = float(rel_match.group(1))
        unit = rel_match.group(2)
        if unit == 's': return int(val)
        if unit == 'm': return int(val * 60)
        if unit == 'h': return int(val * 3600)

    abs_match = re.match(r'^(\d{1,2}):(\d{2})\s*(am|pm)?$', clean_str)
    if abs_match:
        target_hr = int(abs_match.group(1))
        target_mn = int(abs_match.group(2))
        period = abs_match.group(3)

        if period == 'pm' and target_hr < 12: target_hr += 12
        elif period == 'am' and target_hr == 12: target_hr = 0

        now = datetime.now()
        target_time = now.replace(hour=target_hr, minute=target_mn, second=0, microsecond=0)
        if target_time <= now:
            target_time += timedelta(days=1)
        return int((target_time - now).total_seconds())

    raise ValueError("Invalid time match profile formatting.")

async def sleep_timer_worker(guild_id: int, delay: int):
    await asyncio.sleep(delay)
    guild = bot.get_guild(guild_id)
    if guild and guild.voice_client:
        stop_active_hardware_process()
        clear_stream_state()
        await guild.voice_client.disconnect()
    if guild_id in bot.sleep_tasks:
        del bot.sleep_tasks[guild_id]

@radio_group.command(name="sleep", description="Establish an absolute timer target to step down device capture runs")
async def sleep(interaction: discord.Interaction, duration: str):
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message("The bot must be connected to a voice channel to set a sleep timer!", ephemeral=True)
        return

    try:
        seconds = parse_duration_to_seconds(duration)
    except ValueError:
        if any(char in duration.lower() for char in ['x', 'z', 'y']):
            await interaction.response.send_message("⚠️ Unrecognized duration unit. Please use seconds, minutes, or hours.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Invalid time string format. Try inputs like `30m`, `1.5h`, or `11:45pm`.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    if guild_id in bot.sleep_tasks:
        bot.sleep_tasks[guild_id].cancel()

    task = asyncio.create_task(sleep_timer_worker(guild_id, seconds))
    bot.sleep_tasks[guild_id] = task
    await interaction.response.send_message(f"🌙 Sleep timer locked. Audio feeds drop out in **{duration}**.")

async def wake_timer_worker(guild_id: int, channel_id: int, delay: int):
    await asyncio.sleep(delay)
    channel = bot.get_channel(channel_id)
    if channel and isinstance(channel, discord.VoiceChannel):
        class WakeInteractionObject:
            def __init__(self, g, ch):
                self.guild = g
                self.user = discord.Object(id=0)
                self.user.voice = discord.Object(id=0)
                self.user.voice.channel = ch
                self.response = discord.Object(id=0)
            async def defer(self, ephemeral=True): pass
            class followup:
                @staticmethod
                async def send(content): print(f"📢 [Wake Scheduler] {content}")

        fake_interaction = WakeInteractionObject(channel.guild, channel)
        await execute_stream_pipeline(fake_interaction, channel)
    if guild_id in bot.wake_tasks:
        del bot.wake_tasks[guild_id]

@radio_group.command(name="wake", description="Automatically boot and run streams when tracking clocks hit boundaries")
async def wake(interaction: discord.Interaction, duration: str):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("⚠️ You must be inside a voice channel when running this command so the bot knows where to connect!", ephemeral=True)
        return

    try:
        seconds = parse_duration_to_seconds(duration)
    except ValueError:
        await interaction.response.send_message("⚠️ Unrecognized wake duration unit. Use seconds, minutes, or hours.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    channel_id = interaction.user.voice.channel.id

    if guild_id in bot.wake_tasks:
        bot.wake_tasks[guild_id].cancel()

    task = asyncio.create_task(wake_timer_worker(guild_id, channel_id, seconds))
    bot.wake_tasks[guild_id] = task
    await interaction.response.send_message(f"⏰ Wake timer initialized. Broadcasting starts automatically in **{duration}**.")

@radio_group.command(name="android-ui", description="Get a link and one-time password for the running Android emulator's web UI")
async def android_ui(interaction: discord.Interaction):
    # NOTE: this hands out interactive control of a full Android instance.
    # No subcommand in this bot currently has role/permission gating beyond
    # Discord's own slash-command permissions for the guild -- this command
    # follows that same existing pattern rather than introducing a new,
    # inconsistent check. If per-command permissions are added to this bot
    # in the future, this command should be near the front of the list for
    # it (see memory notes / docs/sources.md security section).
    if not getattr(bot, "compose_stack_file", None) or not getattr(bot, "android_web_password", None):
        await interaction.response.send_message(
            "The Android emulator source isn't currently running.", ephemeral=True
        )
        return

    host = ANDROID_WEB_HOST or "<set ANDROID_WEB_HOST in .env to your host's LAN IP or domain>"
    port = getattr(bot, "android_web_port", ANDROID_WEB_PORT)
    url = f"http://{host}:{port}"

    # Ephemeral only -- never posted where anyone else in the channel can see it.
    await interaction.response.send_message(
        f"🖥️ Android emulator web UI: {url}\n"
        f"🔑 Password: `{bot.android_web_password}`\n"
        f"_Valid until this source is stopped._",
        ephemeral=True,
    )

# =========================================================================
# 7. CRASH RECOVERY LIFECYCLES & STARTUP HOOKS
# =========================================================================
@bot.event
async def on_ready():
    global CURRENT_TUNED_CHANNEL, CURRENT_VOLUME_LEVEL
    print(f"🤖 Automated profile online. Logged in as: {bot.user.name}")

    if RECOVERY_MODE == "stay_disconnected":
        print("🔄 [Recovery] Stay disconnected policy enforced. Skipping historical trace loops.")
        return

    if not os.path.exists(STATE_FILE):
        print("🔄 [Recovery] Clean boot pipeline detected. No data traces saved to disk.")
        return

    try:
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)

        if "tuned_frequency" in data:
            CURRENT_TUNED_CHANNEL = data["tuned_frequency"]
        if "volume_level" in data:
            CURRENT_VOLUME_LEVEL = data["volume_level"]

        current_source_type = data.get("selected_source", "test_signal")
        current_device = data.get("selected_device")

        if not data.get("is_active", True):
            print(f"🔄 [Recovery] Found dormant profile configuration parameters. Caching source token {current_source_type}, baseline {CURRENT_TUNED_CHANNEL} without auto-connecting.")
            return

        guild_id = data.get("guild_id")
        channel_id = data.get("channel_id")

        channel = bot.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.VoiceChannel):
            print("🔄 [Recovery] Saved channel context is invalid or deleted. Wiping trace file mappings.")
            clear_stream_state()
            return

        print(f"🔄 [Recovery] Resuming broadcast on target channel map: {channel.name} using engine token: {current_source_type}")

        class SynthesizedInteraction:
            def __init__(self, g, ch):
                self.guild = g
                self.user = discord.Object(id=0)
                self.user.voice = discord.Object(id=0)
                self.user.voice.channel = ch
                self.response = discord.Object(id=0)
            async def defer(self, ephemeral=True): pass
            class followup:
                @staticmethod
                async def send(content): print(f"📢 [Recovery Notice] {content}")

        fake_interaction = SynthesizedInteraction(channel.guild, channel)
        await execute_stream_pipeline(fake_interaction, channel, force_source_type=current_source_type,
                                       force_device=current_device)
        print("🔄 [Recovery] State resume completed successfully.")
    except Exception as e:
        print(f"❌ [Recovery] Internal failure processing recovery routine payload: {e}")
        clear_stream_state()

# =========================================================================
# 8. FFT-BASED CLEAR CHANNEL SCANNER
# =========================================================================
SCAN_DEFAULT_START_MHZ = 88.0
SCAN_DEFAULT_END_MHZ = 108.0
SCAN_SAMPLE_RATE_HZ = 2_400_000        # rtl-sdr's standard stable sample rate
SCAN_CAPTURE_SECONDS = 0.25            # raw IQ capture window per sweep step
SCAN_FFT_SIZE = 4096
SCAN_STEP_OVERLAP = 0.9                # step by 90% of sample rate so band edges aren't missed
SCAN_PEAK_THRESHOLD_DB = 12.0          # dB above a step's own noise floor to count as a channel
SCAN_MIN_CHANNEL_SPACING_HZ = 200_000  # merge candidates closer than this, matches FM channel spacing
SCAN_MAX_SPAN_MHZ = 60.0               # sanity cap so a mistyped range can't trigger a runaway scan
SCAN_MAX_STEPS = 120

def parse_scan_range(frequency_arg: str):
    """Parses a '/radio channel' argument of 'scan' or 'scan <start>-<end>' (MHz)
    into a (start_hz, end_hz) tuple. Returns None if the argument isn't a scan
    request at all, so the caller falls through to normal single-frequency tuning.
    Raises ValueError on a malformed or out-of-bounds range."""
    clean = frequency_arg.strip().lower()
    match = re.match(r'^scan(?:\s+(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*m?)?$', clean)
    if not match:
        return None

    if match.group(1) and match.group(2):
        start_mhz = float(match.group(1))
        end_mhz = float(match.group(2))
    else:
        start_mhz = SCAN_DEFAULT_START_MHZ
        end_mhz = SCAN_DEFAULT_END_MHZ

    if end_mhz <= start_mhz:
        raise ValueError("scan end frequency must be greater than the start frequency, e.g. `scan 88-108`")
    if (end_mhz - start_mhz) > SCAN_MAX_SPAN_MHZ:
        raise ValueError(f"scan span is capped at {SCAN_MAX_SPAN_MHZ:.0f}MHz per run, try narrowing the range")

    return (start_mhz * 1_000_000, end_mhz * 1_000_000)

def capture_iq_samples(center_hz: float, sample_rate: int, duration_s: float):
    """Captures raw 8-bit IQ samples from the SDR dongle via rtl_sdr and returns them
    as a complex numpy array centered on baseband. This needs exclusive access to the
    dongle, callers must make sure no hardware pipeline (rtl_fm, etc.) currently holds
    it open, or rtl_sdr will fail to claim the USB interface."""
    num_iq_pairs = int(sample_rate * duration_s)
    cmd = [
        "rtl_sdr", "-f", str(int(center_hz)), "-s", str(sample_rate),
        "-n", str(num_iq_pairs * 2), "-"
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=duration_s + 5.0)
    if result.returncode != 0 or len(result.stdout) < 2:
        stderr_text = result.stderr.decode(errors="ignore").strip()
        last_line = stderr_text.splitlines()[-1] if stderr_text else "rtl_sdr produced no samples"
        raise RuntimeError(last_line)

    raw = np.frombuffer(result.stdout, dtype=np.uint8).astype(np.float64)
    raw = raw[: len(raw) - (len(raw) % 2)]
    iq = (raw - 127.5) / 127.5
    i_samples = iq[0::2]
    q_samples = iq[1::2]
    n = min(len(i_samples), len(q_samples))
    return i_samples[:n] + 1j * q_samples[:n]

def find_peaks_in_step(center_hz: float, sample_rate: int, complex_samples):
    """Runs a windowed FFT over one capture window and returns candidate
    (freq_hz, power_db) peaks that clear the step's own noise floor."""
    if len(complex_samples) < SCAN_FFT_SIZE:
        return []

    window = np.hanning(SCAN_FFT_SIZE)
    windowed = complex_samples[:SCAN_FFT_SIZE] * window
    spectrum = np.fft.fftshift(np.fft.fft(windowed, n=SCAN_FFT_SIZE))
    power_db = 20.0 * np.log10(np.abs(spectrum) + 1e-12)
    freq_offsets = np.fft.fftshift(np.fft.fftfreq(SCAN_FFT_SIZE, d=1.0 / sample_rate))
    freq_bins = freq_offsets + center_hz

    noise_floor_db = float(np.median(power_db))
    threshold = noise_floor_db + SCAN_PEAK_THRESHOLD_DB

    # The exact center frequency carries a DC spike that's a dongle artifact, not
    # a real signal, and would otherwise register as a "channel" on every single
    # step regardless of what's actually tuned in. Exclude a small guard band
    # around it.
    dc_bin = SCAN_FFT_SIZE // 2
    dc_guard_bins = 3

    above = np.where(power_db > threshold)[0]
    if len(above) == 0:
        return []

    # Group contiguous bin runs into single peaks (a real signal typically lights
    # up several adjacent bins), keep only the strongest bin per run.
    peaks = []
    run_start = above[0]
    prev = above[0]
    for b in list(above[1:]) + [None]:
        if b is not None and b == prev + 1:
            prev = b
            continue
        in_dc_guard = (dc_bin - dc_guard_bins <= run_start) and (prev <= dc_bin + dc_guard_bins)
        if not in_dc_guard:
            run = range(run_start, prev + 1)
            best_idx = max(run, key=lambda i: power_db[i])
            peaks.append((float(freq_bins[best_idx]), float(power_db[best_idx])))
        if b is not None:
            run_start = b
            prev = b
    return peaks

def merge_nearby_channels(candidates):
    """Collapses candidate peaks within SCAN_MIN_CHANNEL_SPACING_HZ of each other
    (e.g. the same station seen from two overlapping sweep steps) into a single
    entry, keeping whichever reading was strongest."""
    if not candidates:
        return []
    candidates = sorted(candidates, key=lambda c: c[0])
    merged = [candidates[0]]
    for freq_hz, power_db in candidates[1:]:
        last_freq, last_power = merged[-1]
        if freq_hz - last_freq <= SCAN_MIN_CHANNEL_SPACING_HZ:
            if power_db > last_power:
                merged[-1] = (freq_hz, power_db)
        else:
            merged.append((freq_hz, power_db))
    return merged

def scan_for_clear_channels_sync(start_hz: float, end_hz: float):
    """Sweeps [start_hz, end_hz) in sample-rate-sized steps, running an FFT over each
    capture window to build a power spectrum, and returns a sorted list of
    {"frequency": "94.9M", "power_db": float} channel catalog entries.

    This is the blocking implementation (rtl_sdr subprocess calls plus numpy FFT
    work) -- callers must run it off the bot's event loop, e.g. via
    asyncio.to_thread(), or it will stall every other Discord interaction for the
    duration of the sweep.
    """
    step_hz = SCAN_SAMPLE_RATE_HZ * SCAN_STEP_OVERLAP
    span_hz = end_hz - start_hz
    num_steps = min(SCAN_MAX_STEPS, max(1, int(span_hz / step_hz) + 1))

    all_candidates = []
    for step in range(num_steps):
        center_hz = start_hz + (SCAN_SAMPLE_RATE_HZ / 2.0) + (step * step_hz)
        if center_hz - (SCAN_SAMPLE_RATE_HZ / 2.0) > end_hz:
            break
        try:
            samples = capture_iq_samples(center_hz, SCAN_SAMPLE_RATE_HZ, SCAN_CAPTURE_SECONDS)
        except Exception as e:
            print(f"⚠️ [Scan] Skipping step at {center_hz / 1e6:.3f}MHz, capture failed: {e}")
            continue
        all_candidates.extend(find_peaks_in_step(center_hz, SCAN_SAMPLE_RATE_HZ, samples))

    merged = merge_nearby_channels(all_candidates)
    catalog = []
    for freq_hz, power_db in merged:
        if freq_hz < start_hz or freq_hz > end_hz:
            continue
        freq_mhz = round(freq_hz / 1_000_000, 1)
        catalog.append({"frequency": f"{freq_mhz}M", "power_db": round(power_db, 1)})

    catalog.sort(key=lambda c: float(c["frequency"].rstrip("M")))
    return catalog

async def execute_channel_scan(interaction: discord.Interaction, scan_range):
    """Command-facing wrapper: validates tooling is present, frees the SDR dongle
    from any active pipeline, runs the blocking sweep off-thread, and posts the
    resulting channel catalog back to the channel."""
    start_hz, end_hz = scan_range
    start_mhz = start_hz / 1_000_000
    end_mhz = end_hz / 1_000_000

    if shutil.which("rtl_sdr") is None:
        await interaction.response.send_message(
            "❌ `rtl_sdr` not found on PATH, install the rtl-sdr tools package in the container image to use channel scanning.",
            ephemeral=True
        )
        return
    if not NUMPY_AVAILABLE:
        await interaction.response.send_message(
            "❌ `numpy` is not installed, it's required to run the FFT over captured samples for channel scanning.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=False)

    # Scanning needs exclusive access to the dongle. If a pipeline is currently
    # streaming from it, free it first rather than letting rtl_sdr fail to claim
    # the USB interface partway through the sweep.
    was_streaming = bot.hardware_process is not None
    if was_streaming:
        stop_active_hardware_process()
        await interaction.followup.send("⏸️ Pausing the active pipeline to free the SDR dongle for scanning...")

    await interaction.followup.send(
        f"🔎 Scanning **{start_mhz:.1f}MHz - {end_mhz:.1f}MHz** for clear channels "
        f"(FFT size {SCAN_FFT_SIZE}, this can take a little while)..."
    )

    try:
        catalog = await asyncio.to_thread(scan_for_clear_channels_sync, start_hz, end_hz)
    except Exception as e:
        await interaction.followup.send(f"❌ Scan failed: {e}")
        return

    if not catalog:
        response = f"📻 No channels above the noise floor detected between **{start_mhz:.1f}MHz** and **{end_mhz:.1f}MHz**.\n"
    else:
        response = f"📻 **Clear Channels Found ({start_mhz:.1f}MHz - {end_mhz:.1f}MHz):**\n"
        for entry in catalog:
            response += f"`{entry['frequency']}` : {entry['power_db']} dB\n"
        response += "\n*Run `/radio channel <frequency>` to tune to one of these.*\n"

    if was_streaming:
        response += "⚠️ *The pipeline that was running before this scan is now stopped. Use `/radio restart` or `/radio start` to resume it.*"

    # Discord caps messages at 2000 characters; split long catalogs across multiple sends.
    for chunk_start in range(0, len(response), 1900):
        await interaction.followup.send(response[chunk_start: chunk_start + 1900])

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)