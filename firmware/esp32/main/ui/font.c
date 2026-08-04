/**
 * 嵌入式位图字体 — 5×7 小字 + 8×12 大字 + 图标
 * 经典 CP437 风格，纯黑/白，适合 1-bit 墨水屏
 */
#include "font.h"
#include <string.h>

/* ═══════════════════════════════════════════════════
 *  5×7 小字体 (每字符 5 列，每列 7 bit，LSB=top)
 * ═══════════════════════════════════════════════════ */
static const uint8_t font5x7[][5] = {
    // ASCII 32 (space)
    [0]  = {0x00,0x00,0x00,0x00,0x00},
    // ! 33
    [1]  = {0x00,0x00,0x5F,0x00,0x00},
    // " 34
    [2]  = {0x00,0x07,0x00,0x07,0x00},
    // # 35
    [3]  = {0x14,0x7F,0x14,0x7F,0x14},
    // $ 36
    [4]  = {0x24,0x2A,0x7F,0x2A,0x12},
    // % 37
    [5]  = {0x23,0x13,0x08,0x64,0x62},
    // & 38
    [6]  = {0x36,0x49,0x55,0x22,0x50},
    // ' 39
    [7]  = {0x00,0x05,0x03,0x00,0x00},
    // ( 40
    [8]  = {0x00,0x1C,0x22,0x41,0x00},
    // ) 41
    [9]  = {0x00,0x41,0x22,0x1C,0x00},
    // * 42
    [10] = {0x14,0x08,0x3E,0x08,0x14},
    // + 43
    [11] = {0x08,0x08,0x3E,0x08,0x08},
    // , 44
    [12] = {0x00,0x50,0x30,0x00,0x00},
    // - 45
    [13] = {0x08,0x08,0x08,0x08,0x08},
    // . 46
    [14] = {0x00,0x60,0x60,0x00,0x00},
    // / 47
    [15] = {0x20,0x10,0x08,0x04,0x02},
    // 0-9 (ASCII 48-57)
    [16] = {0x3E,0x51,0x49,0x45,0x3E}, // 0
    [17] = {0x00,0x42,0x7F,0x40,0x00}, // 1
    [18] = {0x42,0x61,0x51,0x49,0x46}, // 2
    [19] = {0x21,0x41,0x45,0x4B,0x31}, // 3
    [20] = {0x18,0x14,0x12,0x7F,0x10}, // 4
    [21] = {0x27,0x45,0x45,0x45,0x39}, // 5
    [22] = {0x3C,0x4A,0x49,0x49,0x30}, // 6
    [23] = {0x01,0x71,0x09,0x05,0x03}, // 7
    [24] = {0x36,0x49,0x49,0x49,0x36}, // 8
    [25] = {0x06,0x49,0x49,0x29,0x1E}, // 9
    // : 58
    [26] = {0x00,0x36,0x36,0x00,0x00},
    // ; 59
    [27] = {0x00,0x56,0x36,0x00,0x00},
    // < 60
    [28] = {0x08,0x14,0x22,0x41,0x00},
    // = 61
    [29] = {0x14,0x14,0x14,0x14,0x14},
    // > 62
    [30] = {0x00,0x41,0x22,0x14,0x08},
    // ? 63
    [31] = {0x02,0x01,0x51,0x09,0x06},
    // @ 64
    [32] = {0x32,0x49,0x79,0x41,0x3E},
    // A-Z (ASCII 65-90)
    [33] = {0x7E,0x11,0x11,0x11,0x7E}, // A
    [34] = {0x7F,0x49,0x49,0x49,0x36}, // B
    [35] = {0x3E,0x41,0x41,0x41,0x22}, // C
    [36] = {0x7F,0x41,0x41,0x22,0x1C}, // D
    [37] = {0x7F,0x49,0x49,0x49,0x41}, // E
    [38] = {0x7F,0x09,0x09,0x09,0x01}, // F
    [39] = {0x3E,0x41,0x49,0x49,0x7A}, // G
    [40] = {0x7F,0x08,0x08,0x08,0x7F}, // H
    [41] = {0x00,0x41,0x7F,0x41,0x00}, // I
    [42] = {0x20,0x40,0x41,0x3F,0x01}, // J
    [43] = {0x7F,0x08,0x14,0x22,0x41}, // K
    [44] = {0x7F,0x40,0x40,0x40,0x40}, // L
    [45] = {0x7F,0x02,0x0C,0x02,0x7F}, // M
    [46] = {0x7F,0x04,0x08,0x10,0x7F}, // N
    [47] = {0x3E,0x41,0x41,0x41,0x3E}, // O
    [48] = {0x7F,0x09,0x09,0x09,0x06}, // P
    [49] = {0x3E,0x41,0x51,0x21,0x5E}, // Q
    [50] = {0x7F,0x09,0x19,0x29,0x46}, // R
    [51] = {0x46,0x49,0x49,0x49,0x31}, // S
    [52] = {0x01,0x01,0x7F,0x01,0x01}, // T
    [53] = {0x3F,0x40,0x40,0x40,0x3F}, // U
    [54] = {0x1F,0x20,0x40,0x20,0x1F}, // V
    [55] = {0x3F,0x40,0x38,0x40,0x3F}, // W
    [56] = {0x63,0x14,0x08,0x14,0x63}, // X
    [57] = {0x07,0x08,0x70,0x08,0x07}, // Y
    [58] = {0x61,0x51,0x49,0x45,0x43}, // Z
    // [ 91
    [59] = {0x00,0x7F,0x41,0x41,0x00},
    // \ 92
    [60] = {0x02,0x04,0x08,0x10,0x20},
    // ] 93
    [61] = {0x00,0x41,0x41,0x7F,0x00},
    // ^ 94
    [62] = {0x04,0x02,0x01,0x02,0x04},
    // _ 95
    [63] = {0x40,0x40,0x40,0x40,0x40},
    // ` 96
    [64] = {0x00,0x01,0x02,0x04,0x00},
    // a-z (ASCII 97-122)
    [65] = {0x20,0x54,0x54,0x54,0x78}, // a
    [66] = {0x7F,0x48,0x44,0x44,0x38}, // b
    [67] = {0x38,0x44,0x44,0x44,0x20}, // c
    [68] = {0x38,0x44,0x44,0x48,0x7F}, // d
    [69] = {0x38,0x54,0x54,0x54,0x18}, // e
    [70] = {0x08,0x7E,0x09,0x01,0x02}, // f
    [71] = {0x0C,0x52,0x52,0x52,0x3E}, // g
    [72] = {0x7F,0x08,0x04,0x04,0x78}, // h
    [73] = {0x00,0x44,0x7D,0x40,0x00}, // i
    [74] = {0x20,0x40,0x44,0x3D,0x00}, // j
    [75] = {0x7F,0x10,0x28,0x44,0x00}, // k
    [76] = {0x00,0x41,0x7F,0x40,0x00}, // l
    [77] = {0x7C,0x04,0x18,0x04,0x78}, // m
    [78] = {0x7C,0x08,0x04,0x04,0x78}, // n
    [79] = {0x38,0x44,0x44,0x44,0x38}, // o
    [80] = {0x7C,0x14,0x14,0x14,0x08}, // p
    [81] = {0x08,0x14,0x14,0x18,0x7C}, // q
    [82] = {0x7C,0x08,0x04,0x04,0x08}, // r
    [83] = {0x48,0x54,0x54,0x54,0x20}, // s
    [84] = {0x04,0x3F,0x44,0x40,0x20}, // t
    [85] = {0x3C,0x40,0x40,0x20,0x7C}, // u
    [86] = {0x1C,0x20,0x40,0x20,0x1C}, // v
    [87] = {0x3C,0x40,0x30,0x40,0x3C}, // w
    [88] = {0x44,0x28,0x10,0x28,0x44}, // x
    [89] = {0x0C,0x50,0x50,0x50,0x3C}, // y
    [90] = {0x44,0x64,0x54,0x4C,0x44}, // z
};

