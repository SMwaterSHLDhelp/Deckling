"""Game context from a mocked Steam snapshot. No Steam client and no store network."""

from __future__ import annotations

import asyncio
import json
import threading

from ai_assistant.game_context import enrich_store, format_block, prepare_snapshot, rom_title, suggestions
from ai_assistant.service import AssistantService, outgoing_history


def test_rom_title_comes_from_the_emulator_command() -> None:
    options = 'retroarch -L /cores/mgba_libretro.so "/home/deck/Emulation/roms/gba/Pokemon_Emerald.gba"'
    assert rom_title("RetroArch", "retroarch", options) == "Pokemon Emerald"
    prepared = prepare_snapshot(
        {
            "appid": 2_147_483_648,
            "name": "RetroArch",
            "shortcut": True,
            "exe": "/usr/bin/retroarch",
            "launch_options": options,
            "sources": ["router"],
        }
    )
    assert prepared["name"] == "Pokemon Emerald"
    assert prepared["emulator"] == "retroarch"
    assert prepared["shortcut"] is True


def test_prompt_keeps_only_known_fields_and_honors_privacy() -> None:
    game = prepare_snapshot(
        {
            "appid": 1245620,
            "name": "Elden Ring",
            "rich_presence": "Act 2 - The Forest",
            "playtime_minutes": 125,
            "session_started": 1_000,
            "achievements_unlocked": 12,
            "achievements_total": 40,
            "recent_achievements": ["First Blood"],
            "next_achievement": "The Forest",
            "compat_tool": "proton-9.0-4",
            "sources": ["router", "rich-presence", "achievements", "compat"],
        },
        now=1_000 + 25 * 60,
    )
    game["genres"] = ["Action"]
    game["developer"] = "FromSoftware"
    block = format_block(game, {"share_game_context": True, "include_achievements": True, "include_playtime": True})
    assert "Game: Elden Ring" in block
    assert "Act 2 - The Forest" in block
    assert "Achievements: 12/40" in block
    assert "Playtime:" in block
    assert "proton-9.0-4" in block
    assert "exe" not in block.lower() or "Shortcut exe" not in block
    hidden = format_block(
        game,
        {"share_game_context": True, "include_achievements": False, "include_playtime": False},
    )
    assert "Achievements" not in hidden
    assert "Playtime" not in hidden
    assert "Session" not in hidden
    assert format_block(game, {"share_game_context": False}) == ""
    assert any("The Forest" in item for item in suggestions(game))


def test_store_details_are_cached_per_appid(tmp_path) -> None:
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return json.dumps(
            {
                "1245620": {
                    "success": True,
                    "data": {
                        "short_description": "<p>A big tree.</p>",
                        "developers": ["FromSoftware"],
                        "genres": [{"description": "Action"}],
                        "header_image": "https://cdn.example/capsule.jpg",
                    },
                }
            }
        ).encode()

    game = prepare_snapshot({"appid": 1245620, "name": "Elden Ring"})
    enrich_store(game, str(tmp_path), fetch, now=10)
    enrich_store(game, str(tmp_path), fetch, now=11)
    assert calls == ["https://store.steampowered.com/api/appdetails?appids=1245620&l=english"]
    assert game["developer"] == "FromSoftware"
    assert game["about"] == "A big tree."
    assert game["capsule"].endswith("capsule.jpg")
    assert game["guide"].endswith("/guides/")

    shortcut = prepare_snapshot({"appid": 2_147_483_648, "name": "RetroArch", "shortcut": True})
    enrich_store(shortcut, str(tmp_path), fetch, now=12)
    assert len(calls) == 1
    assert "capsule" not in shortcut


def test_store_failure_is_not_cached(tmp_path) -> None:
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        raise OSError("offline")

    game = prepare_snapshot({"appid": 1245620, "name": "Elden Ring"})
    enrich_store(game, str(tmp_path), fetch, now=10)
    enrich_store(game, str(tmp_path), fetch, now=11)
    assert len(calls) == 2
    assert game.get("about", "") == ""
    assert game["guide"].endswith("/guides/")


def test_service_adds_the_block_for_the_running_game(tmp_path) -> None:
    service = AssistantService(str(tmp_path / "settings"), str(tmp_path / "runtime"))
    result = service.set_game_context(
        {
            "appid": 2_147_483_648,
            "name": "RetroArch",
            "shortcut": True,
            "exe": "retroarch",
            "launch_options": "/roms/snes/Chrono_Trigger.sfc",
            "rich_presence": "600 AD",
            "sources": ["router", "rich-presence"],
        }
    )
    assert result["ok"] is True
    assert result["game"]["name"] == "Chrono Trigger"
    block = service._with_game_context("Be brief.", service.store.load_config())
    assert block.startswith("Be brief.")
    assert "Chrono Trigger" in block
    assert "600 AD" in block
    service.save_context({"share_game_context": False})
    assert service._with_game_context("Be brief.", service.store.load_config()) == "Be brief."


def test_switching_games_opens_that_chat_and_sends_the_current_one(tmp_path) -> None:
    service = AssistantService(str(tmp_path / "settings"), str(tmp_path / "runtime"))
    sephiria = service.set_game_context({"appid": 11, "name": "Sephiria", "sources": ["router"]})
    assert sephiria["focused"] is True
    assert sephiria["notice"] == ""
    seph_id = sephiria["current_session_id"]
    service.store.append_message(seph_id, "user", "[Playing: Sephiria]\n\nWhere is the door?")
    keeper = service.set_game_context({"appid": 22, "name": "Graveyard Keeper", "sources": ["router"]})
    assert keeper["notice"] == "Switched to Graveyard Keeper"
    assert keeper["focused"] is True
    assert keeper["current_session_id"] != seph_id
    assert keeper["game"]["name"] == "Graveyard Keeper"
    keeper_id = keeper["current_session_id"]
    block = service._with_game_context("Be brief.", service.store.load_config())
    assert "Graveyard Keeper" in block
    assert "Sephiria" not in block
    rewritten = outgoing_history(
        [
            {"role": "user", "content": "[Playing: Sephiria]\n\nWhere is the door?"},
            {"role": "assistant", "content": "In the chapel."},
            {"role": "user", "content": "[Playing: Sephiria]\n\nWhat now?"},
        ],
        "Graveyard Keeper",
    )
    assert "Sephiria" not in rewritten[0]["content"]
    assert rewritten[2]["content"].startswith("[Playing: Graveyard Keeper]")
    back = service.set_game_context({"appid": 11, "name": "Sephiria", "sources": ["router"]})
    assert back["notice"] == "Switched to Sephiria"
    assert back["current_session_id"] == seph_id
    service._streams["stuck"] = threading.Event()
    again = service.set_game_context({"appid": 22, "name": "Graveyard Keeper", "sources": ["router"]})
    assert again["focused"] is True
    assert again["current_session_id"] == keeper_id
    assert again["notice"] == "Switched to Graveyard Keeper"

    async def run() -> None:
        saved = service.save_provider(
            {"kind": "custom", "name": "Local", "base_url": "http://127.0.0.1:9/v1", "default_model": "m"}
        )
        service._streams.clear()
        result = service.start_chat(
            saved["provider"]["id"],
            "m",
            "What should I do?",
            "req-switch",
            "Sephiria",
        )
        assert result["ok"] is True
        assert result["messages"][-1]["content"].startswith("[Playing: Graveyard Keeper]")
        assert "Sephiria" not in result["messages"][-1]["content"]
        await service.shutdown()

    asyncio.run(run())
