/**
 * ES8311 音频编解码驱动
 */
#pragma once
#include <stdbool.h>
#include "esp_err.h"

esp_err_t es8311_prepare_pa_muted(void);
esp_err_t es8311_init(void);
esp_err_t es8311_set_volume(int volume);  // 0-100
esp_err_t es8311_mute(bool mute);
