"""Screen-help intent, Jarvis prompt, and which models can take an image."""

from __future__ import annotations

import base64
import re
from typing import Any

SCREEN_PHRASES = (
    "how do i do this",
    "what am i looking at",
    "help me with this",
    "what should i do here",
    "what's on my screen",
    "what is on my screen",
    "what's this",
    "what is this",
    "look at my screen",
    "look at the screen",
    "what should i do",
)

DEFAULT_QUESTION = "What am I looking at, and what should I do next?"

_VISION_HINTS = (
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
    "claude-3",
    "claude-sonnet",
    "claude-opus",
    "claude-haiku",
    "gemini",
    "grok-4",
    "grok-vision",
    "llava",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "pixtral",
    "moondream",
    "internvl",
    "vision",
)
_NOT_VISION = ("embedding", "whisper", "tts", "dall-e", "moderation", "transcribe", "audio")


def wants_screen_look(text: str) -> bool:
    cleaned = re.sub(r"[^a-z0-9' ]+", " ", str(text or "").lower())
    cleaned = " ".join(cleaned.split())
    return any(phrase in cleaned for phrase in SCREEN_PHRASES)


_VISION_CAPS = {"multimodal", "vision", "image"}


def model_sees_images(model_id: str) -> bool:
    lower = str(model_id or "").lower()
    if not lower or any(word in lower for word in _NOT_VISION):
        return False
    if any(hint in lower for hint in _VISION_HINTS):
        return True
    return "sonnet" in lower or "opus" in lower


def capabilities_see_images(capabilities: Any) -> bool:
    if not isinstance(capabilities, list):
        return False
    return any(str(item).strip().lower() in _VISION_CAPS for item in capabilities)


def row_sees_images(row: dict[str, Any], props_vision: bool | None = None) -> bool:
    """True when a model record, llama.cpp /props, or the model name says it takes images."""
    if capabilities_see_images(row.get("capabilities")):
        return True
    modalities = row.get("modalities")
    if isinstance(modalities, dict) and modalities.get("vision") is True:
        return True
    name = str(row.get("id") or row.get("name") or row.get("model") or "")
    if model_sees_images(name):
        return True
    return props_vision is True


def vision_override_map(provider: dict[str, Any] | None) -> dict[str, bool]:
    raw = (provider or {}).get("vision_override")
    if not isinstance(raw, dict):
        return {}
    found: dict[str, bool] = {}
    for key, value in raw.items():
        model = str(key or "").strip()
        if model:
            found[model[:200]] = bool(value)
    return found


def model_can_see(provider: dict[str, Any] | None, model_id: str, auto: set[str] | None = None) -> bool:
    """A saved per-model switch wins over detection."""
    model = str(model_id or "").strip()
    overrides = vision_override_map(provider)
    if model in overrides:
        return overrides[model]
    if auto is not None and model in auto:
        return True
    return model_sees_images(model)


def vision_ids(models: list[str], provider: dict[str, Any] | None = None, auto: set[str] | None = None) -> list[str]:
    return [model for model in models if model_can_see(provider, model, auto)]


def jarvis_prompt(game: str) -> str:
    name = " ".join(str(game or "").split()) or "no game detected"
    return (
        "You are Jarvis, a concise companion on a Steam Deck. "
        f"The person is playing: {name}. "
        "They shared a screenshot and a short question. "
        "Answer in two or three short spoken sentences. "
        "Be friendly, specific to the screenshot, and useful. "
        "Do not use markdown, labels, or a preamble. "
        "Do not spoil anything that is not already on screen."
    )


def _b64(image: bytes) -> str:
    return base64.b64encode(image).decode("ascii")


EARLIER_SCREENSHOT = "[earlier screenshot]"
_MULTI_IMAGE_KINDS = {"openai", "anthropic", "gemini", "xai"}


def keeps_multiple_images(kind: str) -> bool:
    """Cloud vision models can take several pictures. Local servers keep the latest one."""
    return str(kind or "") in _MULTI_IMAGE_KINDS


def _message_has_image(message: dict[str, Any]) -> bool:
    if message.get("images"):
        return True
    content = message.get("content")
    if not isinstance(content, list):
        return False
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in {"image_url", "image"} or "inlineData" in part or "inline_data" in part:
            return True
    return False


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    bits = [str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("text")]
    return " ".join(bit for bit in bits if bit)


def retain_latest_image(messages: list[dict[str, Any]], *, multiple: bool) -> list[dict[str, Any]]:
    """Drop older screenshots so a later look does not resend them."""
    if multiple:
        return messages
    indexes = [index for index, message in enumerate(messages) if _message_has_image(message)]
    if len(indexes) <= 1:
        return messages
    keep = indexes[-1]
    updated: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if index not in indexes or index == keep:
            updated.append(message)
            continue
        text = _message_text(message).strip()
        if EARLIER_SCREENSHOT not in text:
            text = f"{text} {EARLIER_SCREENSHOT}".strip()
        copied = dict(message)
        copied["content"] = text
        copied.pop("images", None)
        updated.append(copied)
    return updated


def openai_messages(messages: list[dict[str, Any]], image: bytes | None) -> list[dict[str, Any]]:
    if not image:
        return messages
    encoded = "data:image/jpeg;base64," + _b64(image)
    updated = [dict(item) for item in messages]
    for index in range(len(updated) - 1, -1, -1):
        content = updated[index].get("content")
        if updated[index].get("role") == "user" and isinstance(content, str):
            updated[index]["content"] = [
                {"type": "text", "text": content},
                {"type": "image_url", "image_url": {"url": encoded}},
            ]
            break
    return updated


def anthropic_messages(messages: list[dict[str, Any]], image: bytes | None) -> list[dict[str, Any]]:
    if not image:
        return messages
    encoded = _b64(image)
    updated = [dict(item) for item in messages]
    for index in range(len(updated) - 1, -1, -1):
        content = updated[index].get("content")
        if updated[index].get("role") == "user" and isinstance(content, str):
            updated[index]["content"] = [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": encoded}},
                {"type": "text", "text": content},
            ]
            break
    return updated


def gemini_parts(text: str, image: bytes | None) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [{"text": text}]
    if image:
        parts.insert(0, {"inlineData": {"mimeType": "image/jpeg", "data": _b64(image)}})
    return parts


def ollama_messages(messages: list[dict[str, Any]], image: bytes | None) -> list[dict[str, Any]]:
    if not image:
        return messages
    encoded = _b64(image)
    updated = [dict(item) for item in messages]
    for index in range(len(updated) - 1, -1, -1):
        if updated[index].get("role") == "user":
            updated[index]["images"] = [encoded]
            break
    return updated
