/**
 * SHTC3 温湿度传感器
 */
#pragma once
#include "esp_err.h"

esp_err_t shtc3_init(void);
esp_err_t shtc3_read(float *temperature, float *humidity);
