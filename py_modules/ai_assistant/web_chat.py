"""Let a chat model call web_search and fetch_page, streaming the text it produces."""

from __future__ import annotations

import base64
import json
import re
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from typing import Any

from .http_util import HttpError, iter_lines, join_url
from .providers import (
    ANTHROPIC_VERSION,
    _auth_headers,
    _base,
    _gemini_headers,
    _llamacpp_default_model,
    _max_tokens,
    _openai_root,
    require_credentials,
)
from .sse import iter_json_lines, iter_sse_json
from .vision import keeps_multiple_images, retain_latest_image
from .web import WebClient

TOOL_KINDS = {"openai", "anthropic", "gemini", "xai", "ollama", "llamacpp", "hermes", "custom"}
_MAX_ROUNDS = 3
_HINT = (
    "You can call web_search and fetch_page when a fact about the game would help. "
    "Prefer Fandom or wiki.gg, PCGamingWiki, Steam guides, and the Steam store."
)
_SCREEN_HINT = (
    "You can call look_at_screen when a screenshot would answer the question. "
    "The app shows a Taking photo banner before it captures, so the shot is never silent."
)

Executor = Callable[[str, dict[str, Any]], str]
Status = Callable[[str], None]


class ToolsUnsupported(Exception):
    """The provider rejected the tools field. The caller streams a normal reply instead."""


def tool_spec(*, include_web: bool = True, include_screen: bool = False) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    if include_web:
        specs.extend(
            [
                {
                    "name": "web_search",
                    "description": "Search the web. Use the game name plus what you need to know.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
                {
                    "name": "fetch_page",
                    "description": "Read one public page and return its title and text.",
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                    },
                },
            ]
        )
    if include_screen:
        specs.append(
            {
                "name": "look_at_screen",
                "description": (
                    "Take a screenshot of the Steam Deck and look at it. "
                    "Use this when the person asks what is on screen or how to do what they see."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": [],
                },
            }
        )
    return specs


class ScreenCapture:
    """Runs the screenshot grab and keeps the JPEG for the next model turn."""

    def __init__(self, grab: Callable[[dict[str, Any]], tuple[str, bytes | None]]) -> None:
        self.grab = grab
        self._image: bytes | None = None

    def run(self, arguments: dict[str, Any]) -> str:
        text, image = self.grab(arguments)
        self._image = image
        return text

    def take(self) -> bytes | None:
        image = self._image
        self._image = None
        return image


_TOOL_TAG = re.compile(r"<(?:tool_call|function_call)>\s*(.*?)\s*</(?:tool_call|function_call)>", re.I | re.S)
_INTENT = re.compile(
    r"(look(?:ing)? (?:it |this |that |them )?up"
    r"|let me (?:search|look)"
    r"|i(?:'|’)ll (?:search|look)"
    r"|i will (?:search|look)"
    r"|search(?:ing)? the web)",
    re.I,
)


def text_tool_calls(blob: str, allowed: set[str] | None = None) -> list[dict[str, str]]:
    """Qwen and Hermes put calls in `<tool_call>{...}</tool_call>` instead of tool_calls."""
    found: list[dict[str, str]] = []
    for match in _TOOL_TAG.finditer(blob or ""):
        payload = _loads(match.group(1))
        name = str(payload.get("name") or "")
        arguments: Any = payload.get("arguments", payload.get("parameters", payload.get("args")))
        function = payload.get("function")
        if isinstance(function, dict):
            name = name or str(function.get("name") or "")
            if arguments is None:
                arguments = function.get("arguments")
        names = allowed or {"web_search", "fetch_page"}
        if name not in names:
            continue
        if isinstance(arguments, str):
            encoded = arguments or "{}"
        else:
            encoded = json.dumps(arguments or {}, ensure_ascii=False)
        found.append({"id": f"text_{len(found)}", "name": name, "arguments": encoded})
    return found


def wants_lookup(text: str) -> bool:
    for match in _INTENT.finditer(text or ""):
        window = (text or "")[max(0, match.start() - 20) : match.start()].lower()
        if re.search(r"\b(not|don't|dont|won't|wont|without)\b", window):
            continue
        return True
    return False


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for item in reversed(messages):
        if item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()[:300]
    return ""


