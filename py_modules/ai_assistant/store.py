"""Credential and session storage. Secret files are created mode 0600."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from typing import Any

from .catalog import get_kind
from .chats import chat_title, normalize_chats, preview_text, prune_sessions
from .game_context import normalize_context
from .web import normalize_web, public_web

_MAX_SESSIONS = 30
_MAX_MESSAGES = 200
_MAX_CONTENT = 100_000


def _chmod_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)


def _atomic_write(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    _chmod_dir(directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str, default: dict[str, Any]) -> dict[str, Any]:
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return default
    return data


def _blank_config() -> dict[str, Any]:
    return {
        "version": 1,
        "system_prompt": "",
        "default_provider_id": "",
        "default_model": "",
        "providers": [],
    }


def normalize_voice(raw: Any) -> dict[str, Any]:
    voice = {
        "voice_enabled": False,
        "voice_engine": "piper",
        "piper_voice": "en_US-lessac-medium",
        "kitten_voice": "Jasper",
        "voice_speed": 1.0,
        "screen_capture": True,
        "kitten_error": "",
    }
    if isinstance(raw, dict):
        for key in voice:
            if key in raw:
                voice[key] = raw[key]
    voice["voice_enabled"] = bool(voice["voice_enabled"])
    voice["screen_capture"] = bool(voice["screen_capture"])
    engine = str(voice["voice_engine"] or "piper")
    voice["voice_engine"] = engine if engine in {"piper", "kittentts"} else "piper"
    try:
        speed = float(voice["voice_speed"])
    except (TypeError, ValueError):
        speed = 1.0
    voice["voice_speed"] = max(0.5, min(2.0, speed))
    voice["piper_voice"] = str(voice["piper_voice"] or "en_US-lessac-medium")[:80]
    voice["kitten_voice"] = str(voice["kitten_voice"] or "Jasper")[:40]
    voice["kitten_error"] = str(voice["kitten_error"] or "")[:500]
    return voice


def normalize_hearing(raw: Any) -> dict[str, Any]:
    hearing = {
        "wake_enabled": False,
        "sensitivity": 0.5,
        "wake_model": "hey_jarvis",
        "stt_model": "tiny.en",
        "ptt_enabled": True,
        "battery_saver": False,
        "debug_audio": False,
        "done_sound": True,
        "thinking_tick": True,
        "thinking_tick_set": False,
        "wake_error": "",
        "stt_backend": "",
        "install_message": "",
        "install_progress": 0.0,
    }
    if isinstance(raw, dict):
        for key in hearing:
            if key in raw:
                hearing[key] = raw[key]
    hearing["wake_enabled"] = bool(hearing["wake_enabled"])
    hearing["ptt_enabled"] = bool(hearing["ptt_enabled"])
    hearing["battery_saver"] = bool(hearing["battery_saver"])
    hearing["debug_audio"] = bool(hearing["debug_audio"])
    hearing["done_sound"] = bool(hearing["done_sound"])
    hearing["thinking_tick_set"] = bool(hearing.get("thinking_tick_set"))
    if hearing["thinking_tick_set"]:
        hearing["thinking_tick"] = bool(hearing["thinking_tick"])
    else:
        hearing["thinking_tick"] = True
    try:
        sensitivity = float(hearing["sensitivity"])
    except (TypeError, ValueError):
        sensitivity = 0.5
    hearing["sensitivity"] = max(0.0, min(1.0, sensitivity))
    hearing["wake_model"] = str(hearing["wake_model"] or "hey_jarvis")[:40]
    hearing["stt_model"] = str(hearing["stt_model"] or "tiny.en")[:40]
    hearing["wake_error"] = str(hearing["wake_error"] or "")[:500]
    hearing["stt_backend"] = str(hearing["stt_backend"] or "")[:40]
    hearing["install_message"] = str(hearing["install_message"] or "")[:500]
    try:
        progress = float(hearing.get("install_progress") or 0)
    except (TypeError, ValueError):
        progress = 0.0
    hearing["install_progress"] = max(0.0, min(1.0, progress))
    return hearing


def _blank_sessions() -> dict[str, Any]:
    return {"version": 1, "current_id": "", "sessions": []}


def _merge_secret(incoming: Any, previous: str) -> str:
    """``None`` keeps the stored secret. ``\"\"`` clears it. Any other string replaces it."""
    if incoming is None:
        return previous
    if not isinstance(incoming, str):
        raise ValueError("Secret fields must be strings")
    return incoming.strip()


