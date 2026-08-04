/**
 * minigame_protocol.h — WS protocol stub (Phase 1: offline/hardcoded)
 */
#pragma once
#include "esp_err.h"

/** Phase 1: no-op.  Future: send game events to the Hermes gateway. */
esp_err_t mg_protocol_init(void);
