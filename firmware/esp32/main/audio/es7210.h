/**
 * ES7210 ADC 驱动 — 双麦克风回声消除
 */
#pragma once
#include "esp_err.h"

esp_err_t es7210_init(void);
esp_err_t es7210_set_gain(int gain_db);