#define FONT5X7_COUNT 91

/* ASCII → 索引映射 */
static int char_to_idx(char c)
{
    if (c >= 32 && c <= 96)  return c - 32;
    if (c >= 'a' && c <= 'z') return 65 + (c - 'a');
    return 0; // fallback: space
}

void font_draw_char(uint8_t *buf, int buf_w, int x, int y, char c, uint8_t color)
{
    int idx = char_to_idx(c);
    if (idx < 0 || idx >= FONT5X7_COUNT) idx = 0;
    const uint8_t *glyph = font5x7[idx];
    for (int col = 0; col < 5; col++) {
        uint8_t bits = glyph[col];
        for (int row = 0; row < 7; row++) {
            if (bits & (1 << row)) {
                int px = x + col;
                int py = y + row;
                if (px >= 0 && px < buf_w && py >= 0) {
                    buf[py * buf_w + px] = color;
                }
            }
        }
    }
}

void font_draw_string(uint8_t *buf, int buf_w, int x, int y, const char *str, uint8_t color)
{
    while (*str) {
        font_draw_char(buf, buf_w, x, y, *str, color);
        x += 6; // 5px char + 1px gap
        str++;
    }
}

int font_string_width(const char *str)
{
    int len = (int)strlen(str);
    if (len == 0) return 0;
    return len * 6 - 1; // last char has no trailing gap
}

