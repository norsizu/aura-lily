/**
 * 按键处理
 */
#pragma once
#include "esp_err.h"

typedef enum {
    BTN_EVENT_NONE = 0,
    BTN_EVENT_KEY_SHORT,
    BTN_EVENT_KEY_LONG,
    BTN_EVENT_BOOT_SHORT,
    BTN_EVENT_BOOT_LONG,
} button_event_t;

esp_err_t buttons_init(void);
button_event_t buttons_poll(void);
button_event_t buttons_poll_key(void);
button_event_t buttons_poll_boot(void);
