/**
 * ST7305 RLCD 驱动 — 4.2" 反射式 LCD (400x300, 1-bit) SPI 接口
 * Waveshare ESP32-S3-RLCD-4.2
 */
#pragma once
#include <stdbool.h>
#include "esp_err.h"
#include <stdint.h>

esp_err_t rlcd_init(void);
void rlcd_flush(const uint8_t *framebuffer);
void rlcd_flush_partial(const uint8_t *data, int x, int y, int w, int h);
void rlcd_clear(uint8_t color);  // 0x00=black, 0xFF=white
void rlcd_set_pixel(uint8_t *framebuffer, int x, int y, bool black);
void rlcd_sleep(void);
void rlcd_wake(void);
