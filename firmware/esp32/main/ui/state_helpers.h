#pragma once

#include <stdbool.h>
#include "renderer.h"

void aura_ui_mark_dirty(void);
void aura_ui_enter_listening(int pose, int mic_level);
bool aura_ui_ensure_listening(int pose, int min_mic_level);
void aura_ui_set_dialogue(const char *text, int ttl_ticks);
void aura_ui_clear_dialogue(void);
void aura_ui_set_dialogue_progress(int progress_permille);
void aura_ui_limit_dialogue_ttl(int ttl_ticks);
void aura_ui_set_agent_panel(bool visible, int progress,
                             const char *title, const char *status);
void aura_ui_set_agent_visible(bool visible);
void aura_ui_set_ws_connected(bool connected);
bool aura_ui_display_tick(bool hold_dialogue, int page_ticks, aura_state_t *snapshot);
bool aura_ui_copy_and_clear_dirty(aura_state_t *snapshot);