def run_tool(client: WebClient, name: str, arguments: dict[str, Any]) -> str:
    if name == "web_search":
        results = client.search(str(arguments.get("query") or ""))
        payload: dict[str, Any] = {"results": results}
        if client.last_error and not results:
            payload["error"] = client.last_error
        return json.dumps(payload, ensure_ascii=False)[:4000]
    if name == "fetch_page":
        page = client.fetch_page(str(arguments.get("url") or ""))
        return json.dumps({"url": page.get("url"), "title": page.get("title"), "text": page.get("text")}, ensure_ascii=False)[:4000]
    return json.dumps({"error": "Unknown tool"})


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _with_hint(
    messages: list[dict[str, Any]], *, include_web: bool = True, include_screen: bool = False
) -> list[dict[str, Any]]:
    parts = []
    if include_web:
        parts.append(_HINT)
    if include_screen:
        parts.append(_SCREEN_HINT)
    hint = "\n\n".join(parts)
    copied = [dict(item) for item in messages]
    if not hint:
        return copied
    if copied and copied[0].get("role") == "system" and isinstance(copied[0].get("content"), str):
        copied[0]["content"] = copied[0]["content"] + "\n\n" + hint
        return copied
    copied.insert(0, {"role": "system", "content": hint})
    return copied


def _shot_bytes(screen: ScreenCapture | None, name: str) -> bytes | None:
    if screen is None or name != "look_at_screen":
        return None
    return screen.take()


def _jpeg_b64(image: bytes) -> str:
    return base64.b64encode(image).decode("ascii")


def iter_with_tools(
    provider: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
    cancel: threading.Event,
    client: WebClient,
    on_status: Status | None = None,
    screen: ScreenCapture | None = None,
    include_web: bool = True,
) -> Iterator[str]:
    kind = str(provider.get("kind") or "")
    if kind not in TOOL_KINDS:
        raise ToolsUnsupported(kind)
    if not str(model or "").strip() and kind == "llamacpp":
        model = _llamacpp_default_model(provider)
    include_screen = screen is not None
    if not include_web and not include_screen:
        raise ToolsUnsupported(kind)
    prepared = _with_hint(messages, include_web=include_web, include_screen=include_screen)
    notify = on_status or (lambda _phase: None)

    pages = {"n": 0}

    def execute(name: str, arguments: dict[str, Any]) -> str:
        if name == "look_at_screen":
            notify("screen")
            try:
                if screen is None:
                    return json.dumps({"error": "Screen capture is turned off in settings."})
                return screen.run(arguments)
            finally:
                notify("thinking")
        if name == "fetch_page":
            pages["n"] += 1
            notify(f"reading:{pages['n']}")
        else:
            notify("searching")
        try:
            return run_tool(client, name, arguments)
        finally:
            notify("thinking")

    if kind == "anthropic":
        yield from _anthropic_tools(provider, prepared, model, cancel, execute, screen, include_web)
        return
    if kind == "gemini":
        yield from _gemini_tools(provider, prepared, model, cancel, execute, screen, include_web)
        return
    if kind == "ollama":
        yield from _ollama_tools(provider, prepared, model, cancel, execute, screen, include_web)
        return
    yield from _openai_tools(provider, prepared, model, cancel, execute, notify, screen, include_web)


def _openai_shot(image: bytes) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "This is the screenshot you just took."},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _jpeg_b64(image)}},
        ],
    }