class Store:
    def __init__(self, settings_dir: str, runtime_dir: str) -> None:
        self.settings_dir = settings_dir
        self.runtime_dir = runtime_dir
        self.credentials_path = os.path.join(settings_dir, "credentials.json")
        self.sessions_path = os.path.join(runtime_dir, "sessions.json")
        self._lock = threading.RLock()
        _chmod_dir(settings_dir)
        _chmod_dir(runtime_dir)

    def load_config(self) -> dict[str, Any]:
        with self._lock:
            data = _read_json(self.credentials_path, _blank_config())
            data.setdefault("providers", [])
            data.setdefault("system_prompt", "")
            data.setdefault("default_provider_id", "")
            data.setdefault("default_model", "")
            return data

    def save_config(self, data: dict[str, Any]) -> None:
        with self._lock:
            _atomic_write(self.credentials_path, data)

    def load_sessions(self) -> dict[str, Any]:
        with self._lock:
            self._migrate_legacy_sessions()
            index = _read_json(self.sessions_path, _blank_sessions())
            index.setdefault("sessions", [])
            index.setdefault("current_id", "")
            full: list[dict[str, Any]] = []
            for meta in index["sessions"]:
                if not isinstance(meta, dict) or not meta.get("id"):
                    continue
                record = _session_meta(meta)
                record["messages"] = self._read_chat_messages(str(meta["id"]))
                full.append(record)
            return {"current_id": index.get("current_id") or "", "sessions": full}

    def save_sessions(self, data: dict[str, Any]) -> None:
        with self._lock:
            keep = normalize_chats(self.load_config().get("chats"))["keep"]
            current_id = str(data.get("current_id") or "")
            sessions = [item for item in (data.get("sessions") or []) if isinstance(item, dict) and item.get("id")]
            sessions = prune_sessions(sessions, keep, current_id)
            directory = self._chats_dir()
            os.makedirs(directory, exist_ok=True)
            os.chmod(directory, 0o700)
            slim: list[dict[str, Any]] = []
            kept: set[str] = set()
            for item in sessions:
                session_id = str(item["id"])
                kept.add(session_id)
                self._write_chat_messages(session_id, list(item.get("messages") or []))
                slim.append(_session_meta(item))
            for name in os.listdir(directory):
                if not name.endswith(".json"):
                    continue
                session_id = name[:-5]
                if session_id not in kept:
                    try:
                        os.remove(os.path.join(directory, name))
                    except OSError:
                        pass
            _atomic_write(self.sessions_path, {"version": 2, "current_id": current_id, "sessions": slim})

    def delete_private_files(self) -> None:
        with self._lock:
            for path in (self.credentials_path, self.sessions_path):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            directory = self._chats_dir()
            if os.path.isdir(directory):
                for name in os.listdir(directory):
                    try:
                        os.remove(os.path.join(directory, name))
                    except OSError:
                        pass

    def upsert_provider(self, incoming: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(incoming, dict):
            raise ValueError("Provider must be an object")
        kind = get_kind(str(incoming.get("kind") or ""))
        name = str(incoming.get("name") or "").strip()
        if not name or len(name) > 80:
            raise ValueError("Provider name must be 1-80 characters")
        base_url = str(incoming.get("base_url") or kind.default_base_url).strip()
        if kind.kind == "custom" and not base_url:
            raise ValueError("Custom providers need a base URL")
        if base_url:
            from .http_util import check_url

            check_url(base_url if "://" in base_url else f"http://{base_url}")
        raw_model = incoming.get("default_model")
        if raw_model is None:
            model = str(kind.default_model or "").strip()
        else:
            model = str(raw_model).strip()
        if len(model) > 200:
            raise ValueError("Model id is too long")
        try:
            max_tokens = int(incoming.get("max_tokens") or 1024)
        except (TypeError, ValueError) as exc:
            raise ValueError("Max tokens must be a number") from exc
        max_tokens = max(1, min(max_tokens, 8192))

        with self._lock:
            config = self.load_config()
            providers: list[dict[str, Any]] = list(config["providers"])
            provider_id = str(incoming.get("id") or "")
            existing = next((item for item in providers if item.get("id") == provider_id), None)
            if provider_id and existing is None:
                raise ValueError("That provider no longer exists")
            record = dict(existing or {})
            record.update(
                {
                    "id": existing.get("id") if existing else uuid.uuid4().hex,
                    "kind": kind.kind,
                    "name": name,
                    "base_url": base_url,
                    "default_model": model,
                    "max_tokens": max_tokens,
                    "api_key": _merge_secret(incoming.get("api_key"), str(record.get("api_key") or "")),
                    "oauth_client_id": str(
                        incoming.get("oauth_client_id")
                        if incoming.get("oauth_client_id") is not None
                        else record.get("oauth_client_id") or ""
                    ).strip(),
                    "oauth_client_secret": _merge_secret(
                        incoming.get("oauth_client_secret"),
                        str(record.get("oauth_client_secret") or ""),
                    ),
                    "oauth_access_token": str(record.get("oauth_access_token") or ""),
                    "oauth_refresh_token": str(record.get("oauth_refresh_token") or ""),
                    "oauth_expires_at": int(record.get("oauth_expires_at") or 0),
                }
            )
            if existing:
                providers = [record if item.get("id") == record["id"] else item for item in providers]
            else:
                providers.append(record)
            config["providers"] = providers
            if not config.get("default_provider_id"):
                config["default_provider_id"] = record["id"]
                config["default_model"] = record["default_model"]
            self.save_config(config)
            return record

    def set_model_vision(self, provider_id: str, model: str, enabled: bool) -> dict[str, Any]:
        chosen = str(model or "").strip()
        if not chosen or len(chosen) > 200:
            raise ValueError("Choose a model before changing whether it can see images")
        with self._lock:
            config = self.load_config()
            providers: list[dict[str, Any]] = list(config["providers"])
            existing = next((item for item in providers if item.get("id") == provider_id), None)
            if existing is None:
                raise ValueError("That provider no longer exists")
            overrides = _vision_override(existing.get("vision_override"))
            overrides[chosen] = bool(enabled)
            if len(overrides) > 40:
                raise ValueError("Too many vision overrides on this provider")
            existing["vision_override"] = overrides
            config["providers"] = providers
            self.save_config(config)
            return existing

    def delete_provider(self, provider_id: str) -> None:
        with self._lock:
            config = self.load_config()
            config["providers"] = [item for item in config["providers"] if item.get("id") != provider_id]
            if config.get("default_provider_id") == provider_id:
                config["default_provider_id"] = ""
                config["default_model"] = ""
                if config["providers"]:
                    first = config["providers"][0]
                    config["default_provider_id"] = first["id"]
                    config["default_model"] = first.get("default_model") or ""
            self.save_config(config)

    def update_settings(self, system_prompt: str, default_provider_id: str, default_model: str) -> dict[str, Any]:
        if len(system_prompt) > 8000:
            raise ValueError("System prompt is too long")
        if len(default_model) > 200:
            raise ValueError("Model id is too long")
        with self._lock:
            config = self.load_config()
            if default_provider_id and not any(item.get("id") == default_provider_id for item in config["providers"]):
                raise ValueError("Choose a saved provider")
            config["system_prompt"] = system_prompt
            config["default_provider_id"] = default_provider_id
            config["default_model"] = default_model.strip()
            self.save_config(config)
            return config

    def update_voice(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("Voice settings must be an object")
        with self._lock:
            config = self.load_config()
            voice = normalize_voice(config.get("voice"))
            for key in voice:
                if key in patch:
                    voice[key] = patch[key]
            voice = normalize_voice(voice)
            config["voice"] = voice
            self.save_config(config)
            return voice

    def set_connection(self, provider_id: str, status: str, detail: str) -> None:
        with self._lock:
            config = self.load_config()
            for item in config["providers"]:
                if item.get("id") == provider_id:
                    item["connection_status"] = _connection_status(status)
                    item["connection_detail"] = str(detail or "")[:300]
                    self.save_config(config)
                    return

    def update_context(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("Game context settings must be an object")
        with self._lock:
            config = self.load_config()
            context = normalize_context(config.get("context"))
            for key in context:
                if key in patch:
                    context[key] = patch[key]
            context = normalize_context(context)
            config["context"] = context
            self.save_config(config)
            return context

    def update_chats(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("Chat settings must be an object")
        with self._lock:
            config = self.load_config()
            chats = normalize_chats(config.get("chats"))
            if "keep" in patch:
                chats["keep"] = patch["keep"]
            if "remember_model" in patch:
                chats["remember_model"] = patch["remember_model"]
            chats = normalize_chats(chats)
            config["chats"] = chats
            self.save_config(config)
            data = self.load_sessions()
            self.save_sessions(data)
            return chats

    def update_web(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("Web lookup settings must be an object")
        with self._lock:
            config = self.load_config()
            web = normalize_web(config.get("web"))
            for key in ("enabled", "provider", "searxng_url"):
                if key in patch:
                    web[key] = patch[key]
            for key in ("brave_key", "tavily_key", "serper_key"):
                if key in patch and patch[key] is not None:
                    web[key] = patch[key]
            web = normalize_web(web)
            config["web"] = web
            self.save_config(config)
            return public_web(web)

    def update_hearing(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("Listening settings must be an object")
        with self._lock:
            config = self.load_config()
            hearing = normalize_hearing(config.get("hearing"))
            for key in hearing:
                if key in patch:
                    hearing[key] = patch[key]
            hearing = normalize_hearing(hearing)
            config["hearing"] = hearing
            self.save_config(config)
            return hearing

    def set_oauth_tokens(
        self,
        provider_id: str,
        *,
        access_token: str,
        refresh_token: str,
        expires_at: int,
    ) -> None:
        with self._lock:
            config = self.load_config()
            found = False
            for item in config["providers"]:
                if item.get("id") == provider_id:
                    item["oauth_access_token"] = access_token
                    if refresh_token:
                        item["oauth_refresh_token"] = refresh_token
                    item["oauth_expires_at"] = expires_at
                    found = True
            if not found:
                raise ValueError("That provider no longer exists")
            self.save_config(config)

    def get_provider(self, provider_id: str) -> dict[str, Any]:
        config = self.load_config()
        for item in config["providers"]:
            if item.get("id") == provider_id:
                return item
        raise ValueError("That provider no longer exists")

    def ensure_session(self) -> tuple[dict[str, Any], dict[str, Any]]:
        data = self.load_sessions()
        sessions: list[dict[str, Any]] = data["sessions"]
        current = next((item for item in sessions if item.get("id") == data.get("current_id")), None)
        if current is None:
            current = _new_session()
            sessions.insert(0, current)
            data["current_id"] = current["id"]
            data["sessions"] = sessions
            self.save_sessions(data)
        return data, current

    def new_session(self, game_key: str = "general", game_label: str = "General") -> dict[str, Any]:
        with self._lock:
            data = self.load_sessions()
            current = _new_session(game_key, game_label)
            data["sessions"] = [current, *data["sessions"]]
            data["current_id"] = current["id"]
            self.save_sessions(data)
            return current

    def focus_game(self, game_key: str, game_label: str) -> dict[str, Any]:
        """Open the newest chat for this game, or create one. Same game keeps the chat that is open."""
        with self._lock:
            data = self.load_sessions()
            current = next((item for item in data["sessions"] if item.get("id") == data.get("current_id")), None)
            if current is not None and str(current.get("game_key") or "general") == game_key:
                return {"switched": False, "session": current}
            matches = [item for item in data["sessions"] if str(item.get("game_key") or "general") == game_key]
            if not matches:
                created = self.new_session(game_key, game_label)
                return {"switched": True, "session": created}
            matches.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
            chosen = matches[0]
            data["current_id"] = chosen["id"]
            self.save_sessions(data)
            return {"switched": True, "session": chosen}

    def switch_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            data = self.load_sessions()
            match = next((item for item in data["sessions"] if item.get("id") == session_id), None)
            if match is None:
                raise ValueError("That conversation no longer exists")
            data["current_id"] = session_id
            self.save_sessions(data)
            return match

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        with self._lock:
            data = self.load_sessions()
            match = next((item for item in data["sessions"] if item.get("id") == session_id), None)
            if match is None:
                raise ValueError("That conversation no longer exists")
            match["title"] = chat_title(title)[:60]
            match["renamed"] = True
            match["updated_at"] = int(time.time())
            self.save_sessions(data)
            return match

    def pin_session(self, session_id: str, pinned: bool) -> dict[str, Any]:
        with self._lock:
            data = self.load_sessions()
            match = next((item for item in data["sessions"] if item.get("id") == session_id), None)
            if match is None:
                raise ValueError("That conversation no longer exists")
            match["pinned"] = bool(pinned)
            self.save_sessions(data)
            return match

    def move_session(self, session_id: str, game_key: str, game_label: str) -> dict[str, Any]:
        with self._lock:
            data = self.load_sessions()
            match = next((item for item in data["sessions"] if item.get("id") == session_id), None)
            if match is None:
                raise ValueError("That conversation no longer exists")
            key = str(game_key or "general")[:120]
            label = " ".join(str(game_label or "").split())[:80] or "General"
            match["game_key"] = key
            match["game_label"] = label
            self.save_sessions(data)
            return match

    def remember_model(self, session_id: str, provider_id: str, model: str) -> None:
        if not normalize_chats(self.load_config().get("chats"))["remember_model"]:
            return
        with self._lock:
            data = self.load_sessions()
            match = next((item for item in data["sessions"] if item.get("id") == session_id), None)
            if match is None:
                return
            match["provider_id"] = str(provider_id or "")[:80]
            match["model"] = str(model or "")[:120]
            self.save_sessions(data)

    def clear_session(self, session_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            data, current = self.ensure_session()
            target_id = session_id or current["id"]
            for item in data["sessions"]:
                if item.get("id") == target_id:
                    item["messages"] = []
                    item["title"] = "New chat"
                    item["renamed"] = False
                    item["preview"] = ""
                    item["updated_at"] = int(time.time())
                    item.pop("claude_session_id", None)
                    self.save_sessions(data)
                    return item
            raise ValueError("That conversation no longer exists")

    def delete_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            data = self.load_sessions()
            data["sessions"] = [item for item in data["sessions"] if item.get("id") != session_id]
            if data.get("current_id") == session_id:
                if data["sessions"]:
                    data["current_id"] = data["sessions"][0]["id"]
                else:
                    created = _new_session()
                    data["sessions"] = [created]
                    data["current_id"] = created["id"]
            self.save_sessions(data)
            return data

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        sources: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        text = content[:_MAX_CONTENT]
        with self._lock:
            data = self.load_sessions()
            match = next((item for item in data["sessions"] if item.get("id") == session_id), None)
            if match is None:
                raise ValueError("That conversation no longer exists")
            message: dict[str, Any] = {
                "id": uuid.uuid4().hex,
                "role": role,
                "content": text,
                "created_at": int(time.time()),
            }
            if role == "assistant" and sources:
                cleaned = []
                for item in sources[:6]:
                    url = str(item.get("url") or "")
                    if url.startswith("http://") or url.startswith("https://"):
                        cleaned.append({"title": str(item.get("title") or url)[:140], "url": url[:500]})
                if cleaned:
                    message["sources"] = cleaned
            messages = list(match.get("messages") or [])
            messages.append(message)
            match["messages"] = messages[-_MAX_MESSAGES:]
            match["updated_at"] = message["created_at"]
            match["preview"] = preview_text(match["messages"])
            if role == "user" and not match.get("renamed") and (not match.get("title") or match.get("title") == "New chat"):
                match["title"] = chat_title(text)
            self.save_sessions(data)
            return message

    def set_api_key(self, provider_id: str, api_key: str) -> None:
        with self._lock:
            config = self.load_config()
            found = False
            for item in config["providers"]:
                if item.get("id") == provider_id:
                    item["api_key"] = api_key.strip()
                    found = True
            if not found:
                raise ValueError("That provider no longer exists")
            self.save_config(config)

    def _chats_dir(self) -> str:
        return os.path.join(self.runtime_dir, "chats")

    def _chat_path(self, session_id: str) -> str:
        cleaned = str(session_id)
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        if not cleaned or len(cleaned) > 80 or any(character not in allowed for character in cleaned):
            raise ValueError("Bad chat id")
        return os.path.join(self._chats_dir(), cleaned + ".json")

    def _migrate_legacy_sessions(self) -> None:
        if not os.path.exists(self.sessions_path):
            return
        try:
            with open(self.sessions_path, encoding="utf-8") as handle:
                index = json.load(handle)
        except (OSError, ValueError):
            raise
        if not isinstance(index, dict):
            return
        sessions = index.get("sessions") or []
        if not any(isinstance(item, dict) and "messages" in item for item in sessions):
            return
        os.makedirs(self._chats_dir(), exist_ok=True)
        os.chmod(self._chats_dir(), 0o700)
        slim = []
        for item in sessions:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            self._write_chat_messages(str(item["id"]), list(item.get("messages") or []))
            slim.append(_session_meta(item))
        _atomic_write(
            self.sessions_path,
            {"version": 2, "current_id": index.get("current_id") or "", "sessions": slim},
        )

    def _read_chat_messages(self, session_id: str) -> list[dict[str, Any]]:
        try:
            path = self._chat_path(session_id)
        except ValueError:
            return []
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return []
        messages = data.get("messages") if isinstance(data, dict) else None
        return list(messages) if isinstance(messages, list) else []

    def _write_chat_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        _atomic_write(self._chat_path(session_id), {"messages": messages[-_MAX_MESSAGES:]})

    def set_claude_session(self, session_id: str, claude_session_id: str) -> None:
        cleaned = str(claude_session_id or "").strip()
        if not cleaned or len(cleaned) > 200 or any(character.isspace() for character in cleaned):
            return
        with self._lock:
            data = self.load_sessions()
            for item in data["sessions"]:
                if item.get("id") == session_id:
                    item["claude_session_id"] = cleaned
                    self.save_sessions(data)
                    return


def _new_session(game_key: str = "general", game_label: str = "General") -> dict[str, Any]:
    label = " ".join(str(game_label or "").split())[:80] or "General"
    return {
        "id": uuid.uuid4().hex,
        "title": "New chat",
        "updated_at": int(time.time()),
        "messages": [],
        "game_key": str(game_key or "general")[:120],
        "game_label": label,
        "pinned": False,
        "renamed": False,
        "preview": "",
        "provider_id": "",
        "model": "",
    }


def _session_meta(item: dict[str, Any]) -> dict[str, Any]:
    key = str(item.get("game_key") or "general")[:120]
    label = " ".join(str(item.get("game_label") or "").split())[:80]
    if not label:
        label = "General" if key == "general" else "Game"
    meta = {
        "id": item.get("id") or "",
        "title": str(item.get("title") or "New chat")[:80],
        "updated_at": int(item.get("updated_at") or 0),
        "game_key": key,
        "game_label": label,
        "pinned": bool(item.get("pinned")),
        "renamed": bool(item.get("renamed")),
        "preview": str(item.get("preview") or preview_text(list(item.get("messages") or [])))[:100],
        "provider_id": str(item.get("provider_id") or "")[:80],
        "model": str(item.get("model") or "")[:120],
    }
    claude_session = str(item.get("claude_session_id") or "").strip()
    if claude_session:
        meta["claude_session_id"] = claude_session[:200]
    return meta


def _vision_override(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    found: dict[str, bool] = {}
    for key, enabled in value.items():
        model = str(key or "").strip()[:200]
        if model:
            found[model] = bool(enabled)
    return found


def _connection_status(value: Any) -> str:
    status = str(value or "unknown")
    return status if status in {"connected", "error", "unknown"} else "unknown"


def public_provider(record: dict[str, Any]) -> dict[str, Any]:
    """Provider fields that are safe to send to the frontend."""
    api_key = str(record.get("api_key") or "")
    from .redact import last4

    return {
        "id": record.get("id") or "",
        "kind": record.get("kind") or "",
        "name": record.get("name") or "",
        "base_url": record.get("base_url") or "",
        "default_model": record.get("default_model") or "",
        "max_tokens": int(record.get("max_tokens") or 1024),
        "has_api_key": bool(api_key),
        "api_key_last4": last4(api_key),
        "oauth_client_id": record.get("oauth_client_id") or "",
        "has_oauth_secret": bool(record.get("oauth_client_secret")),
        "oauth_connected": bool(record.get("oauth_access_token") or record.get("oauth_refresh_token")),
        "oauth_expires_at": int(record.get("oauth_expires_at") or 0),
        "connection_status": _connection_status(record.get("connection_status")),
        "connection_detail": str(record.get("connection_detail") or "")[:300],
        "vision_override": _vision_override(record.get("vision_override")),
    }


def public_session_summary(record: dict[str, Any]) -> dict[str, Any]:
    messages = list(record.get("messages") or [])
    return {
        "id": record.get("id") or "",
        "title": record.get("title") or "New chat",
        "updated_at": int(record.get("updated_at") or 0),
        "game_key": record.get("game_key") or "general",
        "game_label": record.get("game_label") or "General",
        "pinned": bool(record.get("pinned")),
        "preview": record.get("preview") or preview_text(messages),
        "provider_id": record.get("provider_id") or "",
        "model": record.get("model") or "",
    }