/* ═══════════════════════════════════════════════════
 *  5×7 字体 2x 放大 (10×14 实际像素)
 * ═══════════════════════════════════════════════════ */
void font_draw_char_2x(uint8_t *buf, int buf_w, int x, int y, char c, uint8_t color)
{
    int idx = char_to_idx(c);
    if (idx < 0 || idx >= FONT5X7_COUNT) idx = 0;
    const uint8_t *glyph = font5x7[idx];
    for (int col = 0; col < 5; col++) {
        uint8_t bits = glyph[col];
        for (int row = 0; row < 7; row++) {
            if (bits & (1 << row)) {
                // 每个原始像素 → 2×2 块
                for (int dy = 0; dy < 2; dy++) {
                    for (int dx = 0; dx < 2; dx++) {
                        int px = x + col * 2 + dx;
                        int py = y + row * 2 + dy;
                        if (px >= 0 && px < buf_w && py >= 0) {
                            buf[py * buf_w + px] = color;
                        }
                    }
                }
            }
        }
    }
}

void font_draw_string_2x(uint8_t *buf, int buf_w, int x, int y, const char *str, uint8_t color)
{
    while (*str) {
        font_draw_char_2x(buf, buf_w, x, y, *str, color);
        x += 12; // 10px char + 2px gap
        str++;
    }
}

int font_string_width_2x(const char *str)
{
    int len = (int)strlen(str);
    if (len == 0) return 0;
    return len * 12 - 2;
}

/* ═══════════════════════════════════════════════════
 *  8×12 大字体 (数字 + 常用符号，适合时钟显示)
 *  每字符 8 列，每列 12 bit (低12位有效)，LSB=top
 * ═══════════════════════════════════════════════════ */
static const uint16_t font8x12_digits[][8] = {
    // 0
    {0x1FC,0x302,0x402,0x482,0x442,0x422,0x302,0x1FC},
    // 1
    {0x000,0x204,0x202,0x7FE,0x200,0x200,0x000,0x000},
    // 2
    {0x604,0x502,0x482,0x442,0x422,0x412,0x20C,0x000},
    // 3
    {0x104,0x202,0x422,0x422,0x422,0x422,0x39C,0x000},
    // 4
    {0x060,0x050,0x048,0x044,0x042,0x7FE,0x040,0x000},
    // 5
    {0x11E,0x212,0x412,0x412,0x412,0x412,0x3E2,0x000},
    // 6
    {0x1FC,0x222,0x412,0x412,0x412,0x412,0x3E4,0x000},
    // 7
    {0x002,0x002,0x702,0x0C2,0x032,0x00A,0x006,0x000},
    // 8
    {0x39C,0x422,0x422,0x422,0x422,0x422,0x39C,0x000},
    // 9
    {0x13C,0x242,0x442,0x442,0x442,0x222,0x1FC,0x000},
};

