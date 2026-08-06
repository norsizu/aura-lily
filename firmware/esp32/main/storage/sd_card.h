/**
 * SD 卡管理
 */
#pragma once
#include <stdbool.h>
#include "esp_err.h"
#include "sdmmc_cmd.h"

esp_err_t sd_card_init(void);
bool sd_card_is_mounted(void);
sdmmc_card_t *sd_card_get_card(void);
esp_err_t sd_card_unmount(void);
