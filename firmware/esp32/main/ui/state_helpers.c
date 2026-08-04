#include "state_helpers.h"

#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/portmacro.h"

static portMUX_TYPE s_ui_state_mux = portMUX_INITIALIZER_UNLOCKED;

static void copy_text(char *dst, size_t dst_size, const char *src)
{
    if (!dst || dst_size == 0) return;
    if (!src) src = "";
    strncpy(dst, src, dst_size - 1);
    dst[dst_size - 1] = '\0';
}

static void clear_dialogue_locked(void)
{
    g_state.display_text[0] = '\0';
    g_state.text_char_index = 0;
    g_state.dialogue_page_tick = 0;
    g_state.dialogue_ticks_left = 0;
}

static void clear_agent_panel_locked(void)
{
    g_state.agent_panel_visible = false;
    g_state.agent_progress = 0;
    g_state.agent_title[0] = '\0';
    g_state.agent_status[0] = '\0';
}

void aura_ui_mark_dirty(void)
{
    portENTER_CRITICAL(&s_ui_state_mux);
    g_state.dirty = true;
    portEXIT_CRITICAL(&s_ui_state_mux);
}

void aura_ui_enter_listening(int pose, int mic_level)
{
    if (mic_level < 0) mic_level = 0;
    if (mic_level > 100) mic_level = 100;

    portENTER_CRITICAL(&s_ui_state_mux);
    g_state.ui_mode = AURA_UI_LISTENING;
    g_state.ui_anim_tick = 0;
    g_state.mic_level = mic_level;
    if (pose >= 0) {
        g_state.current_pose = pose;
    }
    clear_dialogue_locked();
    clear_agent_panel_locked();
    g_state.dirty = true;
    portEXIT_CRITICAL(&s_ui_state_mux);
}

bool aura_ui_ensure_listening(int pose, int min_mic_level)
{
    bool changed = false;

    if (min_mic_level < 0) min_mic_level = 0;
    if (min_mic_level > 100) min_mic_level = 100;

    portENTER_CRITICAL(&s_ui_state_mux);

    if (g_state.ui_mode != AURA_UI_LISTENING) {
        g_state.ui_mode = AURA_UI_LISTENING;
        g_state.ui_anim_tick = 0;
        changed = true;
    }
    if (pose >= 0 && g_state.current_pose != pose) {
        g_state.current_pose = pose;
        changed = true;
    }
    if (g_state.mic_level < min_mic_level) {
        g_state.mic_level = min_mic_level;
        changed = true;
    }
    if (g_state.display_text[0] != '\0' ||
        g_state.text_char_index != 0 ||
        g_state.dialogue_page_tick != 0 ||
        g_state.dialogue_ticks_left != 0) {
        clear_dialogue_locked();
        changed = true;
    }
    if (g_state.agent_panel_visible || g_state.agent_progress != 0 ||
        g_state.agent_title[0] != '\0' || g_state.agent_status[0] != '\0') {
        clear_agent_panel_locked();
        changed = true;
    }

    if (changed) {
        g_state.dirty = true;
    }

    portEXIT_CRITICAL(&s_ui_state_mux);
    return changed;
}

void aura_ui_set_dialogue(const char *text, int ttl_ticks)
{
    portENTER_CRITICAL(&s_ui_state_mux);
    if (g_state.ui_mode == AURA_UI_LISTENING && text && text[0] != '\0') {
        /*
         * LISTENING is modal: late status/dialogue writes must not cover the
         * recording capsule. Empty dialogue is still accepted as an explicit
         * clear operation from state transitions.
         */
        clear_dialogue_locked();
        g_state.dirty = true;
        portEXIT_CRITICAL(&s_ui_state_mux);
        return;
    }
    if (text) {
        copy_text(g_state.display_text, sizeof(g_state.display_text), text);
        g_state.text_char_index = 0;
        g_state.dialogue_page_tick = 0;
    }
    g_state.dialogue_ticks_left = ttl_ticks;
    g_state.dirty = true;
    portEXIT_CRITICAL(&s_ui_state_mux);
}

void aura_ui_clear_dialogue(void)
{
    portENTER_CRITICAL(&s_ui_state_mux);
    clear_dialogue_locked();
    g_state.dirty = true;
    portEXIT_CRITICAL(&s_ui_state_mux);
}