// : (colon) for clock
static const uint16_t font8x12_colon[8] =
    {0x000,0x000,0x108,0x108,0x000,0x000,0x000,0x000};

// . (period)
static const uint16_t font8x12_period[8] =
    {0x000,0x000,0x600,0x600,0x000,0x000,0x000,0x000};

// ° (degree)
static const uint16_t font8x12_degree[8] =
    {0x00C,0x012,0x012,0x00C,0x000,0x000,0x000,0x000};

// - (minus)
static const uint16_t font8x12_minus[8] =
    {0x040,0x040,0x040,0x040,0x040,0x000,0x000,0x000};

// space
static const uint16_t font8x12_space[8] =
    {0x000,0x000,0x000,0x000,0x000,0x000,0x000,0x000};

static const uint16_t *large_char_data(char c)
{
    if (c >= '0' && c <= '9') return font8x12_digits[c - '0'];
    if (c == ':') return font8x12_colon;
    if (c == '.') return font8x12_period;
    if (c == '-') return font8x12_minus;
    // 用 * 代表 ° 度
    if (c == '*') return font8x12_degree;
    return font8x12_space;
}

void font_draw_char_large(uint8_t *buf, int buf_w, int x, int y, char c, uint8_t color)
{
    const uint16_t *glyph = large_char_data(c);
    for (int col = 0; col < 8; col++) {
        uint16_t bits = glyph[col];
        for (int row = 0; row < 12; row++) {
            if (bits & (1 << row)) {
                int px = x + col;
                int py = y + row;
                if (px >= 0 && px < buf_w && py >= 0) {
                    buf[py * buf_w + px] = color;
                }
            }
        }
    }
}

void font_draw_string_large(uint8_t *buf, int buf_w, int x, int y, const char *str, uint8_t color)
{
    while (*str) {
        font_draw_char_large(buf, buf_w, x, y, *str, color);
        x += 9; // 8px char + 1px gap
        str++;
    }
}

int font_string_width_large(const char *str)
{
    int len = (int)strlen(str);
    if (len == 0) return 0;
    return len * 9 - 1;
}

/* ═══════════════════════════════════════════════════
 *  16×16 中文点阵字体 (GB2312 一级汉字 + ASCII 8×16)
 *  文件格式: CNFONT header + unicode index + cn bitmaps + ascii bitmaps
 * ═══════════════════════════════════════════════════ */
#include <stdio.h>
#include <stdlib.h>
#include "esp_log.h"
#include "esp_err.h"
#include "esp_heap_caps.h"

static const char *FONT_TAG = "font_cn16";

/* 字体文件内存映射 */
static uint16_t s_cn_count = 0;
static const uint16_t *s_cn_index = NULL;   /* Unicode 码点索引 (sorted) */
static const uint8_t  *s_cn_bitmap = NULL;  /* 中文点阵 32B each */
static const uint8_t  *s_ascii_bitmap = NULL; /* ASCII 点阵 16B each */
static uint8_t *s_font_data = NULL;         /* 整个文件 */

