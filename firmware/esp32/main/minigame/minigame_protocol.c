/**
 * minigame_protocol.c — WS protocol stub (Phase 1: offline/hardcoded)
 */
#include "minigame_protocol.h"

esp_err_t mg_protocol_init(void)
{
    /* Phase 1: no network traffic — all events are hardcoded locally */
    return ESP_OK;
}
