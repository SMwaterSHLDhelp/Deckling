"""Capture the Deck user's microphone from a plugin process that may be running as root."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from typing import Any

Which = Callable[[str], str | None]


def deck_uid() -> int:
    try:
        import pwd

        return int(pwd.getpwnam("deck").pw_uid)
    except (KeyError, ImportError):
        return 1000


def deck_audio_env(euid: int | None = None, uid: int | None = None) -> dict[str, str]:
    """Pulse and PipeWire sockets live under the deck user's runtime dir, not root's."""
    current = os.geteuid() if euid is None else euid
    user_id = uid if uid is not None else (deck_uid() if current == 0 else os.getuid())
    runtime = f"/run/user/{user_id}"
    if current != 0 and os.environ.get("XDG_RUNTIME_DIR"):
        runtime = os.environ["XDG_RUNTIME_DIR"]
    env = {key: value for key, value in os.environ.items() if key != "LD_LIBRARY_PATH"}
    env["XDG_RUNTIME_DIR"] = runtime
    env["PULSE_SERVER"] = f"unix:{runtime}/pulse/native"
    if current == 0:
        env["HOME"] = "/home/deck"
        env["USER"] = "deck"
        env["LOGNAME"] = "deck"
    return env


def capture_argv(which: Which, source: str = "") -> list[str]:
    """Record 16 kHz mono. PipeWire resamples a 48 kHz stereo mic down to that."""
    device = " ".join(str(source or "").split())
    if which("parec"):
        argv = ["parec", "--raw", "--rate=16000", "--channels=1", "--format=s16le", "--latency-msec=50"]
        if device:
            argv.append(f"--device={device}")
        return argv
    if which("pw-record"):
        argv = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16"]
        if device:
            argv.extend(["--target", device])
        argv.append("-")
        return argv
    if which("pw-cat"):
        argv = ["pw-cat", "--record", "--format", "s16", "--rate", "16000", "--channels", "1"]
        if device:
            argv.extend(["--target", device])
        argv.append("-")
        return argv
    raise RuntimeError("Could not find parec or pw-record. Voice control needs PipeWire or PulseAudio.")


def capture_command(which: Which, euid: int | None = None, source: str = "") -> tuple[list[str], dict[str, str]]:
    current = os.geteuid() if euid is None else euid
    env = deck_audio_env(current)
    argv = capture_argv(which, source)
    if current == 0:
        if not which("runuser"):
            raise RuntimeError("Deckling is running as root and cannot switch to the login user for the microphone.")
        user = env.get("USER") or "deck"
        argv = ["runuser", "-u", user, "--preserve-environment", "--", *argv]
    return argv, env


def sources_from_pactl(short_text: str, default_name: str = "") -> list[dict[str, str | bool]]:
    """Pulse source list. Monitor devices are speaker loopbacks, not microphones."""
    default = " ".join(str(default_name or "").split())
    if default.endswith(".monitor"):
        default = ""
    found: list[dict[str, str | bool]] = []
    for line in str(short_text or "").splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 2:
            continue
        name = parts[1].strip()
        if not name or name.endswith(".monitor"):
            continue
        found.append({"name": name, "label": name, "default": name == default})
    if found and not any(item["default"] for item in found):
        found[0]["default"] = True
    return found


def list_input_sources(which: Which, run: Callable[..., Any] | None = None, euid: int | None = None) -> list[dict[str, str | bool]]:
    """The system default input, skipping monitor devices. Empty when pactl is missing."""
    if not which("pactl"):
        return []
    runner = run or subprocess.run
    env = deck_audio_env(euid)
    argv_prefix: list[str] = []
    current = os.geteuid() if euid is None else euid
    if current == 0 and which("runuser"):
        argv_prefix = ["runuser", "-u", env.get("USER") or "deck", "--preserve-environment", "--"]

    def call(args: list[str]) -> str:
        completed = runner([*argv_prefix, *args], capture_output=True, text=True, env=env, check=False)
        return str(getattr(completed, "stdout", "") or "")

    default = call(["pactl", "get-default-source"]).strip()
    short = call(["pactl", "list", "short", "sources"])
    return sources_from_pactl(short, default)


def preferred_source(sources: list[dict[str, str | bool]], saved: str = "") -> str:
    chosen = " ".join(str(saved or "").split())
    names = [str(item.get("name") or "") for item in sources]
    if chosen and chosen in names:
        return chosen
    for item in sources:
        if item.get("default"):
            return str(item.get("name") or "")
    return names[0] if names else ""
