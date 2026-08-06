/**
 * PCF85063 RTC 实时时钟
 */
#pragma once
#include "esp_err.h"
#include <stdint.h>

typedef struct {
    uint8_t second;
    uint8_t minute;
    uint8_t hour;
    uint8_t day;
    uint8_t weekday;
    uint8_t month;
    uint16_t year;
} pcf85063_time_t;

esp_err_t pcf85063_init(void);
esp_err_t pcf85063_get_time(pcf85063_time_t *time);
esp_err_t pcf85063_set_time(const pcf85063_time_t *time);