esp_err_t font_cn16_init(void)
{
    FILE *f = fopen("/spiffs/font_cn16.bin", "rb");
    if (!f) {
        ESP_LOGW(FONT_TAG, "Font file not found: /assets/font_cn16.bin");
        return ESP_ERR_NOT_FOUND;
    }

    fseek(f, 0, SEEK_END);
    long fsize = ftell(f);
    fseek(f, 0, SEEK_SET);

    s_font_data = heap_caps_malloc(fsize, MALLOC_CAP_SPIRAM);
    if (!s_font_data) {
        ESP_LOGE(FONT_TAG, "No memory for font (%ld bytes)", fsize);
        fclose(f);
        return ESP_ERR_NO_MEM;
    }

    fread(s_font_data, 1, fsize, f);
    fclose(f);

    /* Parse header: "CNFONT\0\0" + u16 cn_count + u16 ascii_start + u16 ascii_end + u16 reserved */
    if (memcmp(s_font_data, "CNFONT", 6) != 0) {
        ESP_LOGE(FONT_TAG, "Invalid font file magic");
        free(s_font_data);
        s_font_data = NULL;
        return ESP_ERR_INVALID_ARG;
    }

    s_cn_count = *(uint16_t *)(s_font_data + 8);
    /* uint16_t ascii_start = *(uint16_t *)(s_font_data + 10); */
    /* uint16_t ascii_end   = *(uint16_t *)(s_font_data + 12); */

    size_t hdr_size = 16;
    s_cn_index  = (const uint16_t *)(s_font_data + hdr_size);
    s_cn_bitmap = s_font_data + hdr_size + s_cn_count * 2;
    s_ascii_bitmap = s_cn_bitmap + s_cn_count * 32;

    ESP_LOGI(FONT_TAG, "Loaded: %d CJK chars + ASCII, %ld bytes", s_cn_count, fsize);
    return ESP_OK;
}

/* 二分查找 Unicode 码点 */
static int cn_find(uint16_t codepoint)
{
    if (!s_cn_index || s_cn_count == 0) return -1;
    int lo = 0, hi = s_cn_count - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        if (s_cn_index[mid] == codepoint) return mid;
        if (s_cn_index[mid] < codepoint) lo = mid + 1;
        else hi = mid - 1;
    }
    return -1;
}

/* 画一个 16×16 中文字符 */
static void cn16_draw(uint8_t *buf, int buf_w, int x, int y, int idx, uint8_t color)
{
    const uint8_t *bmp = s_cn_bitmap + idx * 32;
    for (int row = 0; row < 16; row++) {
        uint16_t val = ((uint16_t)bmp[row * 2] << 8) | bmp[row * 2 + 1];
        for (int col = 0; col < 16; col++) {
            if (val & (1 << (15 - col))) {
                int px = x + col;
                int py = y + row;
                if (px >= 0 && px < buf_w && py >= 0) {
                    buf[py * buf_w + px] = color;
                }
            }
        }
    }
}

static void draw_pixel(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    if (x >= 0 && x < buf_w && y >= 0) {
        buf[y * buf_w + x] = color;
    }
}

static void missing16_draw(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    for (int i = 2; i <= 13; i++) {
        draw_pixel(buf, buf_w, x + i, y + 2, color);
        draw_pixel(buf, buf_w, x + i, y + 13, color);
        draw_pixel(buf, buf_w, x + 2, y + i, color);
        draw_pixel(buf, buf_w, x + 13, y + i, color);
    }
}

static bool punctuation16_draw(uint8_t *buf, int buf_w, int x, int y,
                               uint32_t cp, uint8_t color)
{
    switch (cp) {
    case 0xFF0C: /* ， */
        draw_pixel(buf, buf_w, x + 7, y + 10, color);
        draw_pixel(buf, buf_w, x + 8, y + 10, color);
        draw_pixel(buf, buf_w, x + 7, y + 11, color);
        draw_pixel(buf, buf_w, x + 8, y + 11, color);
        draw_pixel(buf, buf_w, x + 7, y + 12, color);
        draw_pixel(buf, buf_w, x + 6, y + 13, color);
        return true;
    case 0x3002: /* 。 */
        draw_pixel(buf, buf_w, x + 6, y + 10, color);
        draw_pixel(buf, buf_w, x + 7, y + 10, color);
        draw_pixel(buf, buf_w, x + 8, y + 10, color);
        draw_pixel(buf, buf_w, x + 6, y + 11, color);
        draw_pixel(buf, buf_w, x + 8, y + 11, color);
        draw_pixel(buf, buf_w, x + 6, y + 12, color);
        draw_pixel(buf, buf_w, x + 7, y + 12, color);
        draw_pixel(buf, buf_w, x + 8, y + 12, color);
        return true;
    case 0x3001: /* 、 */
        draw_pixel(buf, buf_w, x + 7, y + 8, color);
        draw_pixel(buf, buf_w, x + 8, y + 9, color);
        draw_pixel(buf, buf_w, x + 8, y + 10, color);
        draw_pixel(buf, buf_w, x + 9, y + 11, color);
        draw_pixel(buf, buf_w, x + 10, y + 12, color);
        return true;
    default:
        return false;
    }
}

