"""Chat backends. Each kind implements listing models and streaming text deltas."""

from __future__ import annotations

import errno
import ipaddress
import json
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from typing import Any

from . import claude_code, vision
from .http_util import HttpError, iter_lines, join_url, request_json
from .redact import redact
from .sse import iter_json_lines, iter_sse_json

ANTHROPIC_VERSION = "2023-06-01"
_CONTEXT_MESSAGES = 40


def bearer_token(provider: dict[str, Any]) -> str:
    return str(provider.get("oauth_access_token") or provider.get("api_key") or "")


def require_credentials(provider: dict[str, Any]) -> str:
    token = bearer_token(provider)
    kind = provider.get("kind")
    if kind in {"openai", "anthropic", "gemini", "hermes", "xai"} and not token:
        raise ValueError("Add an API key, or finish OAuth sign-in, before chatting")
    return token


def prepare_messages(provider: dict[str, Any], history: list[dict[str, str]], system_prompt: str) -> list[dict[str, str]]:
    messages = [{"role": item["role"], "content": item["content"]} for item in history[-_CONTEXT_MESSAGES:]]
    prompt = (system_prompt or "").strip()
    if prompt:
        messages.insert(0, {"role": "system", "content": prompt})
    if provider.get("kind") == "gemini":
        return messages
    return messages


class ModelReport:
    def __init__(self, ids: list[str], vision: list[str], auto: dict[str, str] | None = None) -> None:
        self.ids = ids
        self.vision = vision
        self.auto = auto or {item: ("yes" if item in vision else "unknown") for item in ids}


def describe_models(provider: dict[str, Any]) -> ModelReport:
    kind = str(provider.get("kind") or "")
    if kind in {"openai", "hermes", "xai", "llamacpp", "custom"}:
        report = _describe_openai_models(provider)
    elif kind == "ollama":
        report = _describe_ollama_models(provider)
    elif kind == "anthropic":
        ids = _list_anthropic_models(provider)
        seen = [item for item in ids if vision.model_sees_images(item)]
        report = ModelReport(ids, seen, _named_auto(ids, seen))
    elif kind == "gemini":
        ids = _list_gemini_models(provider)
        seen = [item for item in ids if vision.model_sees_images(item)]
        report = ModelReport(ids, seen, _named_auto(ids, seen))
    elif kind == "claude_code":
        ids = claude_code.list_models(provider)
        report = ModelReport(ids, [], _named_auto(ids, []))
    else:
        raise ValueError(f"Unknown provider type: {kind}")
    if kind == "hermes":
        hermes = [item for item in report.ids if "hermes" in item.lower()]
        if hermes:
            report.ids = hermes
    if kind == "openai":
        chat = [item for item in report.ids if _looks_like_chat_model(item)]
        if chat:
            report.ids = chat
    allowed = set(report.ids)
    report.vision = [item for item in report.vision if item in allowed]
    return report


def list_models(provider: dict[str, Any]) -> list[str]:
    return describe_models(provider).ids