void aura_ui_set_dialogue_progress(int progress_permille)
{
    if (progress_permille < 0) progress_permille = 0;
    if (progress_permille > 999) progress_permille = 999;

    portENTER_CRITICAL(&s_ui_state_mux);
    if (g_state.dialogue_page_tick != progress_permille) {
        g_state.dialogue_page_tick = progress_permille;
        g_state.dirty = true;
    }
    portEXIT_CRITICAL(&s_ui_state_mux);
}

void aura_ui_limit_dialogue_ttl(int ttl_ticks)
{
    if (ttl_ticks < 0) ttl_ticks = 0;

    portENTER_CRITICAL(&s_ui_state_mux);
    if (g_state.display_text[0] != '\0' &&
        (g_state.dialogue_ticks_left <= 0 || g_state.dialogue_ticks_left > ttl_ticks)) {
        g_state.dialogue_ticks_left = ttl_ticks;
        g_state.dirty = true;
    }
    portEXIT_CRITICAL(&s_ui_state_mux);
}

void aura_ui_set_agent_panel(bool visible, int progress,
                             const char *title, const char *status)
{
    if (progress < 0) progress = 0;
    if (progress > 100) progress = 100;

    portENTER_CRITICAL(&s_ui_state_mux);
    g_state.agent_panel_visible = visible;
    g_state.agent_progress = progress;
    if (title) {
        copy_text(g_state.agent_title, sizeof(g_state.agent_title), title);
    }
    if (status) {
        copy_text(g_state.agent_status, sizeof(g_state.agent_status), status);
    }
    g_state.dirty = true;
    portEXIT_CRITICAL(&s_ui_state_mux);
}

void aura_ui_set_agent_visible(bool visible)
{
    portENTER_CRITICAL(&s_ui_state_mux);
    g_state.agent_panel_visible = visible;
    g_state.dirty = true;
    portEXIT_CRITICAL(&s_ui_state_mux);
}

void aura_ui_set_ws_connected(bool connected)
{
    portENTER_CRITICAL(&s_ui_state_mux);
    g_state.ws_connected = connected;
    g_state.dirty = true;
    portEXIT_CRITICAL(&s_ui_state_mux);
}

bool aura_ui_copy_and_clear_dirty(aura_state_t *snapshot)
{
    bool should_draw = false;
    if (!snapshot) return false;

    portENTER_CRITICAL(&s_ui_state_mux);
    if (g_state.dirty) {
        memcpy(snapshot, &g_state, sizeof(*snapshot));
        g_state.dirty = false;
        should_draw = true;
    }
    portEXIT_CRITICAL(&s_ui_state_mux);
    return should_draw;
}

bool aura_ui_display_tick(bool hold_dialogue, int page_ticks, aura_state_t *snapshot)
{
    bool should_draw = false;
    if (!snapshot) return false;

    portENTER_CRITICAL(&s_ui_state_mux);

    if (g_state.ui_mode == AURA_UI_LISTENING) {
        clear_dialogue_locked();
        clear_agent_panel_locked();
        g_state.dirty = true;
    }

    if (g_state.display_text[0] != '\0' &&
        g_state.text_char_index < (int)strlen(g_state.display_text)) {
        g_state.text_char_index += 2;
        g_state.dirty = true;
    }
    (void)page_ticks;

    if (g_state.dialogue_ticks_left > 0) {
        if (!hold_dialogue) {
            g_state.dialogue_ticks_left--;
        }
        if (g_state.dialogue_ticks_left == 0 &&
            g_state.ui_mode != AURA_UI_LISTENING &&
            g_state.ui_mode != AURA_UI_PROCESSING) {
            clear_dialogue_locked();
            g_state.dirty = true;
        }
    }

    if (g_state.ui_mode != AURA_UI_IDLE || g_state.agent_panel_visible) {
        g_state.ui_anim_tick++;
        g_state.dirty = true;
    }

    if (g_state.dirty) {
        memcpy(snapshot, &g_state, sizeof(*snapshot));
        g_state.dirty = false;
        should_draw = true;
    }

    portEXIT_CRITICAL(&s_ui_state_mux);
    return should_draw;
}
