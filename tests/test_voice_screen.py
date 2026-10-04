"""Voice, vision payloads, and screen capture without a Steam Deck."""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import stat
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ai_assistant.imageutil import decode_png, encode_png, jpeg_size, to_jpeg
from ai_assistant.screen import CaptureError, capture_screen, decode_supplied_image, grab_recent_file
from ai_assistant.service import AssistantService
from ai_assistant.vision import SCREEN_PHRASES, jarvis_prompt, model_sees_images, wants_screen_look
from ai_assistant.voice import VoiceEngine, audio_environment, safe_extract


class _Host:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def emit(self, event: str, payload: dict) -> None:
        self.events.append(payload)

    def info(self, message: str, *args: object) -> None:
        return None

    def warning(self, message: str, *args: object) -> None:
        return None


def _rgb(width: int, height: int, color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    return bytes(color) * (width * height)


def test_png_round_trip_and_jpeg_stays_near_1280px() -> None:
    original = _rgb(8, 8)
    png = encode_png(original, 8, 8)
    decoded, width, height = decode_png(png)
    assert (width, height) == (8, 8)
    assert decoded == original
    jpeg = to_jpeg(png)
    assert jpeg.startswith(b"\xff\xd8")
    assert jpeg.endswith(b"\xff\xd9")
    assert jpeg_size(jpeg) == (8, 8)
    assert to_jpeg(jpeg) == jpeg

    wide = encode_png(_rgb(64, 4, (16, 32, 48)), 64, 4)
    resized = to_jpeg(wide, max_edge=32)
    resized_width, resized_height = jpeg_size(resized) or (0, 0)
    assert max(resized_width, resized_height) <= 32
    assert resized_width == 32


def test_oversized_jpeg_is_refused() -> None:
    body = bytes((8,)) + (1000).to_bytes(2, "big") + (2000).to_bytes(2, "big") + bytes((1, 1, 0x11, 0))
    payload = b"\xff\xd8\xff\xc0" + (len(body) + 2).to_bytes(2, "big") + body + b"\xff\xd9"
    with pytest.raises(ValueError, match="1280"):
        to_jpeg(payload)


def test_screen_phrases_and_vision_models() -> None:
    assert wants_screen_look("How do I do this?")
    assert wants_screen_look("what am I looking at")
    assert wants_screen_look("Help me with this, please")
    assert wants_screen_look("What should I do here")
    assert wants_screen_look("look at the screen")
    assert not wants_screen_look("how are you")
    assert not wants_screen_look("help me with the install")
    assert model_sees_images("gpt-4o-mini")
    assert model_sees_images("claude-sonnet-4-5")
    assert model_sees_images("gemini-2.5-flash")
    assert model_sees_images("grok-4")
    assert model_sees_images("llava:latest")
    assert model_sees_images("qwen2.5-vl")
    assert not model_sees_images("text-embedding-3-small")
    assert not model_sees_images("whisper-1")
    assert not model_sees_images("tts-1")
    assert not model_sees_images("gemini-embedding-001")
    assert not model_sees_images("llama3:latest")
    assert not model_sees_images("qwen3.8-flash-next")
    assert "Hades" in jarvis_prompt("Hades")
    assert "no game detected" in jarvis_prompt("")


def test_frontend_capture_steps_match_the_backend_phrases() -> None:
    text = Path("src/screenHelp.ts").read_text(encoding="utf-8")
    for phrase in SCREEN_PHRASES:
        assert phrase in text
    steps = ["hide-qam", "wait", "steam-screenshot", "backend-capture"]
    positions = [text.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "guide" in text and "face_y" in text
    assert "TakeScreenshot" in text and "RequestScreenshot" in text


def test_capture_refuses_a_visible_menu_and_a_disabled_setting(tmp_path) -> None:
    called: list[str] = []

    def grab(_context):
        called.append("grab")
        return encode_png(_rgb(2, 2), 2, 2)

    with pytest.raises(CaptureError, match="Quick Access"):
        capture_screen(qam_hidden=False, enabled=True, runtime_dir=str(tmp_path), grabbers=[grab])
    with pytest.raises(CaptureError, match="turned off"):
        capture_screen(qam_hidden=True, enabled=False, runtime_dir=str(tmp_path), grabbers=[grab])
    assert called == []


def test_gamescope_socket_writes_and_deletes_a_temp_png(tmp_path) -> None:
    runtime = tmp_path / "run"
    shots = tmp_path / "shots"
    runtime.mkdir()
    shots.mkdir()
    sock_path = runtime / "gamescope-0"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    png = encode_png(_rgb(2, 2, (0, 128, 255)), 2, 2)

    def serve() -> None:
        conn, _addr = server.accept()
        data = b""
        while b"\n" not in data:
            data += conn.recv(200)
        dest = data.decode().split(" ", 1)[1].strip()
        Path(dest).write_bytes(png)
        conn.sendall(b"ok\n")
        conn.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        captured = capture_screen(
            qam_hidden=True,
            enabled=True,
            runtime_dir=str(tmp_path / "data"),
            env={"XDG_RUNTIME_DIR": str(runtime)},
            tmp_dir=str(shots),
            timeout=2,
        )
    finally:
        server.close()
        thread.join(timeout=2)
    assert captured == png
    assert list(shots.glob("gamescope-deckling-*.png")) == []
    assert stat.S_ISSOCK(sock_path.stat().st_mode)


def test_pipewire_node_and_recent_file_order(tmp_path) -> None:
    shots = tmp_path / "shots"
    shots.mkdir()
    png = encode_png(_rgb(2, 2, (1, 2, 3)), 2, 2)
    calls: list[str] = []

    def run(args: list[str]) -> str:
        calls.append(args[0])
        if args[0] == "pw-dump":
            return json.dumps(
                [
                    {
                        "id": 7,
                        "info": {"props": {"node.name": "gamescope-game", "media.class": "Video/Source"}},
                    }
                ]
            )
        dest = args[-1]
        Path(dest).write_bytes(png)
        return ""

    empty = tmp_path / "empty"
    empty.mkdir()
    captured = capture_screen(
        qam_hidden=True,
        enabled=True,
        runtime_dir=str(tmp_path / "data"),
        env={"XDG_RUNTIME_DIR": str(empty)},
        run=run,
        tmp_dir=str(shots),
    )
    assert captured == png
    assert calls == ["pw-dump", "ffmpeg"]
    assert list(shots.glob("gamescope-pw-*.png")) == []

    kept = shots / "steam_recent.png"
    removed = shots / "gamescope_recent.png"
    kept.write_bytes(png)
    removed.write_bytes(png)
    os.utime(removed, (50, 50))
    os.utime(kept, (100, 100))
    steam = capture_screen(
        qam_hidden=True,
        enabled=True,
        runtime_dir=str(tmp_path / "data"),
        grabbers=[grab_recent_file],
        tmp_dir=str(shots),
        started=0,
    )
    assert steam == png
    assert kept.exists()
    assert removed.exists()
    kept.unlink()
    only_gamescope = capture_screen(
        qam_hidden=True,
        enabled=True,
        runtime_dir=str(tmp_path / "data"),
        grabbers=[grab_recent_file],
        tmp_dir=str(shots),
        started=0,
    )
    assert only_gamescope == png
    assert not removed.exists()


def test_supplied_image_reads_temp_files_and_leaves_steam_shots(tmp_path, monkeypatch) -> None:
    png = encode_png(_rgb(2, 2), 2, 2)
    temp = tmp_path / "shot.png"
    temp.write_bytes(png)
    assert decode_supplied_image(str(temp), str(tmp_path / "data"), tmp_dir=str(tmp_path)) == png
    assert not temp.exists()

    home = tmp_path / "home"
    steam = home / ".local" / "share" / "Steam" / "userdata" / "1" / "760" / "screenshots"
    steam.mkdir(parents=True)
    saved = steam / "shot.png"
    saved.write_bytes(png)
    monkeypatch.setattr(os.path, "expanduser", lambda path: str(home) if path == "~" else os.path.expanduser(path))
    assert decode_supplied_image(str(saved), str(tmp_path / "data"), tmp_dir=str(tmp_path / "other")) == png
    assert saved.exists()
    with pytest.raises(CaptureError):
        decode_supplied_image("/etc/passwd", str(tmp_path / "data"), tmp_dir=str(tmp_path))


def test_voice_settings_survive_saving_defaults_and_piper_is_not_downloaded_early(tmp_path) -> None:
    service = AssistantService(str(tmp_path / "settings"), str(tmp_path / "runtime"))
    fetched: list[str] = []

    def fetch(url: str, _dest: str) -> None:
        fetched.append(url)

    service.voice.fetch = fetch
    saved = service.save_voice({"voice_enabled": True, "voice_speed": 1.25, "screen_capture": False, "piper_voice": "nope"})
    assert saved["voice"]["piper_voice"] == "en_US-lessac-medium"
    assert saved["voice"]["voice_speed"] == 1.25
    service.save_settings({"system_prompt": "Be nice", "default_provider_id": "", "default_model": ""})
    voice = service.state()["voice"]
    assert voice["voice_enabled"] is True
    assert voice["screen_capture"] is False
    assert voice["voice_speed"] == 1.25
    assert "en_US-lessac-medium" in voice["piper_voices"]
    assert fetched == []


def test_piper_command_uses_the_deck_audio_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    env = audio_environment()
    assert env["PULSE_SERVER"] == "unix:/run/user/1000/pulse/native"
    engine = VoiceEngine(AssistantService(str(tmp_path / "settings"), str(tmp_path / "runtime")).store, machine="x86_64")
    engine.store.update_voice({"voice_enabled": True, "voice_speed": 1.25, "piper_voice": "en_US-amy-medium"})
    archive = tmp_path / "piper.tar.gz"

    def fetch(url: str, dest: str) -> None:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        if url.endswith(".tar.gz"):
            _tar(archive, {"piper/piper": b"#!/bin/sh\n"})
            Path(dest).write_bytes(archive.read_bytes())
            return
        if url.endswith(".onnx.json"):
            Path(dest).write_text(json.dumps({"audio": {"sample_rate": 22050}}), encoding="utf-8")
            return
        Path(dest).write_bytes(b"onnx")

    procs: list[FakeProc] = []

    def popen(args, **kwargs):
        proc = FakeProc(args)
        proc.kwargs_env = kwargs.get("env") or {}
        procs.append(proc)
        return proc

    engine.fetch = fetch
    engine.popen = popen
    engine.which = lambda name: "/usr/bin/paplay" if name == "paplay" else None
    result = engine.speak_blocking("Hello there", force=True)
    assert result["ok"] is True
    piper = next(proc for proc in procs if "--output-raw" in proc.args)
    player = next(proc for proc in procs if proc.args[0] == "paplay")
    assert "--length-scale" in piper.args
    assert piper.args[piper.args.index("--length-scale") + 1] == "0.800"
    assert "en_US-amy-medium.onnx" in " ".join(piper.args)
    assert "--rate=22050" in player.args
    assert piper.kwargs_env["PULSE_SERVER"] == "unix:/run/user/1000/pulse/native"
    assert b"Hello there" in piper.stdin.snapshot
    long = engine.speak_blocking("word " * 500, force=True)
    assert long["ok"] is True
    assert len(procs[-2].stdin.snapshot) > 700
    assert procs[-2].stdin.snapshot.decode().startswith("word word")


def test_kitten_install_failure_falls_back_to_piper(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    service = AssistantService(str(tmp_path / "settings"), str(tmp_path / "runtime"))
    engine = service.voice
    engine.machine = "x86_64"
    engine.store.update_voice({"voice_engine": "kittentts", "voice_enabled": True})

    def run(_args):
        raise RuntimeError("python -m venv is not available")

    monkeypatch.setattr(
        "ai_assistant.runtime_python.ensure_runtime_python",
        lambda *_args, **_kwargs: "/usr/bin/python3",
    )

    def fetch(url: str, dest: str) -> None:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        if url.endswith(".tar.gz"):
            blob = tmp_path / "piper.tar.gz"
            _tar(blob, {"piper/piper": b"bin"})
            Path(dest).write_bytes(blob.read_bytes())
            return
        if url.endswith(".json"):
            Path(dest).write_text(json.dumps({"audio": {"sample_rate": 22050}}), encoding="utf-8")
            return
        Path(dest).write_bytes(b"onnx")

    engine.run = run
    engine.fetch = fetch
    engine.popen = lambda args, **_kwargs: FakeProc(args)
    engine.which = lambda name: "/usr/bin/paplay" if name == "paplay" else None
    spoken = engine.speak_blocking("Still here", force=True)
    assert spoken["ok"] is True
    assert "Piper is still available" in spoken["warning"]
    assert engine.public()["voice_engine"] == "piper"
    assert "venv" in engine.public()["kitten_error"]
    again = engine.retry_kitten()
    assert again["ok"] is False
    assert engine.public()["voice_engine"] == "piper"


def test_unsafe_piper_archive_is_rejected(tmp_path) -> None:
    archive = tmp_path / "bad.tar.gz"
    _tar(archive, {"../evil": b"x"})
    with pytest.raises(ValueError, match="not safe"):
        safe_extract(str(archive), str(tmp_path / "out"))


def test_piper_library_symlinks_extract_and_escapes_do_not(tmp_path) -> None:
    archive = tmp_path / "piper.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        payload = tmp_path / "lib.so.1"
        payload.write_bytes(b"lib")
        tar.add(payload, arcname="piper/libpiper_phonemize.so.1")
        link = tarfile.TarInfo("piper/libpiper_phonemize.so")
        link.type = tarfile.SYMTYPE
        link.linkname = "libpiper_phonemize.so.1"
        tar.addfile(link)
    out = tmp_path / "out"
    safe_extract(str(archive), str(out))
    extracted = out / "piper" / "libpiper_phonemize.so"
    assert extracted.is_symlink()
    assert os.readlink(extracted) == "libpiper_phonemize.so.1"

    escape = tmp_path / "escape.tar.gz"
    with tarfile.open(escape, "w:gz") as tar:
        info = tarfile.TarInfo("piper/escape")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../etc/passwd"
        tar.addfile(info)
    with pytest.raises(ValueError, match="not safe"):
        safe_extract(str(escape), str(tmp_path / "nope"))


def test_idle_unload_closes_a_resident_model(tmp_path) -> None:
    engine = VoiceEngine(AssistantService(str(tmp_path / "settings"), str(tmp_path / "runtime")).store)
    resident = FakeProc(["kitten"])
    engine.popen = lambda *_args, **_kwargs: resident
    engine._resident = resident
    engine._idle_deadline = 10
    engine.clock = lambda: 11
    assert engine.unload_if_idle() is True
    assert resident.killed is True
    assert engine._resident is None


def test_text_only_model_offers_a_switch_without_capturing(tmp_path) -> None:
    calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            calls.append(self.path)
            body = json.dumps({"models": [{"name": "llama3:latest"}, {"name": "llava:latest"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            calls.append("POST")
            self.send_response(500)
            self.end_headers()

        def log_message(self, fmt: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    grabbed: list[str] = []
    try:
        service = AssistantService(str(tmp_path / "settings"), str(tmp_path / "runtime"), _Host())
        saved = service.save_provider(
            {
                "kind": "ollama",
                "name": "Local",
                "base_url": f"http://127.0.0.1:{server.server_address[1]}",
                "default_model": "llama3:latest",
            }
        )
        service.screen_grabbers = [lambda _context: grabbed.append("grab") or encode_png(_rgb(2, 2), 2, 2)]
        result = service.look_at_screen(saved["provider"]["id"], "llama3:latest", "what am I looking at", "req", "Hades", "", True)
    finally:
        server.shutdown()
    assert result["ok"] is False
    assert result["vision"] is False
    assert "only reads text" in result["error"]
    assert result["suggestions"] == ["llava:latest"]
    assert grabbed == []
    assert calls.count("POST") >= 1


def test_screen_look_sends_jpeg_vision_and_does_not_store_it(tmp_path) -> None:
    posted: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            posted.append(json.loads(self.rfile.read(length)))
            body = b'data: {"choices":[{"delta":{"content":"Press the glowing door."}}]}\n\ndata: [DONE]\n\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = _Host()
    png = encode_png(_rgb(4, 4, (9, 8, 7)), 4, 4)
    try:
        service = AssistantService(str(tmp_path / "settings"), str(tmp_path / "runtime"), host)
        service.save_settings({"system_prompt": "USER PROMPT SECRET", "default_provider_id": "", "default_model": ""})
        saved = service.save_provider(
            {
                "kind": "custom",
                "name": "Local",
                "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
                "default_model": "gpt-4o",
            }
        )
        service.screen_grabbers = [lambda _context: png]
        spoken: list[str] = []

        def speak(text: str, force: bool = False) -> dict:
            spoken.append(text)
            return {"ok": True}

        service.voice.speak_blocking = speak  # type: ignore[method-assign]
        service.save_voice({"voice_enabled": True})

        async def run() -> None:
            result = service.look_at_screen(
                saved["provider"]["id"],
                "gpt-4o",
                "how do I do this",
                "req-screen",
                "Hades",
                "",
                True,
            )
            assert result["ok"] is True
            for _ in range(50):
                if any(item.get("type") == "chat_done" for item in host.events):
                    break
                await asyncio.sleep(0.05)
            for _ in range(20):
                if spoken:
                    break
                await asyncio.sleep(0.05)
            await service.shutdown()

        asyncio.run(run())
    finally:
        server.shutdown()

    assert posted
    system = posted[0]["messages"][0]["content"]
    user = posted[0]["messages"][1]["content"]
    assert "Hades" in system
    assert "Jarvis" in system
    assert "USER PROMPT SECRET" not in json.dumps(posted)
    assert isinstance(user, list)
    assert user[1]["type"] == "image_url"
    assert user[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    done = next(item for item in host.events if item.get("type") == "chat_done")
    assert done["text"] == "Press the glowing door."
    assert done["messages"][-2]["content"] == "[Looking at the screen] how do I do this"
    blob = Path(service.store.sessions_path).read_text(encoding="utf-8")
    assert "data:image" not in blob
    assert "JFIF" not in blob
    assert user[1]["image_url"]["url"] not in blob
    assert spoken == ["Press the glowing door."]
    assert any(item.get("type") == "toast" and item.get("message") == "Taking photo" for item in host.events)
    assert any(item.get("message") == "Looking at your screen..." and item.get("type") == "toast" for item in host.events)
    hidden = service.look_at_screen(saved["provider"]["id"], "gpt-4o", "look", "req-2", "", "", False)
    assert hidden["ok"] is False
    assert "Quick Access" in hidden["error"]
    service.save_voice({"screen_capture": False})
    disabled = service.look_at_screen(saved["provider"]["id"], "gpt-4o", "look", "req-3", "", "", True)
    assert "turned off" in disabled["error"]
    service.save_voice({"screen_capture": True})
    assert service._last_jpeg
    saved_shot = service.save_last_screenshot()
    assert saved_shot["ok"] is True
    shot_path = Path(saved_shot["path"])
    assert shot_path.exists()
    assert stat.S_IMODE(shot_path.stat().st_mode) == 0o600
    assert "saved-screenshots" in str(shot_path)
    sessions_after = Path(service.store.sessions_path).read_text(encoding="utf-8")
    assert shot_path.read_bytes()[:4] != b"" and shot_path.read_bytes() not in sessions_after.encode()


class _Stdin(io.BytesIO):
    def close(self) -> None:
        self.snapshot = self.getvalue()
        super().close()


class FakeProc:
    def __init__(self, args: list[str]) -> None:
        self.args = args
        self.pid = 4242
        self.stdin = _Stdin()
        self.stdout = io.BytesIO(b"\x00\x00" * 8)
        self.kwargs_env: dict[str, str] = {}
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True


def _tar(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
