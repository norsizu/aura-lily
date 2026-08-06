/**
 * 按键检测 — 去抖 + 长按短按识别
 */
#include "buttons.h"
#include "aura_config.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "buttons";

#define DEBOUNCE_MS    50
#define LONG_PRESS_MS  1000

typedef struct {
    gpio_num_t pin;
    int64_t press_time;
    bool pressed;
    bool handled;
} btn_state_t;

static btn_state_t s_key = {0};
static btn_state_t s_boot = {0};

esp_err_t buttons_init(void)
{
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << BTN_KEY_PIN) | (1ULL << BTN_BOOT_PIN),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);

    s_key.pin = BTN_KEY_PIN;
    s_boot.pin = BTN_BOOT_PIN;

    ESP_LOGI(TAG, "Buttons initialized");
    return ESP_OK;
}

static button_event_t check_button(btn_state_t *btn, button_event_t short_evt, button_event_t long_evt)
{
    int level = gpio_get_level(btn->pin);
    int64_t now = esp_timer_get_time() / 1000;  // ms

    if (level == 0 && !btn->pressed) {
        // 按下
        btn->pressed = true;
        btn->press_time = now;
        btn->handled = false;
        ESP_LOGI(TAG, "DIAG button press pin=%d short_evt=%d long_evt=%d",
                 (int)btn->pin, short_evt, long_evt);
    } else if (level == 0 && btn->pressed && !btn->handled) {
        // 持续按住
        if (now - btn->press_time > LONG_PRESS_MS) {
            btn->handled = true;
            ESP_LOGI(TAG, "DIAG button long pin=%d event=%d held_ms=%lld",
                     (int)btn->pin, long_evt, (long long)(now - btn->press_time));
            return long_evt;
        }
    } else if (level == 1 && btn->pressed) {
        // 释放
        btn->pressed = false;
        ESP_LOGI(TAG, "DIAG button release pin=%d held_ms=%lld handled=%d",
                 (int)btn->pin, (long long)(now - btn->press_time), btn->handled ? 1 : 0);
        if (!btn->handled && (now - btn->press_time > DEBOUNCE_MS)) {
            ESP_LOGI(TAG, "DIAG button short pin=%d event=%d",
                     (int)btn->pin, short_evt);
            return short_evt;
        }
    }

    return BTN_EVENT_NONE;
}

button_event_t buttons_poll_key(void)
{
    return check_button(&s_key, BTN_EVENT_KEY_SHORT, BTN_EVENT_KEY_LONG);
}

button_event_t buttons_poll_boot(void)
{
    if (BTN_KEY_PIN == BTN_BOOT_PIN) {
        return BTN_EVENT_NONE;
    }
    return check_button(&s_boot, BTN_EVENT_BOOT_SHORT, BTN_EVENT_BOOT_LONG);
}

button_event_t buttons_poll(void)
{
    button_event_t evt = buttons_poll_key();
    if (evt != BTN_EVENT_NONE) return evt;

    evt = buttons_poll_boot();
    if (evt != BTN_EVENT_NONE) return evt;

    return BTN_EVENT_NONE;
}
