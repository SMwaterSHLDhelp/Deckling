"""Mocked request and response contracts for each chat backend."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ai_assistant.providers import iter_text, list_models

Responder = Callable[[str, str], tuple[int, bytes, str]]


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode()


def _ndjson(*rows: dict) -> bytes:
    return "".join(json.dumps(row) + "\n" for row in rows).encode()


@contextmanager
def _server(responder: Responder):
    calls: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            calls.append(
                {
                    "method": method,
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                    "body": raw,
                }
            )
            status, payload, content_type = responder(method, self.path)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def log_message(self, fmt: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], calls
    finally:
        server.shutdown()
        server.server_close()


def _chat(kind: str, port: int, *, key: str = "", base_path: str = "") -> tuple[list[str], str]:
    provider = {
        "kind": kind,
        "base_url": f"http://127.0.0.1:{port}{base_path}",
        "api_key": key,
        "max_tokens": 64,
    }
    models = list_models(provider)
    text = "".join(
        iter_text(
            provider,
            [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hi"}],
            "chosen-model",
            threading.Event(),
        )
    )
    return models, text


def _json_body(call: dict) -> dict:
    return json.loads(call["body"].decode())


def test_openai_lists_chat_models_and_streams_completions() -> None:
    def respond(method: str, path: str) -> tuple[int, bytes, str]:
        if method == "GET" and path == "/v1/models":
            payload = {"data": [{"id": "gpt-4o-mini"}, {"id": "text-embedding-3-small"}]}
            return 200, json.dumps(payload).encode(), "application/json"
        if method == "POST" and path == "/v1/chat/completions":
            return 200, _sse({"choices": [{"delta": {"content": "hello"}}]}), "text/event-stream"
        return 404, b"missing", "text/plain"

    with _server(respond) as (port, calls):
        models, text = _chat("openai", port, key="sk-test", base_path="/v1")

    assert models == ["gpt-4o-mini"]
    assert text == "hello"
    assert calls[0]["path"] == "/v1/models"
    assert calls[0]["headers"]["authorization"] == "Bearer sk-test"
    posted = _json_body(calls[1])
    assert calls[1]["path"] == "/v1/chat/completions"
    assert posted["model"] == "chosen-model"
    assert posted["stream"] is True
    assert posted["max_completion_tokens"] == 64
    assert "max_tokens" not in posted
    assert posted["messages"][0] == {"role": "system", "content": "Be brief."}
    assert posted["messages"][1] == {"role": "user", "content": "Hi"}


def test_anthropic_uses_messages_and_splits_the_system_prompt() -> None:
    def respond(method: str, path: str) -> tuple[int, bytes, str]:
        if method == "GET" and path == "/v1/models":
            return 200, json.dumps({"data": [{"id": "claude-sonnet-4-5"}]}).encode(), "application/json"
        if method == "POST" and path == "/v1/messages":
            event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}
            return 200, _sse(event), "text/event-stream"
        return 404, b"missing", "text/plain"

    with _server(respond) as (port, calls):
        models, text = _chat("anthropic", port, key="sk-ant-test")

    assert models == ["claude-sonnet-4-5"]
    assert text == "hello"
    assert calls[0]["headers"]["x-api-key"] == "sk-ant-test"
    assert calls[0]["headers"]["anthropic-version"] == "2023-06-01"
    assert "authorization" not in calls[0]["headers"]
    posted = _json_body(calls[1])
    assert posted["model"] == "chosen-model"
    assert posted["max_tokens"] == 64
    assert posted["stream"] is True
    assert posted["system"] == "Be brief."
    assert posted["messages"] == [{"role": "user", "content": "Hi"}]


def test_gemini_uses_generate_content_and_an_api_key_header() -> None:
    def respond(method: str, path: str) -> tuple[int, bytes, str]:
        if method == "GET" and path == "/v1beta/models":
            payload = {
                "models": [
                    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
                ]
            }
            return 200, json.dumps(payload).encode(), "application/json"
        if method == "POST" and path == "/v1beta/models/chosen-model:streamGenerateContent?alt=sse":
            event = {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}
            return 200, _sse(event), "text/event-stream"
        return 404, b"missing", "text/plain"

    with _server(respond) as (port, calls):
        models, text = _chat("gemini", port, key="AIza-test", base_path="/v1beta")

    assert models == ["gemini-2.5-flash"]
    assert text == "hello"
    assert calls[0]["headers"]["x-goog-api-key"] == "AIza-test"
    assert "authorization" not in calls[0]["headers"]
    posted = _json_body(calls[1])
    assert posted["systemInstruction"] == {"parts": [{"text": "Be brief."}]}
    assert posted["generationConfig"] == {"maxOutputTokens": 64}
    assert posted["contents"] == [{"role": "user", "parts": [{"text": "Hi"}]}]


def test_xai_uses_openai_chat_with_max_tokens() -> None:
    def respond(method: str, path: str) -> tuple[int, bytes, str]:
        if method == "GET" and path == "/v1/models":
            payload = {"data": [{"id": "grok-4"}, {"id": "text-embedding-3-small"}]}
            return 200, json.dumps(payload).encode(), "application/json"
        if method == "POST" and path == "/v1/chat/completions":
            return 200, _sse({"choices": [{"delta": {"content": "hello"}}]}), "text/event-stream"
        return 404, b"missing", "text/plain"

    with _server(respond) as (port, calls):
        models, text = _chat("xai", port, key="xai-test", base_path="/v1")

    assert models == ["grok-4", "text-embedding-3-small"]
    assert text == "hello"
    assert calls[0]["headers"]["authorization"] == "Bearer xai-test"
    posted = _json_body(calls[1])
    assert posted["max_tokens"] == 64
    assert "max_completion_tokens" not in posted
    assert posted["stream"] is True
    assert posted["messages"][0]["role"] == "system"


def test_ollama_reads_tags_and_streams_chat_lines() -> None:
    def respond(method: str, path: str) -> tuple[int, bytes, str]:
        if method == "GET" and path == "/api/tags":
            return 200, json.dumps({"models": [{"name": "llama3:latest"}]}).encode(), "application/json"
        if method == "POST" and path == "/api/chat":
            body = _ndjson(
                {"message": {"role": "assistant", "content": "hello"}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True},
            )
            return 200, body, "application/x-ndjson"
        return 404, b"missing", "text/plain"

    with _server(respond) as (port, calls):
        models, text = _chat("ollama", port)

    assert models == ["llama3:latest"]
    assert text == "hello"
    assert "authorization" not in calls[0]["headers"]
    show = [call for call in calls if call["path"] == "/api/show"]
    assert show
    chat = [call for call in calls if call["path"] == "/api/chat"]
    assert chat
    posted = _json_body(chat[0])
    assert posted["model"] == "chosen-model"
    assert posted["stream"] is True
    assert posted["options"] == {"num_predict": 64}
    assert posted["messages"][1] == {"role": "user", "content": "Hi"}


def test_llamacpp_adds_v1_and_reads_reasoning_content() -> None:
    def respond(method: str, path: str) -> tuple[int, bytes, str]:
        if method == "GET" and path == "/v1/models":
            return 200, json.dumps({"data": [{"id": "qwen-test"}]}).encode(), "application/json"
        if method == "POST" and path == "/v1/chat/completions":
            return 200, _sse({"choices": [{"delta": {"content": None, "reasoning_content": "hello"}}]}), "text/event-stream"
        return 404, b"missing", "text/plain"

    with _server(respond) as (port, calls):
        models, text = _chat("llamacpp", port)

    assert models == ["qwen-test"]
    assert text == "hello"
    assert calls[0]["path"] == "/v1/models"
    assert any(call["path"] == "/props" for call in calls)
    posted = _json_body(next(call for call in calls if call["path"] == "/v1/chat/completions"))
    assert posted["max_tokens"] == 64
    assert "max_completion_tokens" not in posted
    assert posted["model"] == "chosen-model"


def test_custom_openai_compatible_uses_models_and_chat_completions() -> None:
    def respond(method: str, path: str) -> tuple[int, bytes, str]:
        if method == "GET" and path == "/v1/models":
            return 200, json.dumps({"data": [{"id": "local-model"}]}).encode(), "application/json"
        if method == "POST" and path == "/v1/chat/completions":
            return 200, _sse({"choices": [{"delta": {"content": "hello"}}]}), "text/event-stream"
        return 404, b"missing", "text/plain"

    with _server(respond) as (port, calls):
        models, text = _chat("custom", port, key="local-key", base_path="/v1")

    assert models == ["local-model"]
    assert text == "hello"
    assert calls[0]["headers"]["authorization"] == "Bearer local-key"
    posted = _json_body(calls[1])
    assert posted["max_tokens"] == 64
    assert posted["stream"] is True
    assert posted["model"] == "chosen-model"
    assert posted["messages"][-1]["content"] == "Hi"
