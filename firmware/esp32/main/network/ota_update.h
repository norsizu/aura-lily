#pragma once

#include <stdbool.h>

#include "esp_err.h"

typedef enum {
    AURA_OTA_CHECKING = 0,
    AURA_OTA_DOWNLOADING_RESOURCES,
    AURA_OTA_DOWNLOADING_APP,
    AURA_OTA_VERIFYING,
    AURA_OTA_UP_TO_DATE,
    AURA_OTA_RESTARTING,
    AURA_OTA_FAILED,
} aura_ota_status_t;

typedef void (*aura_ota_status_cb_t)(aura_ota_status_t status, int progress, void *ctx);

bool ota_update_is_running(void);
esp_err_t ota_update_start(aura_ota_status_cb_t callback, void *ctx);
esp_err_t ota_update_start_resources(aura_ota_status_cb_t callback, void *ctx);
/* Runs on the caller's existing task stack. Use this on memory-constrained
 * builds to avoid allocating another internal-RAM task before TLS starts. */
esp_err_t ota_update_run_blocking(aura_ota_status_cb_t callback, void *ctx,
                                  bool resources_only);

/* Call after SPIFFS is mounted, before any assets are opened. */
void ota_update_recover_resources(void);

/* Call only after the display, audio pipeline, and network have stayed healthy. */
esp_err_t ota_update_confirm_running_app(void);
bool ota_update_running_app_pending_verify(void);
