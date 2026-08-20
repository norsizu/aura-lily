/** RLCD 4.2 battery monitor. */
#pragma once

#include <stdbool.h>
#include "esp_err.h"

esp_err_t battery_init(void);
esp_err_t battery_read(int *percent, bool *valid);
int battery_percent_from_mv(int battery_mv);