/* 画一个 8×16 ASCII 字符 */
static void ascii16_draw(uint8_t *buf, int buf_w, int x, int y, uint8_t ch, uint8_t color)
{
    if (!s_ascii_bitmap || ch < 32 || ch > 126) return;
    const uint8_t *bmp = s_ascii_bitmap + (ch - 32) * 16;
    for (int row = 0; row < 16; row++) {
        uint8_t val = bmp[row];
        for (int col = 0; col < 8; col++) {
            if (val & (1 << (7 - col))) {
                int px = x + col;
                int py = y + row;
                if (px >= 0 && px < buf_w && py >= 0) {
                    buf[py * buf_w + px] = color;
                }
            }
        }
    }
}

/* UTF-8 解码: 返回 Unicode 码点, 更新 *bytes_consumed */
static uint32_t utf8_decode(const char *s, int *bytes_consumed)
{
    uint8_t c = (uint8_t)s[0];
    if (c < 0x80) {
        *bytes_consumed = 1;
        return c;
    } else if ((c & 0xE0) == 0xC0) {
        *bytes_consumed = 2;
        return ((c & 0x1F) << 6) | (s[1] & 0x3F);
    } else if ((c & 0xF0) == 0xE0) {
        *bytes_consumed = 3;
        return ((c & 0x0F) << 12) | ((s[1] & 0x3F) << 6) | (s[2] & 0x3F);
    } else if ((c & 0xF8) == 0xF0) {
        *bytes_consumed = 4;
        return ((c & 0x07) << 18) | ((s[1] & 0x3F) << 12) | ((s[2] & 0x3F) << 6) | (s[3] & 0x3F);
    }
    *bytes_consumed = 1;
    return '?';
}

void font_draw_utf8(uint8_t *buf, int buf_w, int x, int y, const char *utf8_str, uint8_t color)
{
    int cx = x;
    while (*utf8_str) {
        int consumed = 0;
        uint32_t cp = utf8_decode(utf8_str, &consumed);

        if (cp < 0x80) {
            /* ASCII: 8×16 如果字体已加载, 否则用 5×7 2x */
            if (s_ascii_bitmap) {
                ascii16_draw(buf, buf_w, cx, y, (uint8_t)cp, color);
                cx += 9;  /* 8px + 1px gap */
            } else {
                font_draw_char_2x(buf, buf_w, cx, y + 1, (char)cp, color);
                cx += 12;
            }
        } else {
            /* CJK: 16×16 */
            int idx = cn_find((uint16_t)cp);
            if (punctuation16_draw(buf, buf_w, cx, y, cp, color)) {
                /* Draw common dialogue punctuation with readable low-res shapes. */
            } else if (idx >= 0) {
                cn16_draw(buf, buf_w, cx, y, idx, color);
            } else {
                missing16_draw(buf, buf_w, cx, y, color);
            }
            cx += 17;  /* 16px + 1px gap */
        }
        utf8_str += consumed;
    }
}

int font_utf8_width(const char *utf8_str)
{
    int w = 0;
    while (*utf8_str) {
        int consumed = 0;
        uint32_t cp = utf8_decode(utf8_str, &consumed);
        if (cp < 0x80) {
            w += 9;
        } else {
            w += 17;
        }
        utf8_str += consumed;
    }
    return w > 0 ? w - 1 : 0;  /* remove trailing gap */
}

/* ═══════════════════════════════════════════════════
 *  图标
 * ═══════════════════════════════════════════════════ */

