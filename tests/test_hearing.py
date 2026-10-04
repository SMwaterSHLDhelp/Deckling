"""Wake word, speech capture, and phrase routing without a microphone or a model download."""

from __future__ import annotations

import array
import io
import os
import stat
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest

from ai_assistant.audio_in import capture_command, deck_audio_env
from ai_assistant.diagnostics import snapshot
from ai_assistant.hearing import (
    FASTER_WORKER,
    WAKE_WORKER,
    HearingEngine,
    _read_until_silence,
    classify_phrase,
    threshold_for,
)
from ai_assistant.interpreter import system_python
from ai_assistant.service import AssistantService
from ai_assistant.store import Store
from ai_assistant.vad import FRAME_MS, RATE, speech_region

WIDTH = RATE * FRAME_MS // 1000 * 2


def _pcm(ms: int, amplitude: int) -> bytes:
    count = RATE * ms // 1000
    samples = array.array("h", [amplitude] * count)
    return samples.tobytes()


def _engine(tmp_path, **kwargs) -> HearingEngine:
    store = Store(str(tmp_path / "settings"), str(tmp_path / "runtime"))
    kwargs.setdefault("python", sys.executable)
    engine = HearingEngine(store, **kwargs)
    engine.autostart = False
    return engine


def _which(*names: str):
    def which(name: str) -> str | None:
        if name in names:
            return f"/usr/bin/{name}"
        return None

    return which