def _openai_tools(
    provider: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
    cancel: threading.Event,
    execute: Executor,
    notify: Status | None = None,
    screen: ScreenCapture | None = None,
    include_web: bool = True,
) -> Iterator[str]:
    if provider.get("kind") in {"openai", "hermes", "xai"}:
        require_credentials(provider)
    token_field = "max_completion_tokens" if provider.get("kind") == "openai" else "max_tokens"
    specs = tool_spec(include_web=include_web, include_screen=screen is not None)
    allowed = {spec["name"] for spec in specs}
    tools = [{"type": "function", "function": spec} for spec in specs]
    working = list(messages)
    url = join_url(_openai_root(provider), "chat/completions")
    timeout = 300 if provider.get("kind") == "xai" else 120
    looked_up = False
    for _round in range(_MAX_ROUNDS):
        if cancel.is_set():
            return
        body = {
            "model": model,
            "messages": retain_latest_image(working, multiple=keeps_multiple_images(str(provider.get("kind") or ""))),
            "stream": True,
            "tools": tools,
            token_field: _max_tokens(provider),
        }
        text, calls, reasoning = yield from _stream_openai(
            provider, url, body, timeout, cancel, emit=False, notify=notify
        )
        if not calls:
            calls = text_tool_calls(text, allowed) + text_tool_calls(reasoning, allowed)
        if include_web and not calls and not looked_up and wants_lookup(f"{text}\n{reasoning}"):
            looked_up = True
            query = _last_user_text(working)
            result = execute("web_search", {"query": query})
            working.append(
                {
                    "role": "user",
                    "content": "Web lookup results. Answer from these excerpts and cite the URLs:\n" + result,
                }
            )
            continue
        if not calls:
            visible = _without_tool_tags(text)
            if visible:
                yield visible
            return
        working.append(
            {
                "role": "assistant",
                "content": text or None,
                "tool_calls": [
                    {
                        "id": call["id"] or f"call_{index}",
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call["arguments"] or "{}"},
                    }
                    for index, call in enumerate(calls)
                ],
            }
        )
        for call in calls:
            result = execute(call["name"], _loads(call["arguments"]))
            working.append({"role": "tool", "tool_call_id": call["id"] or call["name"], "content": result})
            image = _shot_bytes(screen, call["name"])
            if image:
                working.append(_openai_shot(image))
    if cancel.is_set():
        return
    body = {
        "model": model,
        "messages": retain_latest_image(working, multiple=keeps_multiple_images(str(provider.get("kind") or ""))),
        "stream": True,
        token_field: _max_tokens(provider),
    }
    yield from _stream_openai(provider, url, body, timeout, cancel, notify=notify)


def _without_tool_tags(text: str) -> str:
    cleaned = _TOOL_TAG.sub("", text or "")
    return cleaned.strip()


def _absorb_tool(calls: dict[int, dict[str, str]], tool: dict[str, Any]) -> None:
    index = int(tool.get("index") or 0)
    slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
    if tool.get("id"):
        slot["id"] = str(tool["id"])
    function = tool.get("function") or {}
    if not isinstance(function, dict):
        return
    if function.get("name"):
        slot["name"] += str(function["name"])
    arguments = function.get("arguments")
    if isinstance(arguments, dict):
        slot["arguments"] = json.dumps(arguments, ensure_ascii=False)
    elif arguments:
        slot["arguments"] += str(arguments)


def _stream_openai(
    provider: dict[str, Any],
    url: str,
    body: dict[str, Any],
    timeout: float,
    cancel: threading.Event,
    emit: bool = True,
    notify: Status | None = None,
) -> Iterator[str]:
    calls: dict[int, dict[str, str]] = {}
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    try:
        lines = iter_lines(
            "POST",
            url,
            headers=_auth_headers(provider),
            body=json.dumps(body).encode("utf-8"),
            timeout=timeout,
            cancel=cancel,
        )
        for event in iter_sse_json(lines):
            if not isinstance(event, dict):
                continue
            error = event.get("error")
            if error:
                message = error.get("message") if isinstance(error, dict) else error
                if "tool" in str(message).lower() or "function" in str(message).lower():
                    raise ToolsUnsupported(str(message))
                raise HttpError(400, str(message)[:400])
            choices = event.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            message = choice.get("message") or {}
            if not isinstance(delta, dict):
                delta = {}
            if not isinstance(message, dict):
                message = {}
            for source in (delta, message):
                content = source.get("content")
                if isinstance(content, str) and content:
                    text_parts.append(content)
                    if emit:
                        if notify:
                            notify("writing")
                        yield content
                reasoning = source.get("reasoning_content") or source.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    reasoning_parts.append(reasoning)
                    if notify:
                        notify("thinking")
                for tool in source.get("tool_calls") or []:
                    if isinstance(tool, dict):
                        _absorb_tool(calls, tool)
    except HttpError as exc:
        if exc.status == 400 and not text_parts:
            raise ToolsUnsupported(str(exc)) from exc
        raise
    return "".join(text_parts), [calls[index] for index in sorted(calls)], "".join(reasoning_parts)