/* WiFi 信号格 📶 — 4格递增高度方块, level 0-4; level 0 = 无信号 */
void font_draw_wifi(uint8_t *buf, int buf_w, int x, int y, int level, uint8_t color)
{
    /* 4 根柱子, 宽2px, 间隔1px, 高度从左到右递增: 2,4,6,8 */
    /* 总宽 = 4*2 + 3*1 = 11px, 总高 = 8px                    */
    static const int bar_h[4] = {2, 4, 6, 8};
    if (level < 0) level = 0;
    if (level > 4) level = 4;

    for (int i = 0; i < 4; i++) {
        int bx = x + i * 3;
        int h  = bar_h[i];
        int by = y + 8 - h;
        if (level > 0 && i < level) {
            /* 实心 */
            for (int dy = 0; dy < h; dy++)
                for (int dx = 0; dx < 2; dx++) {
                    int px = bx + dx, py = by + dy;
                    if (px >= 0 && px < buf_w && py >= 0)
                        buf[py * buf_w + px] = color;
                }
        } else {
            /* 空心: 只画底部 1px 表示空格位 */
            for (int dx = 0; dx < 2; dx++) {
                int px = bx + dx, py = y + 7;
                if (px >= 0 && px < buf_w && py >= 0)
                    buf[py * buf_w + px] = color;
            }
        }
    }

    if (level == 0) {
        /* 断连态：在空柱子上加一条斜杠，避免被误看成仍有信号 */
        for (int step = 0; step < 9; step++) {
            int px = x + 10 - step;
            int py = y + step;
            if (px >= 0 && px < buf_w && py >= 0) {
                buf[py * buf_w + px] = color;
            }
        }
    }
}

/* ♥ 心形 7×6 */
void font_draw_heart(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    static const uint8_t heart[] = {
        0b0110110,
        0b1111111,
        0b1111111,
        0b0111110,
        0b0011100,
        0b0001000,
    };
    for (int row = 0; row < 6; row++) {
        for (int col = 0; col < 7; col++) {
            if (heart[row] & (1 << (6 - col))) {
                buf[(y+row)*buf_w + x+col] = color;
            }
        }
    }
}

/* ★ 星形 7×7 */
void font_draw_star(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    static const uint8_t star[] = {
        0b0001000,
        0b0011100,
        0b1111111,
        0b0111110,
        0b0011100,
        0b0100010,
        0b1000001,
    };
    for (int row = 0; row < 7; row++) {
        for (int col = 0; col < 7; col++) {
            if (star[row] & (1 << (6 - col))) {
                buf[(y+row)*buf_w + x+col] = color;
            }
        }
    }
}

/* 餐碗 / 饱腹 图标 7×7 */
void font_draw_bowl(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    static const uint8_t bowl[] = {
        0b0010100,
        0b0001000,
        0b0000000,
        0b1000001,
        0b1100011,
        0b0111110,
        0b0011100,
    };
    for (int row = 0; row < 7; row++) {
        for (int col = 0; col < 7; col++) {
            if (bowl[row] & (1 << (6 - col))) {
                buf[(y + row) * buf_w + x + col] = color;
            }
        }
    }
}

/* 豆子 / Beans 图标 7×7 */
void font_draw_bean(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    static const uint8_t bean[] = {
        0b0001100,
        0b0011110,
        0b0110111,
        0b0110011,
        0b1110011,
        0b0111110,
        0b0011100,
    };
    for (int row = 0; row < 7; row++) {
        for (int col = 0; col < 7; col++) {
            if (bean[row] & (1 << (6 - col))) {
                buf[(y + row) * buf_w + x + col] = color;
            }
        }
    }
}

/* 皇冠（等级） 图标 7×7 */
void font_draw_crown(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    static const uint8_t crown[] = {
        0b0100010,
        0b1110111,
        0b1111111,
        0b1111111,
        0b0111110,
        0b0111110,
        0b0000000,
    };
    for (int row = 0; row < 7; row++) {
        for (int col = 0; col < 7; col++) {
            if (crown[row] & (1 << (6 - col))) {
                buf[(y + row) * buf_w + x + col] = color;
            }
        }
    }
}