def _named_auto(ids: list[str], seen: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    vision_ids = set(seen)
    for item in ids:
        if item in vision_ids:
            found[item] = "yes"
        elif vision.vision_status({"id": item}, None) is False:
            found[item] = "no"
        else:
            found[item] = "unknown"
    return found


def probe_sees_images(provider: dict[str, Any], model: str) -> bool | None:
    """Send a 1x1 image once. True if the server accepts it, False if it refuses, None if unsure."""
    from .imageutil import encode_jpeg

    jpeg = encode_jpeg(b"\xff\x00\x00", 1, 1)
    cancel = threading.Event()
    timer = threading.Timer(12, cancel.set)
    timer.daemon = True
    timer.start()
    try:
        saw = False
        for _delta in iter_text(
            provider,
            [{"role": "user", "content": "Reply with one word."}],
            model,
            cancel,
            image=jpeg,
        ):
            saw = True
            cancel.set()
            break
        if cancel.is_set() and not saw:
            return None
        return True
    except (HttpError, ValueError, OSError) as exc:
        text = str(exc).lower()
        if any(word in text for word in ("image", "vision", "multimodal", "unsupported", "modalit")):
            return False
        status = int(getattr(exc, "status", 0) or 0)
        if status in {400, 404, 415, 422}:
            return False
        return None
    finally:
        timer.cancel()


def iter_text(
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    model: str,
    cancel: threading.Event,
    meta: dict[str, Any] | None = None,
    image: bytes | None = None,
    on_status: Callable[[str], None] | None = None,
) -> Iterator[str]:
    kind = str(provider.get("kind") or "")
    chosen = model.strip()
    if not chosen and kind == "llamacpp":
        chosen = _llamacpp_default_model(provider)
    if not chosen:
        raise ValueError("Choose a model")
    model = chosen
    if image and kind == "claude_code":
        raise ValueError(
            "Claude Code cannot view screenshots. "
            "Switch to an Anthropic, OpenAI, Gemini, Grok, Ollama, or llama.cpp model that can see images."
        )
    dispatch = {
        "openai": _iter_openai,
        "hermes": _iter_openai,
        "llamacpp": _iter_openai,
        "custom": _iter_openai,
        "anthropic": _iter_anthropic,
        "gemini": _iter_gemini,
        "ollama": _iter_ollama,
        "xai": _iter_openai,
        "claude_code": _iter_claude,
    }
    handler = dispatch.get(kind)
    if handler is None:
        raise ValueError(f"Unknown provider type: {kind}")
    if kind == "claude_code":
        yield from _iter_claude(provider, messages, model.strip(), cancel, meta)
        return
    if handler is _iter_openai:
        yield from _iter_openai(provider, messages, model.strip(), cancel, image, on_status)
        return
    yield from handler(provider, messages, model.strip(), cancel, image)


def _auth_headers(provider: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = bearer_token(provider)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    host = urllib.parse.urlsplit(str(provider.get("base_url") or "")).hostname or ""
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        headers["HTTP-Referer"] = "https://github.com/SMwaterSHLDhelp/Deckling"
        headers["X-Title"] = "Deckling"
    return headers


def _base(provider: dict[str, Any]) -> str:
    base = str(provider.get("base_url") or "").strip()
    if not base:
        raise ValueError("This provider needs a base URL")
    return base.rstrip("/")


def _openai_root(provider: dict[str, Any]) -> str:
    """Base URL for OpenAI-compatible routes.

    llama.cpp accepts ``http://host:8080`` and ``http://host:8080/v1``. Older
    builds only mount the ``/v1`` routes, so a missing suffix is added once.
    """
    base = _base(provider)
    if provider.get("kind") != "llamacpp":
        return base
    path = urllib.parse.urlsplit(base).path.rstrip("/")
    if path.endswith("/v1"):
        return base
    return join_url(base, "v1")


def _provider_host(provider: dict[str, Any] | None) -> str:
    if not provider:
        return ""
    return urllib.parse.urlsplit(str(provider.get("base_url") or "")).hostname or ""


def _private_host(host: str) -> bool:
    lowered = host.lower().strip("[]").rstrip(".")
    if not lowered:
        return False
    if lowered == "localhost" or lowered.endswith(".local") or lowered.endswith(".localdomain"):
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return bool(address.is_private or address.is_loopback or address.is_link_local)


def _connection_refused(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError):
            return True
        if getattr(current, "errno", None) in {errno.ECONNREFUSED, 111, 10061}:
            return True
        current = current.__cause__ or current.__context__
    return False


def _short_error(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    if len(text) > 220:
        text = text[:220].rstrip() + "..."
    return f"{type(exc).__name__}: {text}"


def _explain_llamacpp(exc: Exception, provider: dict[str, Any] | None = None) -> Exception:
    if isinstance(exc, HttpError):
        if exc.status == 503:
            return HttpError(
                503,
                "llama-server is still loading the model (HTTP 503). Wait until it is ready, then try again.",
            )
        if exc.status == 401:
            return HttpError(
                401,
                "llama-server rejected the API key. Use the same value as --api-key, or leave the key blank if the server has none.",
            )
        return exc
    host = _provider_host(provider)
    # The firewall sentence hid TLS failures: SSLCertVerificationError is an OSError.
    if isinstance(exc, OSError) and (_private_host(host) or _connection_refused(exc)):
        return OSError(
            "Can't reach llama-server. Check the host and port, and that the PC firewall allows this Deck on the LAN."
        )
    if isinstance(exc, OSError):
        return OSError(_short_error(exc))
    return exc


def _llamacpp_default_model(provider: dict[str, Any]) -> str:
    models = _list_openai_models(provider)
    if len(models) == 1:
        return models[0]
    if len(models) > 1:
        shown = ", ".join(models[:8])
        extra = f" (+{len(models) - 8} more)" if len(models) > 8 else ""
        raise ValueError(f"llama-server has more than one model. Choose one: {shown}{extra}")
    raise ValueError("llama-server did not report a loaded model. Start it with -m, or type the model id.")


def _max_tokens(provider: dict[str, Any]) -> int:
    try:
        value = int(provider.get("max_tokens") or 1024)
    except (TypeError, ValueError):
        value = 1024
    return max(1, min(value, 8192))


def _looks_like_chat_model(model_id: str) -> bool:
    lower = model_id.lower()
    blocked = ("embedding", "whisper", "tts", "dall-e", "davinci", "babbage", "moderation", "transcribe", "audio")
    if any(word in lower for word in blocked):
        return False
    return lower.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4", "ft:"))


def _row_name(row: dict[str, Any]) -> str:
    name = row.get("id") or row.get("name") or row.get("model")
    return name.strip() if isinstance(name, str) else ""


def _as_rows(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return rows
    for item in value:
        if isinstance(item, str) and item.strip():
            rows.append({"id": item.strip()})
        elif isinstance(item, dict) and _row_name(item):
            rows.append(item)
    return rows


def _auto_map(rows: dict[str, dict[str, Any]], props_vision: bool | None) -> dict[str, str]:
    found: dict[str, str] = {}
    for name, row in rows.items():
        status = vision.vision_status(row, props_vision)
        found[name] = "yes" if status is True else "no" if status is False else "unknown"
    return found


def _merge_row(current: dict[str, Any], row: dict[str, Any]) -> None:
    caps = [str(item) for item in current.get("capabilities") or [] if isinstance(current.get("capabilities"), list)]
    extra = row.get("capabilities")
    if isinstance(extra, list):
        caps.extend(str(item) for item in extra)
    for key, value in row.items():
        if value not in (None, "", []):
            current[key] = value
    if caps:
        current["capabilities"] = caps


def _models_from_openai_payload(
    payload: Any, props_vision: bool | None = None
) -> tuple[list[str], list[str], dict[str, str]]:
    if not isinstance(payload, dict):
        return [], [], {}
    data_rows = _as_rows(payload.get("data"))
    model_rows = _as_rows(payload.get("models"))
    chosen = data_rows or model_rows
    by_name: dict[str, dict[str, Any]] = {}
    for row in chosen:
        name = _row_name(row)
        by_name.setdefault(name, {"id": name})
        _merge_row(by_name[name], row)
    if data_rows:
        for row in model_rows:
            name = _row_name(row)
            if name in by_name:
                _merge_row(by_name[name], row)
    ids = sorted(by_name)
    auto = _auto_map(by_name, props_vision)
    seen = sorted(name for name, status in auto.items() if status == "yes")
    return ids, seen, auto


def _ids_from_openai_payload(payload: Any) -> list[str]:
    ids, _vision, _auto = _models_from_openai_payload(payload, None)
    return ids


def _llamacpp_props_vision(provider: dict[str, Any]) -> bool | None:
    """llama.cpp serves ``/props`` with ``modalities.vision`` next to the server root."""
    if provider.get("kind") != "llamacpp":
        return None
    base = _base(provider)
    parts = urllib.parse.urlsplit(base)
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    root = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    try:
        payload = request_json("GET", join_url(root or base, "props"), headers=_auth_headers(provider), timeout=8)
    except (HttpError, OSError, ValueError):
        return None
    modalities = payload.get("modalities") if isinstance(payload, dict) else None
    if isinstance(modalities, dict) and "vision" in modalities:
        return bool(modalities.get("vision"))
    return None


def _openai_rows(provider: dict[str, Any]) -> tuple[list[str], list[str], dict[str, str]]:
    payload = request_json(
        "GET",
        join_url(_openai_root(provider), "models"),
        headers=_auth_headers(provider),
        timeout=20,
    )
    props = _llamacpp_props_vision(provider)
    ids, seen, auto = _models_from_openai_payload(payload, props)
    return ids, seen, auto


def _describe_openai_models(provider: dict[str, Any]) -> ModelReport:
    if provider.get("kind") in {"openai", "hermes", "xai"}:
        require_credentials(provider)
    try:
        ids, seen, auto = _openai_rows(provider)
    except (HttpError, OSError) as exc:
        if provider.get("kind") == "llamacpp":
            raise _explain_llamacpp(exc, provider) from exc
        raise
    return ModelReport(ids, seen, auto)


def _ollama_show_status(provider: dict[str, Any], name: str) -> bool | None:
    """Ollama /api/show capabilities. None when the server does not say."""
    try:
        payload = request_json(
            "POST",
            join_url(_base(provider), "api/show"),
            headers=_auth_headers(provider),
            payload={"model": name},
            timeout=8,
        )
    except (HttpError, OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if vision.capabilities_see_images(payload.get("capabilities")):
        return True
    capabilities = payload.get("capabilities")
    if isinstance(capabilities, list) and capabilities:
        return False
    return None


def _describe_ollama_models(provider: dict[str, Any]) -> ModelReport:
    headers = _auth_headers(provider)
    payload = request_json("GET", join_url(_base(provider), "api/tags"), headers=headers, timeout=15)
    rows = payload.get("models") if isinstance(payload, dict) else None
    by_name: dict[str, dict[str, Any]] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, str) and row.strip():
                by_name[row.strip()] = {"name": row.strip()}
            elif isinstance(row, dict):
                name = row.get("name") or row.get("model")
                if isinstance(name, str) and name:
                    by_name[name] = row
    auto = _auto_map(by_name, None)
    checked = 0
    for name, status in list(auto.items()):
        if status != "unknown" or checked >= 6:
            continue
        checked += 1
        shown = _ollama_show_status(provider, name)
        if shown is True:
            auto[name] = "yes"
        elif shown is False:
            auto[name] = "no"
    ids = sorted(by_name)
    seen = sorted(name for name, status in auto.items() if status == "yes")
    return ModelReport(ids, seen, auto)


def _xai_http_message(exc: HttpError) -> str:
    if exc.status == 401:
        return "xAI rejected the credentials. Check the API key, or sign in again with the device code."
    if exc.status == 403:
        return (
            "xAI refused this request (HTTP 403). Subscription sign-in can be limited by plan "
            "even after the browser step succeeds. An API key from the xAI console still works."
        )
    if exc.status == 429:
        return "xAI rate limit. Wait and try again, or check the account's usage."
    return str(exc)


def _list_openai_models(provider: dict[str, Any]) -> list[str]:
    if provider.get("kind") in {"openai", "hermes", "xai"}:
        require_credentials(provider)
    return _describe_openai_models(provider).ids


def _list_anthropic_models(provider: dict[str, Any]) -> list[str]:
    token = require_credentials(provider)
    headers = {
        "x-api-key": token,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    payload = request_json("GET", join_url(_base(provider), "v1/models"), headers=headers, timeout=20)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    found = [row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]
    return sorted(set(found))


def _list_gemini_models(provider: dict[str, Any]) -> list[str]:
    headers = _gemini_headers(provider)
    payload = request_json("GET", join_url(_base(provider), "models"), headers=headers, timeout=20)
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    found: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        methods = row.get("supportedGenerationMethods") or []
        if methods and "generateContent" not in methods:
            continue
        name = str(row.get("name") or "")
        if name.startswith("models/"):
            name = name[len("models/") :]
        if name:
            found.append(name)
    return sorted(set(found))


def _list_ollama_models(provider: dict[str, Any]) -> list[str]:
    headers = _auth_headers(provider)
    payload = request_json("GET", join_url(_base(provider), "api/tags"), headers=headers, timeout=15)
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    found: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name") or row.get("model")
        if isinstance(name, str) and name:
            found.append(name)
    return sorted(set(found))


def _gemini_headers(provider: dict[str, Any]) -> dict[str, str]:
    token = require_credentials(provider)
    headers = {"Content-Type": "application/json"}
    # API keys are not bearer tokens. OAuth access tokens are.
    if token.startswith("ya29.") or provider.get("oauth_access_token"):
        headers["Authorization"] = f"Bearer {token}"
    else:
        headers["x-goog-api-key"] = token
    return headers


def _split_system(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    system: list[str] = []
    rest: list[dict[str, str]] = []
    for message in messages:
        if message["role"] == "system":
            system.append(message["content"])
        else:
            role = "assistant" if message["role"] == "assistant" else "user"
            rest.append({"role": role, "content": message["content"]})
    return "\n\n".join(part for part in system if part.strip()), rest


def _merge_roles(messages: list[dict[str, str]], assistant_role: str) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    for message in messages:
        role = assistant_role if message["role"] == "assistant" else "user"
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n\n" + message["content"]
        else:
            merged.append({"role": role, "content": message["content"]})
    if merged and merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "(conversation continues)"})
    return merged


def _openai_delta(payload: Any) -> str:
    thought, text = _openai_parts(payload)
    return text or thought


def _openai_parts(payload: Any) -> tuple[str, str]:
    """Split a chunk into reasoning and the visible reply. Null content is ignored."""
    if not isinstance(payload, dict):
        return "", ""
    error = payload.get("error")
    if isinstance(error, dict):
        raise HttpError(400, redact(str(error.get("message") or error))[:400])
    if isinstance(error, str):
        raise HttpError(400, redact(error)[:400])
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return "", ""
    thought = ""
    text = ""
    delta = choices[0].get("delta") or {}
    message = choices[0].get("message") or {}
    for source in (delta, message):
        if not isinstance(source, dict):
            continue
        content = source.get("content")
        if isinstance(content, str) and content:
            text += content
        reasoning = source.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            thought += reasoning
    return thought, text


def _iter_claude(
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    model: str,
    cancel: threading.Event,
    meta: dict[str, Any] | None,
) -> Iterator[str]:
    yield from claude_code.stream_text(provider, messages, model, cancel, meta)


def _iter_openai(
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    model: str,
    cancel: threading.Event,
    image: bytes | None = None,
    on_status: Callable[[str], None] | None = None,
) -> Iterator[str]:
    if provider.get("kind") in {"openai", "hermes", "xai"}:
        require_credentials(provider)
    token_field = "max_completion_tokens" if provider.get("kind") == "openai" else "max_tokens"
    outbound = vision.retain_latest_image(messages, multiple=vision.keeps_multiple_images(str(provider.get("kind") or "")))
    body = {
        "model": model,
        "messages": vision.openai_messages(outbound, image),
        "stream": True,
        token_field: _max_tokens(provider),
    }
    encoded = json.dumps(body).encode("utf-8")
    timeout = 300 if provider.get("kind") == "xai" else 120
    try:
        lines = iter_lines(
            "POST",
            join_url(_openai_root(provider), "chat/completions"),
            headers=_auth_headers(provider),
            body=encoded,
            timeout=timeout,
            cancel=cancel,
        )
        reasoning: list[str] = []
        wrote = False
        for event in iter_sse_json(lines):
            thought, text = _openai_parts(event)
            if thought:
                reasoning.append(thought)
                if on_status:
                    on_status("thinking")
            if text:
                wrote = True
                if on_status:
                    on_status("writing")
                yield text
        if not wrote and reasoning:
            yield "".join(reasoning)
    except HttpError as exc:
        if provider.get("kind") == "xai":
            raise HttpError(exc.status, _xai_http_message(exc)) from exc
        if provider.get("kind") == "llamacpp":
            raise _explain_llamacpp(exc, provider) from exc
        raise
    except OSError as exc:
        if provider.get("kind") == "llamacpp":
            raise _explain_llamacpp(exc, provider) from exc
        raise


def _iter_anthropic(
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    model: str,
    cancel: threading.Event,
    image: bytes | None = None,
) -> Iterator[str]:
    token = require_credentials(provider)
    system, rest = _split_system(messages)
    conv = vision.anthropic_messages(_merge_roles(rest, "assistant"), image)
    if not conv:
        raise ValueError("Nothing to send")
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": _max_tokens(provider),
        "messages": conv,
        "stream": True,
    }
    if system:
        body["system"] = system
    headers = {
        "Content-Type": "application/json",
        "x-api-key": token,
        "anthropic-version": ANTHROPIC_VERSION,
        "Accept": "text/event-stream",
    }
    lines = iter_lines(
        "POST",
        join_url(_base(provider), "v1/messages"),
        headers=headers,
        body=json.dumps(body).encode("utf-8"),
        cancel=cancel,
    )
    for event in iter_sse_json(lines):
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            detail = event.get("error")
            message = detail.get("message") if isinstance(detail, dict) else detail
            raise HttpError(400, redact(str(message or "Anthropic returned an error"))[:400])
        if event.get("type") != "content_block_delta":
            continue
        delta = event.get("delta") or {}
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            yield delta["text"]


def _iter_gemini(
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    model: str,
    cancel: threading.Event,
    image: bytes | None = None,
) -> Iterator[str]:
    system, rest = _split_system(messages)
    contents = []
    merged = _merge_roles(rest, "model")
    last_user = max((index for index, message in enumerate(merged) if message["role"] == "user"), default=-1)
    for index, message in enumerate(merged):
        role = "model" if message["role"] == "model" else "user"
        attached = image if index == last_user else None
        contents.append({"role": role, "parts": vision.gemini_parts(message["content"], attached)})
    if not contents:
        raise ValueError("Nothing to send")
    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": _max_tokens(provider)},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    safe_model = urllib.parse.quote(model, safe="")
    url = join_url(_base(provider), f"models/{safe_model}:streamGenerateContent?alt=sse")
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
            raise HttpError(400, redact(str(error.get("message") or error))[:400])
        candidates = event.get("candidates") or []
        if not candidates or not isinstance(candidates[0], dict):
            continue
        parts = ((candidates[0].get("content") or {}).get("parts") or [])
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                yield part["text"]


def _iter_ollama(
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    model: str,
    cancel: threading.Event,
    image: bytes | None = None,
) -> Iterator[str]:
    outbound = vision.retain_latest_image(messages, multiple=False)
    body = {
        "model": model,
        "messages": vision.ollama_messages(outbound, image),
        "stream": True,
        "options": {"num_predict": _max_tokens(provider)},
    }
    lines = iter_lines(
        "POST",
        join_url(_base(provider), "api/chat"),
        headers=_auth_headers(provider),
        body=json.dumps(body).encode("utf-8"),
        cancel=cancel,
    )
    for event in iter_json_lines(lines):
        if not isinstance(event, dict):
            continue
        if event.get("error"):
            raise HttpError(400, redact(str(event["error"]))[:400])
        message = event.get("message") or {}
        if isinstance(message, dict) and isinstance(message.get("content"), str) and message["content"]:
            yield message["content"]
        if event.get("done"):
            return
