"""Plugin behaviour shared by main.py. Network work runs off the Decky event loop."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from . import claude_code, oauth, providers
from .catalog import catalog_payload, get_kind
from .chats import game_bucket, normalize_chats, sort_sessions
from .claude_code import ClaudeCodeError
from .game_context import enrich_store, format_block, normalize_context, prepare_snapshot, public_game, suggestions
from .hearing import HearingEngine
from .http_util import HttpError
from .imageutil import to_jpeg
from .oauth import OAuthError
from .redact import redact
from .screen import CaptureError, capture_screen, decode_supplied_image
from .store import Store, normalize_voice, public_provider, public_session_summary
from .vision import (
    DEFAULT_QUESTION,
    EARLIER_SCREENSHOT,
    jarvis_prompt,
    model_can_see,
    model_sees_images,
    vision_ids,
)
from .voice import SPOKEN_STYLE, VoiceEngine
from .web import WebClient, normalize_web, public_web
from .web_chat import TOOL_KINDS, ScreenCapture, ToolsUnsupported, iter_with_tools

EVENT = "deckling_event"
_GAME_NAME_LIMIT = 120


class Host(Protocol):
    async def emit(self, event: str, payload: dict[str, Any]) -> None: ...

    def info(self, message: str, *args: object) -> None: ...

    def warning(self, message: str, *args: object) -> None: ...


class _NullHost:
    async def emit(self, event: str, payload: dict[str, Any]) -> None:
        return None

    def info(self, message: str, *args: object) -> None:
        logging.getLogger("deckling").info(message, *args)

    def warning(self, message: str, *args: object) -> None:
        logging.getLogger("deckling").warning(message, *args)


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "error": redact(message)}


def _public_error(exc: Exception) -> str:
    """Keep the exception class visible. Do not replace it with a generic hint."""
    text = " ".join(str(exc).split())
    name = type(exc).__name__
    if text.startswith("Can't reach") or text.startswith(name) or "Error:" in text[:80]:
        return text[:400]
    return f"{name}: {text}"[:400]


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _state_error(
    catalog: list[dict[str, str]],
    message: str,
    voice: dict[str, Any],
    hearing: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": redact(message),
        "catalog": catalog,
        "providers": [],
        "default_provider_id": "",
        "default_model": "",
        "system_prompt": "",
        "current_session_id": "",
        "sessions": [],
        "messages": [],
        "voice": voice,
        "hearing": hearing,
        "context": context or normalize_context(None),
        "game": None,
        "suggestions": [],
        "web": public_web(None),
        "chats": normalize_chats(None),
    }


def _status_text(phase: str) -> str:
    if phase == "searching":
        return "Searching the web..."
    if phase.startswith("reading"):
        count = phase.split(":", 1)[-1] if ":" in phase else "1"
        noun = "page" if count == "1" else "pages"
        return f"Reading {count} {noun}..."
    if phase == "writing":
        return "Writing..."
    if phase == "screen":
        return "Looking at your screen..."
    return "Thinking..."


def _user_question(history: list[dict[str, str]]) -> str:
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        text = str(item.get("content") or "").strip()
        if text.startswith("[Playing:") and "\n\n" in text:
            text = text.split("\n\n", 1)[1].strip()
        if text.startswith("[Looking at the screen]"):
            text = text.replace("[Looking at the screen]", "", 1).strip()
        return text
    return ""


def _inject_block(messages: list[dict[str, Any]], block: str) -> list[dict[str, Any]]:
    copied = [dict(item) for item in messages]
    if copied and copied[0].get("role") == "system":
        copied[0]["content"] = str(copied[0].get("content") or "") + "\n\n" + block
        return copied
    copied.insert(0, {"role": "system", "content": block})
    return copied


def _about_game(name: str) -> str:
    cleaned = " ".join(str(name or "").split())
    return cleaned[:_GAME_NAME_LIMIT]


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


class AssistantService:
    def __init__(self, settings_dir: str, runtime_dir: str, host: Host | None = None) -> None:
        self.store = Store(settings_dir, runtime_dir)
        self.host = host or _NullHost()
        self._streams: dict[str, threading.Event] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._oauth: dict[str, dict[str, Any]] = {}
        self._oauth_cancel: dict[str, threading.Event] = {}
        self.voice = VoiceEngine(self.store)
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self.hearing = HearingEngine(
            self.store,
            notify=self._hearing_notify,
            on_command=self._hearing_command,
            pending=self._hearing_pending,
            speaking=self.voice.is_speaking,
        )
        self._vision_cache: dict[tuple[str, str], bool] = {}
        if self.hearing.public()["wake_enabled"]:
            self.hearing.start()
        self.screen_grabbers = None
        self._last_jpeg: bytes | None = None
        self._capture_lock = threading.Lock()
        self._game: dict[str, Any] = {}

    def state(self) -> dict[str, Any]:
        # The catalog is static data. A broken settings or chat file must not hide it.
        catalog = catalog_payload()
        voice = self.voice.public()
        hearing = self.hearing.public()
        try:
            config = self.store.load_config()
        except (OSError, ValueError) as exc:
            return _state_error(catalog, f"Could not read saved settings: {exc}", voice, hearing, normalize_context(None))
        try:
            sessions, current = self.store.ensure_session()
        except (OSError, ValueError) as exc:
            return {
                **_state_error(
                    catalog,
                    f"Could not read saved chats: {exc}",
                    voice,
                    hearing,
                    normalize_context(config.get("context")),
                ),
                "providers": [public_provider(item) for item in config.get("providers") or []],
                "default_provider_id": config.get("default_provider_id") or "",
                "default_model": config.get("default_model") or "",
                "system_prompt": config.get("system_prompt") or "",
            }
        return {
            "ok": True,
            "catalog": catalog,
            "providers": [public_provider(item) for item in config["providers"]],
            "default_provider_id": config.get("default_provider_id") or "",
            "default_model": config.get("default_model") or "",
            "system_prompt": config.get("system_prompt") or "",
            "current_session_id": current["id"],
            "sessions": [public_session_summary(item) for item in sessions["sessions"]],
            "messages": list(current.get("messages") or []),
            "voice": voice,
            "hearing": hearing,
            "context": normalize_context(config.get("context")),
            "game": public_game(self._game),
            "suggestions": suggestions(self._game) if self._game.get("name") else [],
            "web": public_web(config.get("web")),
            "chats": normalize_chats(config.get("chats")),
        }

    def save_provider(self, incoming: dict[str, Any]) -> dict[str, Any]:
        record = self.store.upsert_provider(incoming)
        self.host.info("Saved provider kind=%s", record.get("kind"))
        return {"ok": True, "provider": public_provider(record), **self._state_bits()}

    def delete_provider(self, provider_id: str) -> dict[str, Any]:
        self.store.delete_provider(provider_id)
        self.host.info("Deleted provider")
        return {"ok": True, **self._state_bits()}

    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(settings, dict):
            raise ValueError("Settings must be an object")
        self.store.update_settings(
            str(settings.get("system_prompt") or ""),
            str(settings.get("default_provider_id") or ""),
            str(settings.get("default_model") or ""),
        )
        return {"ok": True, **self._state_bits()}

    def new_session(self) -> dict[str, Any]:
        self.voice.stop()
        key, label = game_bucket(self._game if self._game.get("name") else None)
        current = self.store.new_session(key, label)
        return {"ok": True, **self._open_session(current)}

    def switch_session(self, session_id: str) -> dict[str, Any]:
        current = self.store.switch_session(session_id)
        return {"ok": True, **self._open_session(current)}

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        current = self.store.rename_session(session_id, title)
        return {"ok": True, **self._open_session(self._current_or(current))}

    def pin_session(self, session_id: str, pinned: bool) -> dict[str, Any]:
        self.store.pin_session(session_id, pinned)
        _data, current = self.store.ensure_session()
        return {"ok": True, **self._open_session(current)}

    def move_session(self, session_id: str, game_key: str, game_label: str) -> dict[str, Any]:
        key = str(game_key or "general")[:120]
        if key != "general" and not key.startswith(("app:", "rom:", "name:")):
            raise ValueError("Pick a game from the list")
        self.store.move_session(session_id, key, game_label)
        _data, current = self.store.ensure_session()
        return {"ok": True, **self._open_session(current)}

    def save_chats(self, settings: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(settings, dict):
            raise ValueError("Chat settings must be an object")
        chats = self.store.update_chats(settings)
        return {"ok": True, "chats": chats, **self._session_bits()}

    def clear_session(self) -> dict[str, Any]:
        self.voice.stop()
        current = self.store.clear_session()
        return {"ok": True, "current_session_id": current["id"], "messages": [], **self._session_bits()}

    def delete_session(self, session_id: str) -> dict[str, Any]:
        self.store.delete_session(session_id)
        _, current = self.store.ensure_session()
        return {
            "ok": True,
            "current_session_id": current["id"],
            "messages": list(current.get("messages") or []),
            **self._session_bits(),
        }

    async def test_provider(self, provider_id: str) -> dict[str, Any]:
        provider = await self._provider_ready(provider_id)
        started = time.perf_counter()
        try:
            report = await asyncio.to_thread(providers.describe_models, provider)
            models = report.ids
            seen = set(report.vision)
        except (HttpError, ValueError, OSError, ClaudeCodeError) as exc:
            self.host.warning("Connection test failed kind=%s", provider.get("kind"))
            elapsed = _elapsed_ms(started)
            status = int(getattr(exc, "status", 0) or 0)
            detail = _public_error(exc)
            if status:
                message = f"HTTP {status} in {elapsed} ms. {detail}"
            else:
                message = f"No HTTP response in {elapsed} ms. {detail}"
            self.store.set_connection(provider_id, "error", message)
            return {**_fail(message), "status": status, "latency_ms": elapsed}
        elapsed = _elapsed_ms(started)
        self.host.info("Connection test ok kind=%s models=%s", provider.get("kind"), len(models))
        preview = models[:50]
        if preview:
            detail = f"Connected. {len(models)} model{'s' if len(models) != 1 else ''} available."
        else:
            detail = "Connected, but the server did not list any models. You can still type a model id."
        message = f"HTTP 200 in {elapsed} ms. {detail}"
        self.store.set_connection(provider_id, "connected", message)
        return {
            "ok": True,
            "message": message,
            "models": preview,
            "vision_models": vision_ids(preview, provider, seen),
            "status": 200,
            "latency_ms": elapsed,
        }

    async def list_models(self, provider_id: str) -> dict[str, Any]:
        provider = await self._provider_ready(provider_id)
        try:
            report = await asyncio.to_thread(providers.describe_models, provider)
            models = report.ids
            seen = set(report.vision)
        except (HttpError, ValueError, OSError, ClaudeCodeError) as exc:
            message = redact(_public_error(exc))
            self.host.warning("Model list failed kind=%s: %s", provider.get("kind"), message)
            self.store.set_connection(provider_id, "error", message)
            return _fail(message)
        shown = models[:80]
        detail = f"Connected. {len(models)} model{'s' if len(models) != 1 else ''} available."
        self.store.set_connection(provider_id, "connected", detail)
        return {"ok": True, "models": shown, "vision_models": vision_ids(shown, provider, seen)}

    def start_chat(
        self,
        provider_id: str,
        model: str,
        content: str,
        request_id: str,
        about_game: str,
    ) -> dict[str, Any]:
        if not request_id or len(request_id) > 80:
            raise ValueError("Missing request id")
        if self._streams:
            return _fail("A response is still streaming")
        self.voice.stop()
        text = str(content or "").strip()
        game = _about_game(about_game)
        if game and text:
            text = f"[Playing: {game}]\n\n{text}"
        elif game and not text:
            text = f"I'm playing {game} on my Steam Deck. Give me a short, spoiler-free tip."
        if not text:
            return _fail("Type a message first")
        _, current = self.store.ensure_session()
        self.store.append_message(current["id"], "user", text)
        self.store.remember_model(current["id"], provider_id, model)
        cancel = threading.Event()
        self._streams[request_id] = cancel
        task = asyncio.get_running_loop().create_task(
            self._run_chat(provider_id, model, request_id, current["id"], cancel)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        refreshed, current = self.store.ensure_session()
        return {
            "ok": True,
            "request_id": request_id,
            "messages": list(current.get("messages") or []),
            "sessions": [public_session_summary(item) for item in refreshed["sessions"]],
        }

    def _cancel_active_streams(self) -> None:
        """A stuck screenshot or vision call must not block the next look."""
        for request_id, cancel in list(self._streams.items()):
            cancel.set()
            self._streams.pop(request_id, None)

    def cancel_chat(self, request_id: str) -> dict[str, Any]:
        self.voice.stop()
        cancel = self._streams.get(request_id)
        if cancel is None:
            return {"ok": True, "message": "Nothing to stop"}
        cancel.set()
        return {"ok": True}

    def _with_game_context(self, prompt: str, config: dict[str, Any]) -> str:
        block = format_block(self._game, normalize_context(config.get("context")))
        if not block:
            return prompt
        return f"{prompt}\n\n{block}".strip() if prompt else block

    def save_context(self, settings: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(settings, dict):
            raise ValueError("Game context settings must be an object")
        context = self.store.update_context(settings)
        return {"ok": True, "context": context, "game": public_game(self._game), "suggestions": self._suggestions()}

    def set_game_context(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(snapshot, dict):
            raise ValueError("Game context must be an object")
        game = prepare_snapshot(snapshot)
        if game.get("name"):
            try:
                enrich_store(game, os.path.join(self.store.runtime_dir, "game-cache"))
            except Exception:
                # Store data is optional. The Steam client fields still go in the prompt.
                pass
        else:
            game = {}
        previous_key, _previous_label = game_bucket(self._game if self._game.get("name") else None)
        self._game = game
        context = normalize_context(self.store.load_config().get("context"))
        key, label = game_bucket(self._game if self._game.get("name") else None)
        payload: dict[str, Any] = {
            "ok": True,
            "context": context,
            "game": public_game(self._game),
            "suggestions": self._suggestions(),
            "focused": False,
            **self._session_bits(),
        }
        if key != previous_key and not self._streams:
            focused = self.store.focus_game(key, label)
            payload["focused"] = True
            payload.update(self._open_session(focused["session"]))
        return payload

    def _suggestions(self) -> list[str]:
        if not self._game.get("name"):
            return []
        return suggestions(self._game)

    def save_web(self, settings: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(settings, dict):
            raise ValueError("Web lookup settings must be an object")
        return {"ok": True, "web": self.store.update_web(settings)}

    async def test_web(self, query: str = "") -> dict[str, Any]:
        return await asyncio.to_thread(self._test_web, query)

    def _test_web(self, query: str) -> dict[str, Any]:
        text = " ".join(str(query or "Elden Ring Malenia weakness").split())[:300]
        config = self.store.load_config()
        web = normalize_web(config.get("web"))
        if not web["enabled"]:
            return {"ok": False, "error": "Web lookup is off.", "query": text, "results": []}
        client = WebClient(os.path.join(self.store.runtime_dir, "web-cache"), web)
        results = client.search(text)
        excerpt = ""
        if results:
            page = client.fetch_page(str(results[0].get("url") or ""))
            excerpt = " ".join(str(page.get("text") or "").split())[:500]
        if client.last_error and not results:
            return {
                "ok": False,
                "error": client.last_error,
                "query": text,
                "results": [],
                "backend": client.last_backend,
            }
        return {
            "ok": True,
            "query": text,
            "results": results,
            "excerpt": excerpt,
            "error": client.last_error,
            "backend": client.last_backend,
        }

    def save_hearing(self, settings: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(settings, dict):
            raise ValueError("Listening settings must be an object")
        return {"ok": True, "hearing": self.hearing.update(settings)}

    def push_to_talk(self) -> dict[str, Any]:
        if not self.hearing.public()["ptt_enabled"]:
            return _fail("Push to talk is turned off in settings.")
        if self.voice.is_speaking():
            self.voice.stop()
            return {"ok": True, "stopped": True, "hearing": self.hearing.public()}
        self.voice.stop()
        self.hearing.begin_ptt()
        return {"ok": True, "hearing": self.hearing.public()}

    def stop_listening(self) -> dict[str, Any]:
        self.hearing.update({"wake_enabled": False})
        self.hearing.stop()
        return {"ok": True, "hearing": self.hearing.public()}

    def set_hearing_activity(self, game_running: bool, sleeping: bool) -> dict[str, Any]:
        return {"ok": True, "hearing": self.hearing.set_activity(bool(game_running), bool(sleeping))}

    def _hearing_notify(self, payload: dict[str, Any]) -> None:
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(self._emit(payload), loop)

    def _hearing_pending(self) -> bool:
        if self._streams:
            return True
        try:
            _sessions, current = self.store.ensure_session()
        except (OSError, ValueError):
            return False
        for message in reversed(current.get("messages") or []):
            if message.get("role") != "assistant":
                continue
            text = str(message.get("content") or "")
            lowered = text.lower()
            cues = ("should i", "shall i", "go ahead", "do you want", "want me to")
            return "?" in text and any(cue in lowered for cue in cues)
        return False

    def _hearing_command(self, action: str, text: str) -> None:
        if action == "new_chat":
            self.new_session()
            self._hearing_notify({"type": "hearing", "phase": "idle", "message": "New chat"})
            return
        if action == "stop_talking":
            self.voice.stop()
            self._hearing_notify({"type": "speech", "status": "done"})
            return
        if action == "cancel":
            self.voice.stop()
            for cancel in list(self._streams.values()):
                cancel.set()
            try:
                _sessions, current = self.store.ensure_session()
                self.store.append_message(current["id"], "assistant", "Cancelled.")
            except (OSError, ValueError):
                return
            self._hearing_notify({"type": "hearing", "phase": "cancelled", "message": "Cancelled."})
            return
        if action == "screen":
            self._hearing_notify({"type": "hearing", "phase": "screen", "transcript": text, "message": text})
            return
        if action in {"confirm", "message"}:
            self._submit_voice(text)

    def _submit_voice(self, text: str) -> None:
        config = self.store.load_config()
        provider_id = str(config.get("default_provider_id") or "")
        if not provider_id:
            providers_saved = config.get("providers") or []
            if providers_saved:
                provider_id = str(providers_saved[0].get("id") or "")
        model = str(config.get("default_model") or "")
        request_id = secrets.token_hex(8)
        self._hearing_notify(
            {"type": "hearing", "phase": "sending", "transcript": text, "request_id": request_id, "message": text}
        )
        loop = self._loop
        if loop is None or not loop.is_running():
            self._hearing_notify(
                {"type": "hearing", "phase": "error", "message": "Deckling cannot send a voice message yet."}
            )
            return

        async def run() -> None:
            try:
                self.start_chat(provider_id, model, text, request_id, "")
            except Exception as exc:  # noqa: BLE001 - the chat panel shows this
                await self._emit({"type": "chat_error", "request_id": request_id, "error": redact(str(exc))})

        asyncio.run_coroutine_threadsafe(run(), loop)

    def save_voice(self, settings: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(settings, dict):
            raise ValueError("Voice settings must be an object")
        return {"ok": True, "voice": self.voice.update(settings)}

    async def test_voice(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.voice.test)

    def stop_speaking(self) -> dict[str, Any]:
        self.voice.stop()
        return {"ok": True}

    async def retry_kitten(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.voice.retry_kitten)

    def save_last_screenshot(self) -> dict[str, Any]:
        data = self._last_jpeg
        if not data:
            return _fail("Nothing to save yet.")
        folder = os.path.join(self.store.runtime_dir, "saved-screenshots")
        os.makedirs(folder, exist_ok=True)
        os.chmod(folder, 0o700)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        path = os.path.join(folder, f"screen-{stamp}.jpg")
        suffix = 1
        while os.path.exists(path):
            path = os.path.join(folder, f"screen-{stamp}-{suffix}.jpg")
            suffix += 1
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(path, 0o600)
        return {"ok": True, "path": path}

    def look_at_screen(
        self,
        provider_id: str,
        model: str,
        question: str,
        request_id: str,
        game: str,
        image_b64: str,
        qam_hidden: bool,
    ) -> dict[str, Any]:
        self.voice.stop()
        self._cancel_active_streams()
        if not request_id or len(str(request_id)) > 80:
            raise ValueError("Missing request id")
        enabled = bool(normalize_voice(self.store.load_config().get("voice"))["screen_capture"])
        hidden = _as_bool(qam_hidden)
        if not enabled:
            return _fail("Screen capture is turned off in settings.")
        if not hidden:
            return _fail("Hide the Quick Access Menu before taking the shot.")
        provider = self.store.get_provider(provider_id)
        chosen = str(model or "").strip() or str(provider.get("default_model") or "")
        if provider.get("kind") == "claude_code" or not self._model_can_see(provider, chosen):
            return self._text_only(provider, chosen)
        question_text = " ".join(str(question or "").split())[:2000] or DEFAULT_QUESTION
        game_name = _about_game(game)
        _, current = self.store.ensure_session()
        cancel = threading.Event()
        self._streams[request_id] = cancel
        task = asyncio.get_running_loop().create_task(
            self._run_screen(
                provider_id,
                chosen,
                question_text,
                request_id,
                current["id"],
                cancel,
                game_name,
                str(image_b64 or ""),
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return {"ok": True, "request_id": request_id}

    def _model_can_see(self, provider: dict[str, Any], chosen: str) -> bool:
        overrides = provider.get("vision_override") if isinstance(provider.get("vision_override"), dict) else {}
        if chosen in overrides:
            return bool(overrides[chosen])
        key = (str(provider.get("id") or provider.get("base_url") or ""), chosen)
        if key in self._vision_cache:
            return self._vision_cache[key]
        auto: set[str] = set()
        described = False
        try:
            auto = set(providers.describe_models(provider).vision)
            described = True
        except (HttpError, ValueError, OSError, ClaudeCodeError):
            auto = set()
        seen = model_can_see(provider, chosen, auto)
        if described:
            self._vision_cache[key] = seen
        return seen

    def _offer_screen_tool(self, provider: dict[str, Any], chosen: str) -> bool:
        if not bool(normalize_voice(self.store.load_config().get("voice"))["screen_capture"]):
            return False
        if provider.get("kind") not in TOOL_KINDS:
            return False
        overrides = provider.get("vision_override") if isinstance(provider.get("vision_override"), dict) else {}
        if chosen in overrides:
            return bool(overrides[chosen])
        if model_sees_images(chosen):
            return True
        if provider.get("kind") not in {"llamacpp", "custom", "ollama"}:
            return False
        return self._model_can_see(provider, chosen)

    def _grab_for_model(self, announce: Callable[[str], None]) -> tuple[str, bytes | None]:
        if not bool(normalize_voice(self.store.load_config().get("voice"))["screen_capture"]):
            return json.dumps({"error": "Screen capture is turned off in settings."}), None
        announce("Taking photo")
        try:
            raw = self._locked_capture()
            jpeg = to_jpeg(raw)
        except Exception as exc:  # noqa: BLE001 - the model hears the capture error
            return json.dumps({"error": str(exc)[:400]}), None
        self._last_jpeg = jpeg
        announce("Looking at your screen...")
        return json.dumps({"ok": True, "note": "Screenshot attached."}), jpeg

    def _with_voice(self, prompt: str) -> str:
        if not self.voice.enabled():
            return prompt
        if SPOKEN_STYLE in (prompt or ""):
            return prompt
        base = (prompt or "").rstrip()
        if not base:
            return SPOKEN_STYLE
        return base + "\n\n" + SPOKEN_STYLE

    def set_model_vision(self, provider_id: str, model: str, enabled: bool) -> dict[str, Any]:
        record = self.store.set_model_vision(provider_id, model, enabled)
        return {"ok": True, "provider": public_provider(record)}

    def _text_only(self, provider: dict[str, Any], chosen: str) -> dict[str, Any]:
        suggestions: list[str] = []
        try:
            report = providers.describe_models(provider)
            suggestions = vision_ids(report.ids, provider, set(report.vision))[:8]
        except (HttpError, ValueError, OSError, ClaudeCodeError):
            suggestions = []
        if provider.get("kind") == "claude_code":
            message = "Claude Code cannot view screenshots. Switch to a model that can see images."
        else:
            label = chosen or "This model"
            message = f"{label} only reads text. Switch to a model that can see images."
        return {"ok": False, "vision": False, "error": message, "suggestions": suggestions}

    def start_oauth(self, provider_id: str, flow: str) -> dict[str, Any]:
        provider = self.store.get_provider(provider_id)
        kind = get_kind(str(provider.get("kind")))
        if kind.kind == "claude_code":
            if str(provider.get("base_url") or "").strip():
                return _fail(
                    "Remote mode uses the Claude Code login on the PC running the bridge. "
                    "On that PC run claude login or claude setup-token."
                )
            flow = "setup-token"
        elif kind.oauth == "xai":
            if flow not in {"device"}:
                return _fail("xAI sign-in uses the device-code flow.")
        elif kind.oauth == "none":
            return _fail(f"{kind.label} does not offer third-party OAuth. Use an API key.")
        elif flow not in {"device", "pkce"}:
            return _fail("Choose device or PKCE sign-in")
        previous = self._oauth_cancel.get(provider_id)
        if previous is not None:
            previous.set()
        cancel = threading.Event()
        self._oauth_cancel[provider_id] = cancel
        self._oauth[provider_id] = {"status": "starting", "flow": flow, "message": "Contacting the provider…"}
        task = asyncio.get_running_loop().create_task(self._run_oauth(provider_id, flow, cancel))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return {"ok": True, "status": "starting", "flow": flow, "message": "Contacting the provider…"}

    def cancel_oauth(self, provider_id: str) -> dict[str, Any]:
        cancel = self._oauth_cancel.get(provider_id)
        if cancel is not None:
            cancel.set()
        current = self._oauth.get(provider_id) or {}
        current["status"] = "idle"
        current["message"] = "Login cancelled"
        self._oauth[provider_id] = current
        return {"ok": True, **self._public_oauth(provider_id)}

    def oauth_status(self, provider_id: str) -> dict[str, Any]:
        return {"ok": True, **self._public_oauth(provider_id)}

    async def shutdown(self) -> None:
        self.voice.stop()
        self.hearing.stop()
        for cancel in list(self._streams.values()):
            cancel.set()
        for cancel in list(self._oauth_cancel.values()):
            cancel.set()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _state_bits(self) -> dict[str, Any]:
        config = self.store.load_config()
        return {
            "providers": [public_provider(item) for item in config["providers"]],
            "default_provider_id": config.get("default_provider_id") or "",
            "default_model": config.get("default_model") or "",
            "system_prompt": config.get("system_prompt") or "",
        }

    def _session_bits(self) -> dict[str, Any]:
        data = self.store.load_sessions()
        key, _label = game_bucket(self._game if self._game.get("name") else None)
        ordered = sort_sessions(data["sessions"], key)
        return {"sessions": [public_session_summary(item) for item in ordered]}

    def _open_session(self, current: dict[str, Any]) -> dict[str, Any]:
        chats = normalize_chats(self.store.load_config().get("chats"))
        return {
            "current_session_id": current["id"],
            "messages": list(current.get("messages") or []),
            "provider_id": current.get("provider_id") or "",
            "model": current.get("model") or "",
            "remember_model": chats["remember_model"],
            **self._session_bits(),
        }

    def _current_or(self, fallback: dict[str, Any]) -> dict[str, Any]:
        _data, current = self.store.ensure_session()
        if current.get("id") == fallback.get("id"):
            return fallback
        return current

    def _public_oauth(self, provider_id: str) -> dict[str, Any]:
        raw = dict(self._oauth.get(provider_id) or {"status": "idle", "message": ""})
        # device_code and verifiers never leave the process.
        for secret_key in ("device_auth_id", "device_code", "code_verifier", "state", "port"):
            raw.pop(secret_key, None)
        return raw

    async def _provider_ready(self, provider_id: str) -> dict[str, Any]:
        provider = self.store.get_provider(provider_id)
        refreshed = await asyncio.to_thread(self._refresh_if_needed, provider)
        if refreshed is not None:
            provider = self.store.get_provider(provider_id)
        return provider

    def _refresh_if_needed(self, provider: dict[str, Any]) -> dict[str, Any] | None:
        try:
            fields = oauth.refresh_access_token(provider)
        except OAuthError as exc:
            self.host.warning("Token refresh failed kind=%s", provider.get("kind"))
            raise ValueError(str(exc)) from exc
        if not fields:
            return None
        self.store.set_oauth_tokens(
            str(provider["id"]),
            access_token=fields["access_token"],
            refresh_token=fields.get("refresh_token") or "",
            expires_at=int(fields["expires_at"]),
        )
        self.host.info("Refreshed OAuth token kind=%s", provider.get("kind"))
        return fields

    async def _emit(self, payload: dict[str, Any]) -> None:
        await self.host.emit(EVENT, payload)

    def _speak_reply(self, text: str) -> None:
        loop = asyncio.get_running_loop()

        def run() -> None:
            try:
                asyncio.run_coroutine_threadsafe(self._emit({"type": "speech", "status": "started"}), loop).result()
            except Exception:
                self.host.warning("Could not report that speech started")
            result = self.voice.speak_blocking(text)
            status = "error" if result.get("error") else "done"
            payload = {"type": "speech", "status": status, "error": result.get("error") or result.get("warning") or ""}
            try:
                asyncio.run_coroutine_threadsafe(self._emit(payload), loop).result()
            except Exception:
                self.host.warning("Could not report that speech finished")

        threading.Thread(target=run, daemon=True).start()

    async def _run_screen(
        self,
        provider_id: str,
        model: str,
        question: str,
        request_id: str,
        session_id: str,
        cancel: threading.Event,
        game: str,
        image_b64: str,
    ) -> None:
        await self._emit(
            {
                "type": "status",
                "request_id": request_id,
                "phase": "screen",
                "message": "Looking at your screen...",
            }
        )
        await self._emit({"type": "toast", "message": "Taking photo"})
        try:
            raw = await asyncio.to_thread(self._obtain_screen, image_b64)
            jpeg = await asyncio.to_thread(to_jpeg, raw)
            await self._emit({"type": "toast", "message": "Looking at your screen..."})
            self._last_jpeg = jpeg
            self.store.append_message(session_id, "user", f"[Looking at the screen] {question}")
        except Exception as exc:  # noqa: BLE001 - shown in the panel, never logged raw
            self._streams.pop(request_id, None)
            self.host.warning("Screen capture failed: %s", redact(str(exc)))
            await self._emit(
                {"type": "chat_error", "request_id": request_id, "session_id": session_id, "error": redact(str(exc))}
            )
            return
        await self._run_chat(
            provider_id,
            model,
            request_id,
            session_id,
            cancel,
            image=jpeg,
            system_override=self._with_game_context(jarvis_prompt(game), self.store.load_config()),
            history_override=self._history_for_screen(session_id, question),
        )

    def _history_for_screen(self, session_id: str, question: str) -> list[dict[str, str]]:
        """Earlier looks stay as text. Only the new question is sent with a picture."""
        try:
            data = self.store.load_sessions()
        except (OSError, ValueError):
            data = {"sessions": []}
        current = next((item for item in data.get("sessions") or [] if item.get("id") == session_id), None)
        history: list[dict[str, str]] = []
        for item in (current or {}).get("messages") or []:
            if item.get("role") not in {"user", "assistant"}:
                continue
            history.append({"role": str(item.get("role")), "content": str(item.get("content") or "")})
        screen_turns = [
            index
            for index, item in enumerate(history)
            if item["role"] == "user" and "[Looking at the screen]" in item["content"]
        ]
        for index in screen_turns[:-1]:
            if EARLIER_SCREENSHOT not in history[index]["content"]:
                history[index]["content"] = f"{history[index]['content']} {EARLIER_SCREENSHOT}"
        if history and history[-1]["role"] == "user":
            history[-1]["content"] = question
        else:
            history.append({"role": "user", "content": question})
        return history

    def _thinking_tick(self) -> bool:
        try:
            return bool(self.hearing.public().get("thinking_tick"))
        except (OSError, ValueError, AttributeError):
            return False

    def _tick_loop(self, stop: threading.Event) -> None:
        from .hearing import play_pcm, tick_pcm

        while not stop.wait(3.0):
            play_pcm(tick_pcm())

    def _locked_capture(self) -> bytes:
        if not self._capture_lock.acquire(timeout=12):
            raise CaptureError("A screenshot is still in progress. Try again in a moment.")
        try:
            return capture_screen(
                qam_hidden=True,
                enabled=True,
                runtime_dir=self.store.runtime_dir,
                grabbers=self.screen_grabbers,
            )
        finally:
            self._capture_lock.release()

    def _obtain_screen(self, image_b64: str) -> bytes:
        errors: list[str] = []
        if str(image_b64 or "").strip():
            try:
                return decode_supplied_image(image_b64, self.store.runtime_dir)
            except CaptureError as exc:
                errors.append(str(exc))
        try:
            return self._locked_capture()
        except CaptureError as exc:
            errors.append(str(exc))
            raise CaptureError(" ".join(errors)[:500]) from exc

    async def test_screen(self) -> dict[str, Any]:
        await self._emit({"type": "toast", "message": "Taking photo"})
        result = await asyncio.to_thread(self._test_screen)
        if result.get("ok"):
            await self._emit({"type": "toast", "message": "Looking at your screen..."})
        return result

    def _test_screen(self) -> dict[str, Any]:
        try:
            raw = self._locked_capture()
            jpeg = to_jpeg(raw)
        except Exception as exc:  # noqa: BLE001 - the settings button shows this
            return {"ok": False, "error": str(exc)[:500]}
        self._last_jpeg = jpeg
        return {"ok": True, "image_b64": base64.b64encode(jpeg).decode("ascii"), "bytes": len(jpeg)}

    async def _run_chat(
        self,
        provider_id: str,
        model: str,
        request_id: str,
        session_id: str,
        cancel: threading.Event,
        image: bytes | None = None,
        system_override: str | None = None,
        history_override: list[dict[str, str]] | None = None,
    ) -> None:
        collected: list[str] = []
        meta: dict[str, Any] = {}
        try:
            provider = await self._provider_ready(provider_id)
            chosen = model.strip() or str(provider.get("default_model") or "")
            config = self.store.load_config()
            _data, current = self.store.ensure_session()
            if current["id"] != session_id:
                current = next(item for item in _data["sessions"] if item.get("id") == session_id)
            if history_override is not None:
                history = history_override
                prompt = self._with_voice(system_override or "")
            else:
                history = [
                    {"role": item["role"], "content": item["content"]}
                    for item in (current.get("messages") or [])
                    if item.get("role") in {"user", "assistant"}
                ]
                prompt = self._with_voice(self._with_game_context(str(config.get("system_prompt") or ""), config))
            messages = providers.prepare_messages(provider, history, prompt)
            meta["session_id"] = str(current.get("claude_session_id") or "")
            self.host.info("Chat started kind=%s model=%s screen=%s", provider.get("kind"), chosen, bool(image))
            await self._emit(
                {"type": "status", "request_id": request_id, "phase": "thinking", "message": "Thinking..."}
            )
            web_client = WebClient(os.path.join(self.store.runtime_dir, "web-cache"), config.get("web"))
            game_name = str(self._game.get("name") or "")
            question = _user_question(history)
            kind_name = str(provider.get("kind") or "")

            def _produce(queue: asyncio.Queue[tuple[str, object]], loop: asyncio.AbstractEventLoop) -> None:
                def _status(phase: str) -> None:
                    asyncio.run_coroutine_threadsafe(queue.put(("status", phase)), loop).result()

                outgoing = messages
                streamed = False
                try:
                    offer_screen = (not image) and self._offer_screen_tool(provider, chosen)

                    def _announce(message: str) -> None:
                        asyncio.run_coroutine_threadsafe(
                            self._emit({"type": "toast", "message": message}), loop
                        ).result()

                    screen = ScreenCapture(lambda _args: self._grab_for_model(_announce)) if offer_screen else None
                    if (web_client.enabled or screen is not None) and not image and kind_name in TOOL_KINDS:
                        try:
                            for delta in iter_with_tools(
                                provider,
                                outgoing,
                                chosen,
                                cancel,
                                web_client,
                                _status,
                                screen,
                                web_client.enabled,
                            ):
                                if cancel.is_set():
                                    break
                                streamed = True
                                asyncio.run_coroutine_threadsafe(queue.put(("delta", delta)), loop).result()
                            return
                        except ToolsUnsupported:
                            if streamed:
                                return
                    if web_client.enabled and game_name:
                        _status("searching")
                        block = web_client.auto(game_name, question)
                        _status("idle")
                        if block:
                            outgoing = _inject_block(outgoing, block)
                    for delta in providers.iter_text(provider, outgoing, chosen, cancel, meta, image, _status):
                        if cancel.is_set():
                            break
                        asyncio.run_coroutine_threadsafe(queue.put(("delta", delta)), loop).result()
                except Exception as exc:  # noqa: BLE001 - surfaced to the UI as redacted text
                    asyncio.run_coroutine_threadsafe(queue.put(("error", exc)), loop).result()
                finally:
                    asyncio.run_coroutine_threadsafe(queue.put(("end", None)), loop).result()

            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
            tick_stop = threading.Event()
            if self._thinking_tick():
                threading.Thread(target=self._tick_loop, args=(tick_stop,), name="deckling-tick", daemon=True).start()
            worker = asyncio.create_task(asyncio.to_thread(_produce, queue, loop))
            try:
                while True:
                    kind, payload = await queue.get()
                    if kind == "end":
                        break
                    if kind in {"web", "status"}:
                        phase = str(payload)
                        await self._emit(
                            {
                                "type": "status",
                                "request_id": request_id,
                                "phase": phase,
                                "message": _status_text(phase),
                            }
                        )
                        if phase == "searching" or phase.startswith("reading"):
                            await self._emit({"type": "web", "request_id": request_id, "phase": "searching"})
                        continue
                    if kind == "error":
                        raise payload if isinstance(payload, Exception) else RuntimeError(str(payload))
                    text = str(payload)
                    collected.append(text)
                    await self._emit({"type": "chat_delta", "request_id": request_id, "text": text})
            finally:
                tick_stop.set()
                if not worker.done():
                    await worker
            if cancel.is_set() and not collected:
                await self._emit({"type": "chat_done", "request_id": request_id, "text": "", "cancelled": True})
                return
            full = "".join(collected)
            if full:
                self.store.append_message(session_id, "assistant", full, web_client.sources)
            resumed_now = str(meta.get("claude_session_id") or "")
            if resumed_now:
                self.store.set_claude_session(session_id, resumed_now)
            self.host.info("Chat finished kind=%s chars=%s", provider.get("kind"), len(full))
            data = self.store.load_sessions()
            shown = next((item for item in data["sessions"] if item.get("id") == session_id), None)
            if full and not cancel.is_set() and self.voice.enabled():
                self._speak_reply(full)
            await self._emit(
                {
                    "type": "chat_done",
                    "request_id": request_id,
                    "session_id": session_id,
                    "text": full,
                    "cancelled": cancel.is_set(),
                    "messages": list((shown or {}).get("messages") or []),
                    "sessions": [public_session_summary(item) for item in data["sessions"]],
                }
            )
        except asyncio.CancelledError:
            cancel.set()
            raise
        except Exception as exc:
            self.host.warning("Chat failed: %s", redact(str(exc)))
            await self._emit(
                {"type": "chat_error", "request_id": request_id, "session_id": session_id, "error": redact(str(exc))}
            )
        finally:
            resumed = str(meta.get("claude_session_id") or "")
            if resumed:
                try:
                    self.store.set_claude_session(session_id, resumed)
                except Exception:
                    self.host.warning("Could not store the Claude Code session id")
            self._streams.pop(request_id, None)

    async def _run_oauth(self, provider_id: str, flow: str, cancel: threading.Event) -> None:
        try:
            provider = self.store.get_provider(provider_id)
            kind = str(provider.get("kind"))
            if flow == "setup-token":
                await self._run_claude_setup(provider_id, cancel)
                return
            if flow == "device":
                started = await asyncio.to_thread(self._device_start, provider)
            else:
                started = self._pkce_start(provider)
            self._oauth[provider_id] = {**started, "status": "pending", "flow": flow}
            await self._emit({"type": "oauth", "provider_id": provider_id, **self._public_oauth(provider_id)})
            if flow == "device":
                tokens = await asyncio.to_thread(self._device_wait, provider, started, cancel)
            else:
                tokens = await asyncio.to_thread(self._pkce_wait, provider, started, cancel)
            self.store.set_oauth_tokens(
                provider_id,
                access_token=tokens["access_token"],
                refresh_token=tokens.get("refresh_token") or "",
                expires_at=int(tokens["expires_at"]),
            )
            self._oauth[provider_id] = {
                "status": "success",
                "flow": flow,
                "message": "Signed in. You can test the connection.",
            }
            self.host.info("OAuth finished kind=%s flow=%s", kind, flow)
        except asyncio.CancelledError:
            cancel.set()
            raise
        except Exception as exc:
            if cancel.is_set() and "cancelled" in str(exc).lower():
                self._oauth[provider_id] = {"status": "idle", "flow": flow, "message": "Login cancelled"}
            else:
                self.host.warning("OAuth failed kind flow=%s", flow)
                self._oauth[provider_id] = {"status": "error", "flow": flow, "message": redact(str(exc))}
        finally:
            self._oauth_cancel.pop(provider_id, None)
            await self._emit({"type": "oauth", "provider_id": provider_id, **self._public_oauth(provider_id)})

    async def _run_claude_setup(self, provider_id: str, cancel: threading.Event) -> None:
        binary = claude_code.find_claude()
        if not binary:
            raise ClaudeCodeError(claude_code.INSTALL_HINT)
        loop = asyncio.get_running_loop()
        self._oauth[provider_id] = {
            "status": "pending",
            "flow": "setup-token",
            "message": "Starting claude setup-token…",
        }
        await self._emit({"type": "oauth", "provider_id": provider_id, **self._public_oauth(provider_id)})

        def on_update(info: dict[str, str]) -> None:
            self._oauth[provider_id] = {
                "status": "pending",
                "flow": "setup-token",
                "verification_url": info.get("verification_url") or "",
                "user_code": info.get("user_code") or "",
                "message": info.get("message") or "",
            }
            future = asyncio.run_coroutine_threadsafe(
                self._emit({"type": "oauth", "provider_id": provider_id, **self._public_oauth(provider_id)}),
                loop,
            )
            future.result()

        token = await asyncio.to_thread(claude_code.run_setup_token, binary, cancel, on_update)
        self.store.set_api_key(provider_id, token)
        self._oauth[provider_id] = {
            "status": "success",
            "flow": "setup-token",
            "message": "Signed in. The Claude Code token is saved on this Deck.",
        }
        self.host.info("Claude Code setup-token saved")

    def _device_start(self, provider: dict[str, Any]) -> dict[str, Any]:
        client_id = str(provider.get("oauth_client_id") or "")
        if provider.get("kind") == "openai":
            started = oauth.openai_device_start(client_id)
            return {
                **started,
                "message": "Open the verification page and enter the code.",
            }
        if provider.get("kind") == "gemini":
            started = oauth.google_device_start(client_id)
            return {**started, "message": "Open the verification page and enter the code."}
        if provider.get("kind") == "xai":
            return oauth.xai_device_start()
        raise OAuthError("This provider does not offer device login")

    def _device_wait(self, provider: dict[str, Any], started: dict[str, Any], cancel: threading.Event) -> dict[str, Any]:
        interval = int(started.get("interval") or 5)
        expires_in = int(started.get("expires_in") or 15 * 60)
        deadline = time.time() + max(30, min(expires_in, 15 * 60))
        client_id = str(provider.get("oauth_client_id") or "")
        secret = str(provider.get("oauth_client_secret") or "")
        while not cancel.is_set() and time.time() < deadline:
            try:
                if provider.get("kind") == "openai":
                    tokens = oauth.openai_device_poll(client_id, str(started["device_auth_id"]), str(started["user_code"]))
                elif provider.get("kind") == "xai":
                    tokens = oauth.xai_device_poll(str(started["device_code"]))
                else:
                    tokens = oauth.google_device_poll(client_id, secret, str(started["device_code"]))
            except OAuthError as exc:
                if str(exc) == "slow_down":
                    interval += 5
                    tokens = None
                else:
                    raise
            if tokens:
                return tokens
            if cancel.wait(interval):
                break
        if cancel.is_set():
            raise OAuthError("Login cancelled")
        raise OAuthError("Login timed out. Start it again when you are ready to approve it.")

    def _pkce_start(self, provider: dict[str, Any]) -> dict[str, Any]:
        client_id = str(provider.get("oauth_client_id") or "")
        if not client_id:
            raise OAuthError("Paste an OAuth client ID first. API-key access does not need one.")
        verifier, challenge = oauth.pkce_pair()
        state = secrets.token_urlsafe(24)
        if provider.get("kind") == "openai":
            port = oauth.PKCE_PORT_OPENAI
            redirect = f"http://127.0.0.1:{port}/auth/callback"
            url = oauth.openai_authorize_url(client_id, redirect, challenge, state)
        elif provider.get("kind") == "gemini":
            port = oauth.PKCE_PORT_GOOGLE
            redirect = f"http://127.0.0.1:{port}/"
            url = oauth.google_authorize_url(client_id, redirect, challenge, state)
        else:
            raise OAuthError("This provider does not offer PKCE login")
        return {
            "verification_url": url,
            "user_code": "",
            "message": f"Register redirect URI {redirect} on this OAuth client if you have not already, then open the page.",
            "redirect_uri": redirect,
            "port": port,
            "code_verifier": verifier,
            "state": state,
        }

    def _pkce_wait(self, provider: dict[str, Any], started: dict[str, Any], cancel: threading.Event) -> dict[str, Any]:
        code = oauth.wait_for_redirect(int(started["port"]), str(started["state"]), cancel)
        client_id = str(provider.get("oauth_client_id") or "")
        if provider.get("kind") == "openai":
            return oauth.exchange_openai_code(
                client_id=client_id,
                code=code,
                code_verifier=str(started["code_verifier"]),
                redirect_uri=str(started["redirect_uri"]),
            )
        return oauth.exchange_google_code(
            client_id,
            str(provider.get("oauth_client_secret") or ""),
            code,
            str(started["code_verifier"]),
            str(started["redirect_uri"]),
        )