def _anthropic_tools(
    provider: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
    cancel: threading.Event,
    execute: Executor,
    screen: ScreenCapture | None = None,
    include_web: bool = True,
) -> Iterator[str]:
    token = require_credentials(provider)
    system = ""
    conv: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "system":
            system = str(message.get("content") or "")
        else:
            role = "assistant" if message.get("role") == "assistant" else "user"
            conv.append({"role": role, "content": str(message.get("content") or "")})
    tools = [
        {"name": spec["name"], "description": spec["description"], "input_schema": spec["parameters"]}
        for spec in tool_spec(include_web=include_web, include_screen=screen is not None)
    ]
    headers = {
        "Content-Type": "application/json",
        "x-api-key": token,
        "anthropic-version": ANTHROPIC_VERSION,
        "Accept": "text/event-stream",
    }
    url = join_url(_base(provider), "v1/messages")
    for _round in range(_MAX_ROUNDS):
        if cancel.is_set():
            return
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": _max_tokens(provider),
            "messages": conv,
            "tools": tools,
            "stream": True,
        }
        if system:
            body["system"] = system
        text, calls = yield from _stream_anthropic(url, headers, body, cancel)
        if not calls:
            return
        conv.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": call["id"], "name": call["name"], "input": _loads(call["arguments"])}
                    for call in calls
                ],
            }
        )
        results = []
        shots: list[bytes] = []
        for call in calls:
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": execute(call["name"], _loads(call["arguments"])),
                }
            )
            image = _shot_bytes(screen, call["name"])
            if image:
                shots.append(image)
        conv.append({"role": "user", "content": results})
        for image in shots:
            conv.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/jpeg", "data": _jpeg_b64(image)},
                        },
                        {"type": "text", "text": "This is the screenshot you just took."},
                    ],
                }
            )


def _stream_anthropic(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    cancel: threading.Event,
) -> Iterator[str]:
    blocks: dict[int, dict[str, str]] = {}
    text_parts: list[str] = []
    try:
        lines = iter_lines("POST", url, headers=headers, body=json.dumps(body).encode("utf-8"), cancel=cancel)
        for event in iter_sse_json(lines):
            if not isinstance(event, dict):
                continue
            if event.get("type") == "error":
                detail = event.get("error")
                message = detail.get("message") if isinstance(detail, dict) else detail
                if "tool" in str(message).lower():
                    raise ToolsUnsupported(str(message))
                raise HttpError(400, str(message)[:400])
            if event.get("type") == "content_block_start":
                block = event.get("content_block") or {}
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    blocks[int(event.get("index") or 0)] = {
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "arguments": "",
                    }
            if event.get("type") != "content_block_delta":
                continue
            delta = event.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                text_parts.append(delta["text"])
                yield delta["text"]
            if delta.get("type") == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                slot = blocks.setdefault(int(event.get("index") or 0), {"id": "", "name": "", "arguments": ""})
                slot["arguments"] += delta["partial_json"]
    except HttpError as exc:
        if exc.status == 400 and not text_parts:
            raise ToolsUnsupported(str(exc)) from exc
        raise
    return "".join(text_parts), [blocks[index] for index in sorted(blocks)]


def _gemini_tools(
    provider: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
    cancel: threading.Event,
    execute: Executor,
    screen: ScreenCapture | None = None,
    include_web: bool = True,
) -> Iterator[str]:
    system = ""
    contents: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "system":
            system = str(message.get("content") or "")
            continue
        role = "model" if message.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": str(message.get("content") or "")}]})
    declarations = [
        {"name": spec["name"], "description": spec["description"], "parameters": spec["parameters"]}
        for spec in tool_spec(include_web=include_web, include_screen=screen is not None)
    ]
    safe_model = urllib.parse.quote(model, safe="")
    url = join_url(_base(provider), f"models/{safe_model}:streamGenerateContent?alt=sse")
    for _round in range(_MAX_ROUNDS):
        if cancel.is_set():
            return
        body: dict[str, Any] = {
            "contents": contents,
            "tools": [{"functionDeclarations": declarations}],
            "generationConfig": {"maxOutputTokens": _max_tokens(provider)},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        text, calls = yield from _stream_gemini(provider, url, body, cancel)
        if not calls:
            return
        contents.append(
            {
                "role": "model",
                "parts": [{"functionCall": {"name": call["name"], "args": _loads(call["arguments"])}} for call in calls],
            }
        )
        parts = []
        shots: list[bytes] = []
        for call in calls:
            parts.append(
                {
                    "functionResponse": {
                        "name": call["name"],
                        "response": {"result": execute(call["name"], _loads(call["arguments"]))},
                    }
                }
            )
            image = _shot_bytes(screen, call["name"])
            if image:
                shots.append(image)
        contents.append({"role": "user", "parts": parts})
        for image in shots:
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {"inlineData": {"mimeType": "image/jpeg", "data": _jpeg_b64(image)}},
                        {"text": "This is the screenshot you just took."},
                    ],
                }
            )


