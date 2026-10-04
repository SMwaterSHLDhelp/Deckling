"""Photo banner, spoken-text cleanup, stop-talking, and a second screen look."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ai_assistant.hearing import HearingEngine
from ai_assistant.imageutil import encode_png
from ai_assistant.screen import CaptureError, _execute, _wait_for_file
from ai_assistant.service import AssistantService
from ai_assistant.store import normalize_hearing
from ai_assistant.vision import retain_latest_image
from ai_assistant.voice import CODE_NOTE, LINK_NOTE, for_speech
from ai_assistant.web_chat import ScreenCapture, iter_with_tools, text_tool_calls, tool_spec
from test_voice_screen import FakeProc, _Host, _rgb


def test_for_speech_keeps_words_and_the_full_reply() -> None:
    spoken = for_speech(
        "See **Malenia**.\n\n"
        "- Dodge left\n"
        "- Hit after the slam\n\n"
        "Guide: https://wiki.example/malenia and `R1`.\n\n"
        "```\nprint('hi')\n```\n\n"
        "e.g. a spear. 🙂"
    )
    assert "*" not in spoken
    assert "#" not in spoken
    assert "`" not in spoken
    assert "http" not in spoken
    assert "🙂" not in spoken
    assert "Dodge left." in spoken
    assert "Hit after the slam." in spoken
    assert "for example a spear." in spoken
    assert CODE_NOTE in spoken
    assert LINK_NOTE in spoken
    assert "the rest is in the chat" not in spoken.lower()
    long = for_speech(("Wait. " * 80).strip())
    assert long.count("Wait.") == 80


def test_stop_drops_sentences_that_have_not_started(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    engine = AssistantService(str(tmp_path / "settings"), str(tmp_path / "runtime")).voice
    engine.store.update_voice({"voice_enabled": True})
    spoken: list[bytes] = []

    def popen(args, **kwargs):
        proc = FakeProc(args)
        proc.kwargs_env = kwargs.get("env") or {}
        if args and args[0] == "paplay":

            def wait(timeout=None):  # noqa: ARG001
                engine._stop.set()
                return 0

            proc.wait = wait  # type: ignore[method-assign]
        original = proc.stdin.write

        def write(data):
            spoken.append(bytes(data))
            return original(data)

        proc.stdin.write = write  # type: ignore[method-assign]
        return proc

    engine.fetch = lambda _url, dest: __import__("pathlib").Path(dest).write_bytes(b"x")
    engine.popen = popen
    engine.which = lambda name: "/usr/bin/paplay" if name == "paplay" else None
    engine._ensure_piper = lambda: "/tmp/piper"  # type: ignore[method-assign]
    engine._ensure_piper_voice = lambda _voice: ("/tmp/voice.onnx", "/tmp/voice.onnx.json")  # type: ignore[method-assign]
    monkeypatch.setattr("ai_assistant.voice._sample_rate", lambda _path: 22050)
    result = engine.speak_blocking("First sentence. Second sentence.", force=True)
    assert result.get("stopped") is True
    assert spoken == [b"First sentence."]
    assert engine.is_speaking() is False


def test_push_to_talk_and_voice_stop_do_not_start_a_recording(tmp_path) -> None:
    service = AssistantService(str(tmp_path / "settings"), str(tmp_path / "runtime"))
    started: list[str] = []
    service.hearing.begin_ptt = lambda: started.append("ptt")  # type: ignore[method-assign]
    service.voice._speaking = True
    stopped = service.push_to_talk()
    assert stopped["ok"] is True
    assert stopped["stopped"] is True
    assert started == []
    service.voice._speaking = True
    service._hearing_command("stop_talking", "stop")
    assert service.voice.is_speaking() is False
    _sessions, current = service.store.ensure_session()
    assert all(item.get("content") != "Cancelled." for item in current["messages"])


def test_echo_while_speaking_is_not_a_message(tmp_path) -> None:
    commands: list[tuple[str, str]] = []
    engine = HearingEngine(
        AssistantService(str(tmp_path / "settings"), str(tmp_path / "runtime")).store,
        on_command=lambda action, text: commands.append((action, text)),
        speaking=lambda: True,
    )
    engine.update({"wake_enabled": True})
    assert engine.dispatch("the glowing door is on the left") == "ignore"
    assert engine.dispatch("shut up") == "stop_talking"
    assert commands == [("stop_talking", "shut up")]
    assert engine.public()["wake_enabled"] is True
    assert engine.public()["phase"] == "listening"


def test_screen_tool_is_gated_and_attaches_jpeg(tmp_path) -> None:
    names = {spec["name"] for spec in tool_spec()}
    assert "look_at_screen" not in names
    assert "look_at_screen" in {spec["name"] for spec in tool_spec(include_screen=True)}
    hidden = text_tool_calls('<tool_call>{"name":"look_at_screen","arguments":{}}</tool_call>')
    assert hidden == []
    grabbed: list[int] = []

    def grab(_arguments):
        grabbed.append(1)
        return json.dumps({"ok": True}), b"\xff\xd8jpeg"

    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length))
            seen.append(payload)
            fed = any(
                item.get("role") == "tool" or "screenshot you just took" in json.dumps(item.get("content"))
                for item in payload.get("messages") or []
            )
            if fed:
                raw = b'data: {"choices":[{"delta":{"content":"A red door."}}]}\n\ndata: [DONE]\n\n'
            else:
                body = '<tool_call>{"name":"look_at_screen","arguments":{}}</tool_call>'
                raw = (
                    b'data: {"choices":[{"delta":{"content":'
                    + json.dumps(body).encode()
                    + b"}}]}\n\ndata: [DONE]\n\n"
                )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = {"kind": "llamacpp", "base_url": f"http://127.0.0.1:{server.server_address[1]}", "api_key": ""}
    try:
        from ai_assistant.web import WebClient

        client = WebClient(str(tmp_path), sleep=lambda _seconds: None)
        text = "".join(
            iter_with_tools(
                provider,
                [{"role": "user", "content": "what is on screen"}],
                "qwen",
                threading.Event(),
                client,
                screen=ScreenCapture(grab),
                include_web=False,
            )
        )
    finally:
        server.shutdown()
    assert text == "A red door."
    assert grabbed == [1]
    blob = json.dumps(seen[-1])
    assert "data:image/jpeg;base64," in blob
    assert "look_at_screen" in json.dumps(seen[1]["tools"])


def test_model_grab_announces_before_capture_and_respects_the_toggle(tmp_path) -> None:
    notes: list[str] = []
    service = AssistantService(str(tmp_path / "settings"), str(tmp_path / "runtime"))
    service.screen_grabbers = [lambda _context: (_ for _ in ()).throw(CaptureError("no display"))]
    text, image = service._grab_for_model(notes.append)
    assert notes == ["Taking photo"]
    assert image is None
    assert "no display" in text
    service.save_voice({"screen_capture": False})
    notes.clear()
    refused, shot = service._grab_for_model(notes.append)
    assert notes == []
    assert shot is None
    assert "turned off" in refused
    provider = {"kind": "openai", "id": "cloud", "base_url": "https://example.invalid"}
    assert service._offer_screen_tool(provider, "gpt-4o") is False
    service.save_voice({"screen_capture": True})
    assert service._offer_screen_tool(provider, "gpt-4o") is True
    assert service._offer_screen_tool(provider, "llama3:latest") is False


def test_thinking_tick_is_on_until_the_switch_is_used() -> None:
    assert normalize_hearing(None)["thinking_tick"] is True
    assert normalize_hearing({})["thinking_tick"] is True
    assert normalize_hearing({"wake_enabled": True})["thinking_tick"] is True
    assert normalize_hearing({"thinking_tick": False, "wake_enabled": True})["thinking_tick"] is True
    explicit = normalize_hearing({"thinking_tick": False, "thinking_tick_set": True})
    assert explicit["thinking_tick"] is False
    assert explicit["thinking_tick_set"] is True
    assert normalize_hearing({"thinking_tick": True, "thinking_tick_set": True})["thinking_tick"] is True


def test_old_screenshot_file_is_not_read_again(tmp_path) -> None:
    path = tmp_path / "stale.png"
    path.write_bytes(encode_png(_rgb(2, 2, (1, 2, 3)), 2, 2))
    os.utime(path, (1, 1))
    assert _wait_for_file(str(path), 0.2, not_before=time.time()) is None
    fresh = tmp_path / "fresh.png"
    fresh.write_bytes(encode_png(_rgb(2, 2, (9, 9, 9)), 2, 2))
    assert _wait_for_file(str(fresh), 0.5, not_before=time.time() - 5) == fresh.read_bytes()


def test_capture_timeout_kills_the_child() -> None:
    started = time.time()
    try:
        _execute(["python3", "-c", "import time; time.sleep(30)"], {}, 0.3)
    except OSError as exc:
        assert "timed out" in str(exc)
    else:
        raise AssertionError("the capture tool should time out")
    assert time.time() - started < 3


def test_a_stuck_look_does_not_block_the_next_five(tmp_path) -> None:
    release = threading.Event()
    images: list[int] = []
    posts: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length))
            posts.append(payload)
            count = 0
            for message in payload.get("messages") or []:
                content = message.get("content")
                if isinstance(content, list):
                    count += sum(1 for part in content if isinstance(part, dict) and part.get("type") == "image_url")
            images.append(count)
            if len(posts) == 1:
                release.wait(3)
            raw = f'data: {{"choices":[{{"delta":{{"content":"Shot {len(posts)}."}}}}]}}\n\ndata: [DONE]\n\n'.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = _Host()
    shots = {"n": 0}
    pngs = [encode_png(_rgb(2, 2, (index, 0, 0)), 2, 2) for index in range(1, 6)]

    def grab(_context):
        png = pngs[min(shots["n"], 4)]
        shots["n"] += 1
        return png

    try:
        service = AssistantService(str(tmp_path / "settings"), str(tmp_path / "runtime"), host)
        service.screen_grabbers = [grab]
        saved = service.save_provider(
            {
                "kind": "llamacpp",
                "name": "Local",
                "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
                "default_model": "gpt-4o",
            }
        )
        provider_id = saved["provider"]["id"]

        async def run() -> None:
            first = service.look_at_screen(provider_id, "gpt-4o", "what am I looking at", "req-0", "Hades", "", True)
            assert first["ok"] is True
            await asyncio.sleep(0.15)
            for index in range(1, 5):
                result = service.look_at_screen(
                    provider_id, "gpt-4o", "what am I looking at", f"req-{index}", "Hades", "", True
                )
                assert result["ok"] is True, result
                for _ in range(40):
                    if any(item.get("request_id") == f"req-{index}" and item.get("type") == "chat_done" for item in host.events):
                        break
                    await asyncio.sleep(0.05)
            release.set()
            for _ in range(40):
                if shots["n"] >= 5 and len(posts) >= 5:
                    break
                await asyncio.sleep(0.05)

        asyncio.run(run())
    finally:
        release.set()
        server.shutdown()
    assert shots["n"] == 5
    assert images[:5] == [1, 1, 1, 1, 1]
    later = json.dumps(posts[-1])
    assert "[earlier screenshot]" in later
    assert later.count("data:image/jpeg;base64,") == 1


def test_older_images_become_a_text_note() -> None:
    first = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,YQ=="}}
    second = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,Yg=="}}
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "first"}, first]},
        {"role": "assistant", "content": "red"},
        {"role": "user", "content": [{"type": "text", "text": "second"}, second]},
    ]
    kept = retain_latest_image(messages, multiple=False)
    assert kept[0]["content"] == "first [earlier screenshot]"
    assert isinstance(kept[2]["content"], list)
    assert retain_latest_image(messages, multiple=True) == messages
