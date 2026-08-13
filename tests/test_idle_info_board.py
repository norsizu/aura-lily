from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware" / "esp32" / "main"


def test_idle_board_waits_five_minutes_and_only_draws_when_settled():
    state = (FIRMWARE / "ui" / "state_helpers.c").read_text(encoding="utf-8")
    renderer = (FIRMWARE / "display" / "renderer.c").read_text(encoding="utf-8")

    assert "5LL * 60LL * 1000LL" in state
    assert "g_state.ui_mode == AURA_UI_IDLE" in state
    assert "g_state.dialogue_ticks_left <= 0" in state
    assert "!g_state.agent_panel_visible" in state
    assert "panels_draw_info_board" in renderer


def test_idle_board_preserves_status_bar_and_supports_sleep_inversion():
    panel = (FIRMWARE / "ui" / "panels_info_board.c").read_text(encoding="utf-8")
    websocket = (FIRMWARE / "network" / "ws_client.c").read_text(encoding="utf-8")

    assert "0, STATUS_BAR_HEIGHT" in panel
    assert "state->world_sleeping ? BOARD_BLACK : BOARD_WHITE" in panel
    assert 'cJSON_GetObjectItem(payload, "world_slot")' in websocket
    assert 'strcmp(world_slot->valuestring, "sleep") == 0' in websocket


def test_idle_board_can_be_left_by_either_physical_button():
    main = (FIRMWARE / "main.c").read_text(encoding="utf-8")

    assert "boot_short_open_menu" in main
    assert "bool was_info_board = g_state.info_board_visible;" not in main
    assert main.count("aura_ui_reset_idle_surface();") >= 2


def test_idle_board_is_interrupted_by_foreground_activity():
    helpers = (FIRMWARE / "ui" / "state_helpers.c").read_text(encoding="utf-8")
    websocket = (FIRMWARE / "network" / "ws_client.c").read_text(encoding="utf-8")
    main = (FIRMWARE / "main.c").read_text(encoding="utf-8")

    assert "static void interrupt_idle_surface_locked" in helpers
    for function_name in (
        "aura_ui_enter_listening",
        "aura_ui_ensure_listening",
        "aura_ui_set_dialogue",
        "aura_ui_set_agent_panel",
        "aura_ui_set_agent_visible",
    ):
        function = helpers[helpers.index(f"{function_name}(") :]
        function = function[: function.index("\n}")]
        assert "interrupt_idle_surface_locked" in function

    tts_started = websocket[websocket.index("static void ws_note_tts_started") :]
    tts_started = tts_started[: tts_started.index("\n}")]
    assert "aura_ui_reset_idle_surface();" in tts_started
    pending = websocket[websocket.index("static void ws_apply_pending_dialogue") :]
    pending = pending[: pending.index("\n}")]
    assert "s_pending_pose >= 0 || s_pending_scene >= 0" in pending
    assert "aura_ui_reset_idle_surface();" in pending
    emotion = websocket[websocket.index('strcmp(mtype, "emotion")') :]
    emotion = emotion[: emotion.index("/* ── dialogue")]
    assert "aura_ui_reset_idle_surface();" in emotion

    boot_menu = main[main.index('log_input_diag("boot_short_open_menu"') :]
    boot_menu = boot_menu[: boot_menu.index("continue;")]
    assert "s_menu_open = true;" in boot_menu
    assert "aura_ui_reset_idle_surface();" in boot_menu


def test_idle_board_uses_live_weather_and_notes_with_safe_empty_state():
    panel = (FIRMWARE / "ui" / "panels_info_board.c").read_text(encoding="utf-8")
    websocket = (FIRMWARE / "network" / "ws_client.c").read_text(encoding="utf-8")

    assert "state->weather_valid && state->weather_city[0] ? state->weather_city : \"--\"" in panel
    assert 'cJSON_GetObjectItem(payload, "notes")' in websocket
    assert "state->info_notes[i]" in panel