def _stream_gemini(
    provider: dict[str, Any],
    url: str,
    body: dict[str, Any],
    cancel: threading.Event,
) -> Iterator[str]:
    calls: list[dict[str, str]] = []
    text_parts: list[str] = []
    try:
        lines = iter_lines(
            "POST",
            url,
            headers=_gemini_headers(provider),
            body=json.dumps(body).encode("utf-8"),
            cancel=cancel,
        )
        for event in iter_sse_json(lines):
            if not isinstance(event, dict):
                continue
            error = event.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or error)
                if "tool" in message.lower() or "function" in message.lower():
                    raise ToolsUnsupported(message)
                raise HttpError(400, message[:400])
            candidates = event.get("candidates") or []
            if not candidates or not isinstance(candidates[0], dict):
                continue
            for part in (candidates[0].get("content") or {}).get("parts") or []:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str) and part["text"]:
                    text_parts.append(part["text"])
                    yield part["text"]
                call = part.get("functionCall")
                if isinstance(call, dict) and call.get("name"):
                    calls.append({"id": str(call.get("name")), "name": str(call["name"]), "arguments": json.dumps(call.get("args") or {})})
    except HttpError as exc:
        if exc.status == 400 and not text_parts:
            raise ToolsUnsupported(str(exc)) from exc
        raise
    return "".join(text_parts), calls


def _ollama_tools(
    provider: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
    cancel: threading.Event,
    execute: Executor,
    screen: ScreenCapture | None = None,
    include_web: bool = True,
) -> Iterator[str]:
    tools = [
        {"type": "function", "function": spec}
        for spec in tool_spec(include_web=include_web, include_screen=screen is not None)
    ]
    working = list(messages)
    url = join_url(_base(provider), "api/chat")
    for _round in range(_MAX_ROUNDS):
        if cancel.is_set():
            return
        body = {
            "model": model,
            "messages": working,
            "stream": True,
            "tools": tools,
            "options": {"num_predict": _max_tokens(provider)},
        }
        text, calls = yield from _stream_ollama(provider, url, body, cancel)
        if not calls:
            return
        working.append({"role": "assistant", "content": text, "tool_calls": calls})
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            result = execute(name, _loads(function.get("arguments")))
            working.append({"role": "tool", "content": result})
            image = _shot_bytes(screen, name)
            if image:
                working.append(
                    {
                        "role": "user",
                        "content": "This is the screenshot you just took.",
                        "images": [_jpeg_b64(image)],
                    }
                )


def _stream_ollama(
    provider: dict[str, Any],
    url: str,
    body: dict[str, Any],
    cancel: threading.Event,
) -> Iterator[str]:
    calls: list[dict[str, Any]] = []
    text_parts: list[str] = []
    lines = iter_lines(
        "POST",
        url,
        headers=_auth_headers(provider),
        body=json.dumps(body).encode("utf-8"),
        cancel=cancel,
    )
    for event in iter_json_lines(lines):
        if not isinstance(event, dict):
            continue
        if event.get("error"):
            message = str(event["error"])
            if "tool" in message.lower():
                raise ToolsUnsupported(message)
            raise HttpError(400, message[:400])
        message = event.get("message") or {}
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
                yield content
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict):
                    calls.append(call)
        if event.get("done"):
            break
    return "".join(text_parts), calls