def test_speech_region_closes_after_silence() -> None:
    spoken = _pcm(300, 8000) + _pcm(900, 0)
    region = speech_region(spoken)
    assert region is not None
    start, end = region
    assert start == 0
    assert end == (300 // FRAME_MS + 700 // FRAME_MS) * WIDTH
    assert speech_region(_pcm(300, 8000) + _pcm(200, 0)) is None
    assert speech_region(_pcm(90, 8000) + _pcm(2000, 0)) is None
    assert speech_region(_pcm(500, 0)) is None


def test_recording_stops_on_silence_and_gives_up_when_nobody_talks() -> None:
    spoken = _pcm(300, 8000) + _pcm(900, 0)

    class Stream:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self._offset = 0

        def read(self, count: int) -> bytes:
            chunk = self._data[self._offset : self._offset + count]
            self._offset += len(chunk)
            return chunk

    heard = _read_until_silence(Stream(spoken), threading.Event())
    assert heard.startswith(_pcm(300, 8000)[:WIDTH])
    assert _read_until_silence(Stream(_pcm(4500, 0)), threading.Event()) == b""


def test_phrase_routing() -> None:
    assert classify_phrase("   ", False) == "ignore"
    assert classify_phrase("Stop listening", False) == "stop_listening"
    assert classify_phrase("stop listening please", True) == "stop_listening"
    assert classify_phrase("New chat", False) == "new_chat"
    assert classify_phrase("start a new chat", False) == "new_chat"
    assert classify_phrase("How do I do this?", True) == "screen"
    assert classify_phrase("what am I looking at", False) == "screen"
    assert classify_phrase("Yes!", True) == "confirm"
    assert classify_phrase("go ahead", True) == "confirm"
    assert classify_phrase("do it", True) == "confirm"
    assert classify_phrase("yeah", True) == "confirm"
    assert classify_phrase("yes", False) == "message"
    assert classify_phrase("cancel", True) == "cancel"
    assert classify_phrase("stop", True) == "cancel"
    assert classify_phrase("stop", False) == "message"
    assert classify_phrase("open the map", False) == "message"
    assert classify_phrase("stop", False, True) == "stop_talking"
    assert classify_phrase("stop talking", False, True) == "stop_talking"
    assert classify_phrase("shut up", False, True) == "stop_talking"
    assert classify_phrase("be quiet", False, True) == "stop_talking"
    assert classify_phrase("open the map", False, True) == "ignore"
    assert classify_phrase("what am I looking at", False, True) == "screen"
    assert classify_phrase("stop listening", False, True) == "stop_listening"


def test_sensitivity_maps_onto_a_wake_threshold() -> None:
    assert threshold_for(0) == 0.85
    assert threshold_for(0.5) == 0.55
    assert threshold_for(1) == 0.25
    assert threshold_for(4) == 0.25


def test_capture_as_root_uses_the_deck_session(monkeypatch) -> None:
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/decky/lib")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    argv, env = capture_command(_which("parec", "runuser"), euid=0)
    assert argv[:5] == ["runuser", "-u", "deck", "--preserve-environment", "--"]
    assert "parec" in argv
    assert "--rate=16000" in argv
    assert "--format=s16le" in argv
    assert "LD_LIBRARY_PATH" not in env
    assert env["PULSE_SERVER"].startswith("unix:/run/user/")
    assert env["PULSE_SERVER"].endswith("/pulse/native")
    assert env["XDG_RUNTIME_DIR"] == env["PULSE_SERVER"].removeprefix("unix:").removesuffix("/pulse/native")
    assert env["USER"] == "deck"
    assert env["HOME"] == "/home/deck"

    record, record_env = capture_command(_which("pw-record"), euid=1000)
    assert record[0] == "pw-record"
    assert "runuser" not in record
    assert "LD_LIBRARY_PATH" not in record_env
    with pytest.raises(RuntimeError, match="PipeWire"):
        capture_command(_which(), euid=1000)
    with pytest.raises(RuntimeError, match="deck user"):
        capture_command(_which("parec"), euid=0)


def test_non_root_audio_env_uses_the_runtime_dir(monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/decky/lib")
    env = deck_audio_env(euid=1000, uid=1000)
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert env["PULSE_SERVER"] == "unix:/run/user/1000/pulse/native"
    assert "LD_LIBRARY_PATH" not in env


def test_hearing_settings_survive_a_normal_save_and_do_not_download(tmp_path) -> None:
    fetched: list[str] = []
    engine = _engine(tmp_path, fetch=lambda url, dest, progress=None: fetched.append(url))
    saved = engine.update(
        {
            "wake_enabled": True,
            "sensitivity": 5,
            "wake_model": "hey_deckling",
            "stt_model": "large-v3",
            "battery_saver": True,
            "debug_audio": True,
        }
    )
    assert saved["wake_enabled"] is True
    assert saved["sensitivity"] == 1
    assert saved["wake_model"] == "hey_jarvis"
    assert saved["stt_model"] == "tiny.en"
    assert saved["battery_saver"] is True
    assert engine._thread is None
    assert fetched == []
    engine.store.update_settings("be helpful", "", "")
    config = engine.store.load_config()
    assert config["system_prompt"] == "be helpful"
    assert config["hearing"]["wake_enabled"] is True
    assert config["hearing"]["debug_audio"] is True
    assert config["hearing"]["sensitivity"] == 1
    mode = stat.S_IMODE(os.stat(engine.store.credentials_path).st_mode)
    assert mode == 0o600


def test_install_prefers_faster_whisper_and_keeps_models_in_the_data_dir(tmp_path) -> None:
    destinations: list[str] = []
    envs: list[dict[str, str]] = []

    def fetch(url: str, dest: str, progress=None) -> None:
        destinations.append(dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        Path(dest).write_bytes(b"model")

    def run(command: list[str], env: dict[str, str]) -> None:
        envs.append(env)
        assert "--target" in command
        assert command[command.index("--target") + 1].endswith("/hearing/py")
        if any(part.startswith("openwakeword") for part in command):
            assert "openwakeword==0.4.0" in command

    engine = _engine(tmp_path, fetch=fetch, run=run, machine="x86_64", python="/usr/bin/python")
    public = engine.install()
    assert public["stt_backend"] == "faster-whisper"
    assert public["wake_error"] == ""
    assert "closes after each line" in public["install_message"]
    assert destinations
    assert all(str(tmp_path / "runtime") in dest for dest in destinations)
    assert all("LD_LIBRARY_PATH" not in env and env.get("PYTHONNOUSERSITE") == "1" for env in envs)
    worker = Path(engine.store.runtime_dir, "hearing", "faster_worker.py").read_text(encoding="utf-8")
    assert "download_root" in worker
    assert 'compute_type="int8"' in worker
    assert "os.nice(15)" in WAKE_WORKER
    assert "int8" in FASTER_WORKER


def test_install_falls_back_to_whisper_cpp_when_wheels_fail(tmp_path, monkeypatch) -> None:
    def fetch(url: str, dest: str, progress=None) -> None:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if url.endswith(".tar.gz"):
            payload = b"#!/bin/sh\nprintf '%s\\n' 'heard you'\n"
            info = tarfile.TarInfo("build/whisper-cli")
            info.size = len(payload)
            info.mode = 0o755
            with tarfile.open(dest, "w:gz") as archive:
                archive.addfile(info, io.BytesIO(payload))
            return
        Path(dest).write_bytes(b"model")

    def run(command: list[str], env: dict[str, str]) -> None:
        if "openwakeword" in command or "faster-whisper" in command:
            raise RuntimeError("wheel failed")
        raise AssertionError(command)

    engine = _engine(tmp_path, fetch=fetch, run=run, machine="x86_64")
    public = engine.install()
    assert public["stt_backend"] == "whisper.cpp"
    assert "Push to talk still works" in public["wake_error"]
    binary = Path(engine.store.runtime_dir, "hearing", "whisper", "build", "whisper-cli")
    assert binary.is_file()
    assert os.access(binary, os.X_OK)
    model = Path(engine.store.runtime_dir, "hearing", "whisper", "ggml-tiny.en.bin")
    assert model.is_file()

    def fake_run(argv, **kwargs):
        assert argv[0] == str(binary)
        assert any(item.endswith("ggml-tiny.en.bin") for item in argv)
        assert kwargs["env"]["PULSE_SERVER"].endswith("/pulse/native")
        assert "LD_LIBRARY_PATH" not in kwargs["env"]

        class Completed:
            returncode = 0
            stdout = "heard you\n"
            stderr = ""

        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert engine.transcribe_file(str(tmp_path / "line.wav")) == "heard you"


def test_unknown_whisper_architecture_is_rejected(tmp_path) -> None:
    def fetch(url: str, dest: str, progress=None) -> None:
        raise RuntimeError("offline")

    def run(command: list[str], env: dict[str, str]) -> None:
        raise RuntimeError("wheel failed")

    engine = _engine(tmp_path, fetch=fetch, run=run, machine="mips")
    engine.install()
    assert "no Linux build" in engine.public()["install_message"]


def test_debug_audio_is_kept_at_mode_0600_and_otherwise_deleted(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.update({"debug_audio": True, "stt_backend": "faster-whisper"})
    seen: list[str] = []

    def transcribe(path: str) -> str:
        seen.append(path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        return "yes"

    engine.transcribe_file = transcribe
    engine._finish_pcm(b"\x01\x00" * 80)
    assert seen
    assert Path(seen[0]).is_file()
    assert "debug-audio" in seen[0]

    engine.update({"debug_audio": False})

    def fail(path: str) -> str:
        seen.append(path)
        raise RuntimeError("transcribe failed")

    engine.transcribe_file = fail
    with pytest.raises(RuntimeError, match="transcribe failed"):
        engine._finish_pcm(b"\x01\x00" * 80)
    assert not Path(seen[-1]).exists()
    assert "hearing/tmp" in seen[-1].replace("\\", "/")


def test_dispatch_confirm_cancel_screen_and_stop(tmp_path) -> None:
    commands: list[tuple[str, str]] = []
    notes: list[dict] = []
    engine = _engine(
        tmp_path,
        notify=notes.append,
        on_command=lambda action, text: commands.append((action, text)),
        pending=lambda: True,
    )
    engine.update({"wake_enabled": True})
    assert engine.dispatch("go ahead") == "confirm"
    assert commands[-1] == ("confirm", "Yes, go ahead.")
    assert engine.dispatch("what should I do here") == "screen"
    assert commands[-1][0] == "screen"
    assert engine.dispatch("cancel") == "cancel"
    assert engine.dispatch("stop listening") == "stop_listening"
    assert engine.public()["wake_enabled"] is False
    assert engine.public()["phase"] == "off"
    assert any(item.get("action") == "screen" for item in notes)


def test_service_commands_and_activity_do_not_start_a_download(tmp_path) -> None:
    service = AssistantService(str(tmp_path / "settings"), str(tmp_path / "runtime"))
    service.hearing.autostart = False
    saved = service.save_hearing({"wake_enabled": True, "sensitivity": 0.2, "battery_saver": True})
    assert saved["ok"] is True
    assert saved["hearing"]["wake_model"] == "hey_jarvis"
    assert saved["hearing"]["done_sound"] is True
    assert service.hearing._thread is None
    service.hearing._phase = "listening"
    paused = service.set_hearing_activity(True, False)
    assert paused["hearing"]["phase"] == "paused"
    asleep = service.set_hearing_activity(False, True)
    assert asleep["hearing"]["phase"] == "paused"
    service.hearing._busy = True
    assert service.push_to_talk()["ok"] is True
    service.hearing._busy = False
    service.hearing.stop()
    disabled = service.save_hearing({"ptt_enabled": False, "wake_enabled": False})
    assert disabled["hearing"]["ptt_enabled"] is False
    refused = service.push_to_talk()
    assert refused["ok"] is False
    assert "turned off" in refused["error"]

    _sessions, current = service.store.ensure_session()
    service.store.append_message(current["id"], "assistant", "Should I open the map?")
    assert service._hearing_pending() is True
    service.hearing.dispatch("cancel")
    messages = service.store.load_sessions()["sessions"][0]["messages"]
    assert messages[-1]["content"] == "Cancelled."
    service.store.append_message(current["id"], "assistant", "The door is locked.")
    assert service._hearing_pending() is False
    before = service.store.load_sessions()["current_id"]
    service.hearing.dispatch("new chat")
    assert service.store.load_sessions()["current_id"] != before
    stopped = service.stop_listening()
    assert stopped["hearing"]["wake_enabled"] is False
    assert service.state()["hearing"]["ptt_enabled"] is False


def test_faster_whisper_uses_the_model_name_and_the_data_dir(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    engine.update({"stt_backend": "faster-whisper", "stt_model": "base.en"})
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/decky/lib")
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]

        class Completed:
            returncode = 0
            stdout = "open the chest\n"
            stderr = ""

        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    wav = tmp_path / "line.wav"
    wav.write_bytes(b"RIFF")
    assert engine.transcribe_file(str(wav)) == "open the chest"
    assert captured["argv"][2:5] == ["base.en", str(wav), str(Path(engine.store.runtime_dir, "hearing", "faster"))]
    assert captured["env"]["HF_HOME"].endswith("/hearing/hf")
    assert "LD_LIBRARY_PATH" not in captured["env"]


def test_wake_process_env_drops_decky_library_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/decky/lib")
    seen: list[dict] = []

    class Proc:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b'{"wake": true}\n')

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def popen(argv, **kwargs):
        seen.append(kwargs.get("env") or {})
        return Proc()

    engine = _engine(tmp_path, popen=popen, which=_which("parec"))
    engine.update({"wake_enabled": True, "sensitivity": 1})
    assert engine._listen_once() is True
    assert len(seen) == 2
    assert all("LD_LIBRARY_PATH" not in env for env in seen)
    assert seen[1]["DECKLING_WAKE_THRESHOLD"] == "0.25"
    assert seen[1]["DECKLING_WAKE_MODEL"].endswith("hey_jarvis_v0.1.onnx")
    worker = Path(engine.store.runtime_dir, "hearing", "wake_worker.py").read_text(encoding="utf-8")
    assert "os.nice(15)" in worker
    assert "wakeword_model_paths" in worker
    assert 'inference_framework="onnx"' in worker


def test_loader_log_line_is_not_a_wake(tmp_path) -> None:
    class Proc:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"[main][INFO]: Starting Decky\n")
            self.stderr = io.BytesIO(b"")

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 1

    engine = _engine(tmp_path, popen=lambda *args, **kwargs: Proc(), which=_which("parec"), python="/usr/bin/python3")
    assert engine._listen_once() is False


def test_frozen_loader_is_not_spawned(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("ai_assistant.hearing.frozen_runtime", lambda: True)
    monkeypatch.setattr("ai_assistant.interpreter.frozen_runtime", lambda: True)
    monkeypatch.setattr(os.path, "realpath", lambda path: path)
    engine = _engine(tmp_path, python=sys.executable)
    with pytest.raises(RuntimeError, match="system Python"):
        engine._argv("worker.py")


def test_system_python_skips_the_loader_binary(monkeypatch) -> None:
    monkeypatch.setattr("ai_assistant.interpreter.frozen_runtime", lambda: True)
    monkeypatch.setattr("ai_assistant.interpreter.sys.executable", "/opt/decky/PluginLoader")
    monkeypatch.setattr("ai_assistant.interpreter.os.path.realpath", lambda path: path)
    monkeypatch.setattr("ai_assistant.interpreter.os.access", lambda path, mode: True)

    def which(name: str) -> str | None:
        if name == "python3.13":
            return "/usr/bin/python3"
        return None

    monkeypatch.setattr("ai_assistant.interpreter.shutil.which", which)
    assert system_python() == "/usr/bin/python3"


def test_post_wake_failure_is_isolated_and_recorded(tmp_path) -> None:
    notes: list[dict] = []

    class Proc:
        returncode = 1

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return (b'{"ok": false, "error": "ModuleNotFoundError: No module named audioop"}\n', b"")

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 1

    engine = _engine(
        tmp_path,
        popen=lambda *args, **kwargs: Proc(),
        notify=notes.append,
        python="/usr/bin/python3",
    )
    engine.update({"wake_enabled": True})
    engine._utterance_isolated()
    assert engine._busy is False
    assert any("audioop" in str(item.get("message") or "") for item in notes)
    assert any("audioop" in line for line in snapshot())
    assert engine._phase == "error"


def test_frontend_listening_controls_and_no_bundled_models() -> None:
    hearing = Path("src/hearing.ts").read_text(encoding="utf-8")
    screen = Path("src/screenHelp.ts").read_text(encoding="utf-8")
    panel = Path("src/chat/ChatPanel.tsx").read_text(encoding="utf-8")
    settings = Path("src/settings/HearingSection.tsx").read_text(encoding="utf-8")
    assert "face_x" in hearing
    assert "Push to talk" in hearing
    assert "face_y" in screen
    assert "Push to talk" in panel
    assert "Stop listening" in panel
    assert "Wake word sensitivity" in settings
    assert "hey deckling" in settings
    root = Path(".")
    skipped = {"node_modules", ".git", "dist", "out", ".venv"}
    bundled = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in skipped]
        for name in filenames:
            if name.endswith(".onnx") or name.startswith("ggml-") and name.endswith(".bin"):
                bundled.append(os.path.join(dirpath, name))
    assert bundled == []