/* CP (好感) 图标（四叶草/连花） 7×7 */
void font_draw_cp_icon(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    static const uint8_t cp_icon[] = {
        0b0010100,
        0b0111110,
        0b1101011,
        0b0111110,
        0b1101011,
        0b0111110,
        0b0010100,
    };
    for (int row = 0; row < 7; row++) {
        for (int col = 0; col < 7; col++) {
            if (cp_icon[row] & (1 << (6 - col))) {
                buf[(y + row) * buf_w + x + col] = color;
            }
        }
    }
}

/* 7x7 微笑/Mood 图标 */
void font_draw_smile(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    static const uint8_t bmp[] = {
        0b0011100,
        0b0100010,
        0b1010101,
        0b1000001,
        0b1011101,
        0b0100010,
        0b0011100,
    };
    for (int row = 0; row < 7; row++) {
        for (int col = 0; col < 7; col++) {
            if (bmp[row] & (1 << (6 - col))) buf[(y + row) * buf_w + x + col] = color;
        }
    }
}

/* 7x7 闪电/Energy 图标 */
void font_draw_bolt(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    static const uint8_t bmp[] = {
        0b0001100,
        0b0011000,
        0b0111110,
        0b0001100,
        0b0011000,
        0b0010000,
        0b0100000,
    };
    for (int row = 0; row < 7; row++) {
        for (int col = 0; col < 7; col++) {
            if (bmp[row] & (1 << (6 - col))) buf[(y + row) * buf_w + x + col] = color;
        }
    }
}

/* 7x7 刀叉/Satiety 图标 */
void font_draw_fork_knife(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    static const uint8_t bmp[] = {
        0b1010010,
        0b1010010,
        0b1110010,
        0b0100010,
        0b0100010,
        0b0100010,
        0b0100010,
    };
    for (int row = 0; row < 7; row++) {
        for (int col = 0; col < 7; col++) {
            if (bmp[row] & (1 << (6 - col))) buf[(y + row) * buf_w + x + col] = color;
        }
    }
}

/* 7x7 时钟/Clock 图标 */
void font_draw_clock(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    static const uint8_t bmp[] = {
        0b0011100,
        0b0100010,
        0b1001001,
        0b1001101,
        0b1000001,
        0b0100010,
        0b0011100,
    };
    for (int row = 0; row < 7; row++) {
        for (int col = 0; col < 7; col++) {
            if (bmp[row] & (1 << (6 - col))) buf[(y + row) * buf_w + x + col] = color;
        }
    }
}

/* 7x7 日历/Calendar 图标 */
void font_draw_calendar(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    static const uint8_t bmp[] = {
        0b1010101,
        0b1111111,
        0b1000001,
        0b1010101,
        0b1000001,
        0b1111111,
        0b0000000,
    };
    for (int row = 0; row < 7; row++) {
        for (int col = 0; col < 7; col++) {
            if (bmp[row] & (1 << (6 - col))) buf[(y + row) * buf_w + x + col] = color;
        }
    }
}

/* 9x11 大麦克风/Mic 图标 */
void font_draw_mic(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    static const uint16_t bmp[] = {
        0b000111000,
        0b001111100,
        0b001111100,
        0b001111100,
        0b000111000,
        0b010000010,
        0b010000010,
        0b001111100,
        0b000010000,
        0b000010000,
        0b001111100,
    };
    for (int row = 0; row < 11; row++) {
        for (int col = 0; col < 9; col++) {
            if (bmp[row] & (1 << (8 - col))) {
                buf[(y + row) * buf_w + x + col] = color;
            }
        }
    }
}

/* 7x7 闪光/Sparkle 图标 */
void font_draw_sparkle(uint8_t *buf, int buf_w, int x, int y, uint8_t color)
{
    static const uint8_t bmp[] = {
        0b0001000,
        0b0001000,
        0b0001000,
        0b1111111,
        0b0001000,
        0b0001000,
        0b0001000,
    };
    for (int row = 0; row < 7; row++) {
        for (int col = 0; col < 7; col++) {
            if (bmp[row] & (1 << (6 - col))) buf[(y + row) * buf_w + x + col] = color;
        }
    }
}
