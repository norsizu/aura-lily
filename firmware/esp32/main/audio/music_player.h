#pragma once

#include <stdbool.h>
#include "esp_err.h"

esp_err_t music_player_init(void);
void music_player_loop(void);
esp_err_t music_player_request_play(bool wait_for_tts_window);
esp_err_t music_player_request_stop(void);
esp_err_t music_player_request_next(void);
esp_err_t music_player_toggle_pause(void);
esp_err_t music_player_pause_for_interaction(void);
esp_err_t music_player_resume_after_interaction(void);
bool music_player_is_active(void);
bool music_player_is_paused(void);
const char *music_player_current_track(void);
