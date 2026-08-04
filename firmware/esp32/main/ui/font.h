/**
 * font.h — 嵌入式位图字体 (5×7 小字 / 8×12 大字 / 16×16 中文 / 图标)
 * 用于 ST7305 400×300 1-bit 墨水屏
 */
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

/* ── 5×7 小字体 (ASCII 32-122) ── */
void font_draw_char(uint8_t *buf, int buf_w, int x, int y, char c, uint8_t color);
void font_draw_string(uint8_t *buf, int buf_w, int x, int y, const char *str, uint8_t color);
int  font_string_width(const char *str);

/* ── 5×7 字体 2x 放大 (10×14 实际像素) ── */
void font_draw_char_2x(uint8_t *buf, int buf_w, int x, int y, char c, uint8_t color);
void font_draw_string_2x(uint8_t *buf, int buf_w, int x, int y, const char *str, uint8_t color);
int  font_string_width_2x(const char *str);

/* ── 8×12 大字体 (数字 0-9 + :.-° ) ── */
void font_draw_char_large(uint8_t *buf, int buf_w, int x, int y, char c, uint8_t color);
void font_draw_string_large(uint8_t *buf, int buf_w, int x, int y, const char *str, uint8_t color);
int  font_string_width_large(const char *str);

/* ── 16×16 中文字体 (GB2312 + ASCII) ── */
esp_err_t font_cn16_init(void);        /* 从 SPIFFS 加载字体文件 */
void font_draw_utf8(uint8_t *buf, int buf_w, int x, int y, const char *utf8_str, uint8_t color);
int  font_utf8_width(const char *utf8_str);

/* ── 图标 ── */
void font_draw_wifi(uint8_t *buf, int buf_w, int x, int y, int level, uint8_t color);
void font_draw_heart(uint8_t *buf, int buf_w, int x, int y, uint8_t color);
void font_draw_star(uint8_t *buf, int buf_w, int x, int y, uint8_t color);
void font_draw_bowl(uint8_t *buf, int buf_w, int x, int y, uint8_t color);
void font_draw_bean(uint8_t *buf, int buf_w, int x, int y, uint8_t color);
void font_draw_crown(uint8_t *buf, int buf_w, int x, int y, uint8_t color);
void font_draw_cp_icon(uint8_t *buf, int buf_w, int x, int y, uint8_t color);
void font_draw_smile(uint8_t *buf, int buf_w, int x, int y, uint8_t color);
void font_draw_bolt(uint8_t *buf, int buf_w, int x, int y, uint8_t color);
void font_draw_fork_knife(uint8_t *buf, int buf_w, int x, int y, uint8_t color);
void font_draw_clock(uint8_t *buf, int buf_w, int x, int y, uint8_t color);
void font_draw_calendar(uint8_t *buf, int buf_w, int x, int y, uint8_t color);
void font_draw_mic(uint8_t *buf, int buf_w, int x, int y, uint8_t color);
void font_draw_sparkle(uint8_t *buf, int buf_w, int x, int y, uint8_t color);
