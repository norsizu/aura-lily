/**
 * UI 面板系统 — GB RPG 风格
 * 左侧属性块 + 工作场景 + 录音胶囊 + 对话框(带头像)
 */
#include "panels.h"
#include "font.h"
#include "aura_config.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include <string.h>
#include <stdio.h>
#include <stdbool.h>
#include <stdlib.h>

#define UI_WHITE 255
#define UI_BLACK 0

static const char *TAG = "panels";

typedef struct {
    uint8_t *data;
    int frame_count;
    int frame_w;
    int frame_h;
    bool loaded;
    bool tried;
} mini_sprite_t;

static mini_sprite_t s_work_sprite = {0};
static mini_sprite_t s_talk_sprite = {0};

/* 前向声明 */
static int clamp_i(int value, int lo, int hi);
static void fill_rect(uint8_t *buf, int buf_w, int buf_h, int x, int y, int w, int h, uint8_t color);
static void stroke_rect(uint8_t *buf, int buf_w, int buf_h, int x, int y, int w, int h, uint8_t color);
static void draw_rounded_dark_box(uint8_t *buf, int buf_w, int buf_h, int x, int y, int w, int h, int r);
static int beans_progress_pct(int beans);

/* ── 头像 28×28 灰度 (legacy, kept for fallback) ── */
static uint8_t *s_avatar_data = NULL;
static int s_avatar_w = 0, s_avatar_h = 0;
static bool s_avatar_loaded = false;
static bool s_avatar_tried = false;

static void load_avatar(void)
{
    if (s_avatar_loaded || s_avatar_tried) return;
    s_avatar_tried = true;
    FILE *f = fopen("/spiffs/avatar.bin", "rb");
    if (!f) { ESP_LOGW(TAG, "Avatar not found"); return; }
    uint16_t w = 0, h = 0;
    if (fread(&w, 2, 1, f) != 1 || fread(&h, 2, 1, f) != 1) { fclose(f); return; }
    size_t sz = (size_t)w * h;
    s_avatar_data = heap_caps_malloc(sz, MALLOC_CAP_SPIRAM);
    if (!s_avatar_data) { fclose(f); return; }
    if (fread(s_avatar_data, 1, sz, f) != sz) {
        fclose(f); heap_caps_free(s_avatar_data); s_avatar_data = NULL; return;
    }
    fclose(f);
    s_avatar_w = w; s_avatar_h = h;
    s_avatar_loaded = true;
    ESP_LOGI(TAG, "Avatar loaded: %dx%d", w, h);
}

/* ── Outfit face portrait (9 outfits × 115×115 grayscale) ──
 * face_115.bin 按 outfit 索引排列（0睡衣 1洋装 2睡裙 3休闲1 4休闲2
 * 5冬装 6旗袍 7马面裙 8汉服），对话框头像跟随当前穿着。 */
#define FACE_SIZE 115
#define FACE_COUNT 9

static uint8_t *s_face_data = NULL;
static bool s_face_loaded = false;
static bool s_face_tried = false;

static void load_face(void)
{
    if (s_face_loaded || s_face_tried) return;
    s_face_tried = true;
    FILE *f = fopen("/spiffs/face_115.bin", "rb");
    if (!f) { ESP_LOGW(TAG, "face_115.bin not found"); return; }
    size_t total = FACE_COUNT * FACE_SIZE * FACE_SIZE;
    s_face_data = heap_caps_malloc(total, MALLOC_CAP_SPIRAM);
    if (!s_face_data) { fclose(f); return; }
    if (fread(s_face_data, 1, total, f) != total) {
        fclose(f); heap_caps_free(s_face_data); s_face_data = NULL; return;
    }
    fclose(f);
    s_face_loaded = true;
    ESP_LOGI(TAG, "Face portraits loaded: %d × %dx%d", FACE_COUNT, FACE_SIZE, FACE_SIZE);
}

/* ── Emoji portrait system (9 emotions × 48×48 grayscale) ── */
#define EMOJI_SIZE 48
#define EMOJI_COUNT 9

static uint8_t *s_emoji_data = NULL;
static bool s_emoji_loaded = false;
static bool s_emoji_tried = false;

static void load_emoji(void)
{
    if (s_emoji_loaded || s_emoji_tried) return;
    s_emoji_tried = true;
    FILE *f = fopen("/spiffs/emoji_48.bin", "rb");
    if (!f) { ESP_LOGW(TAG, "emoji_48.bin not found"); return; }
    size_t total = EMOJI_COUNT * EMOJI_SIZE * EMOJI_SIZE;
    s_emoji_data = heap_caps_malloc(total, MALLOC_CAP_SPIRAM);
    if (!s_emoji_data) { fclose(f); return; }
    if (fread(s_emoji_data, 1, total, f) != total) {
        fclose(f); heap_caps_free(s_emoji_data); s_emoji_data = NULL; return;
    }
    fclose(f);
    s_emoji_loaded = true;
    ESP_LOGI(TAG, "Emoji sprites loaded: %d × %dx%d", EMOJI_COUNT, EMOJI_SIZE, EMOJI_SIZE);
}

/* Map emotion string → index (0-8), default 0 (neutral) */
static int emotion_to_index(const char *emotion)
{
    if (!emotion || emotion[0] == '\0') return 0;
    static const char *names[] = {
        "neutral", "proud", "thinking",
        "surprised", "apologetic", "assertive",
        "shy", "excited", "relaxed"
    };
    for (int i = 0; i < EMOJI_COUNT; i++) {
        if (strcmp(emotion, names[i]) == 0) return i;
    }
    return 0;  /* fallback: neutral */
}

/* Draw emoji portrait at (x,y), size×size pixels, from emotion index */
static void draw_emoji(uint8_t *buf, int buf_w, int buf_h,
                       int x, int y, int size, int emotion_idx)
{
    load_emoji();
    if (!s_emoji_loaded || !s_emoji_data) {
        /* Fallback: draw old avatar or blank box */
        load_avatar();
        if (s_avatar_loaded && s_avatar_data) {
            for (int dy = 0; dy < size && dy < s_avatar_h; dy++)
                for (int dx = 0; dx < size && dx < s_avatar_w; dx++) {
                    int px = x + dx, py = y + dy;
                    if (px >= 0 && px < buf_w && py >= 0 && py < buf_h)
                        buf[py * buf_w + px] = s_avatar_data[dy * s_avatar_w + dx];
                }
        } else {
            fill_rect(buf, buf_w, buf_h, x, y, size, size, UI_WHITE);
        }
        stroke_rect(buf, buf_w, buf_h, x, y, size, size, UI_WHITE);
        return;
    }

    emotion_idx = clamp_i(emotion_idx, 0, EMOJI_COUNT - 1);
    const uint8_t *src = s_emoji_data + emotion_idx * EMOJI_SIZE * EMOJI_SIZE;

    /* Draw with scaling if size != EMOJI_SIZE */
    for (int dy = 0; dy < size; dy++) {
        int src_y = dy * EMOJI_SIZE / size;
        for (int dx = 0; dx < size; dx++) {
            int src_x = dx * EMOJI_SIZE / size;
            int px = x + dx, py = y + dy;
            if (px >= 0 && px < buf_w && py >= 0 && py < buf_h) {
                buf[py * buf_w + px] = src[src_y * EMOJI_SIZE + src_x];
            }
        }
    }
    /* Thin white border around portrait */
    stroke_rect(buf, buf_w, buf_h, x - 1, y - 1, size + 2, size + 2, UI_WHITE);
}

/* Draw outfit face portrait at (x,y) scaled to size×size; falls back to
 * the emotion emoji when the face sheet is missing. */
static void draw_face(uint8_t *buf, int buf_w, int buf_h,
                      int x, int y, int size, int outfit_idx, int emotion_idx)
{
    load_face();
    if (!s_face_loaded || !s_face_data) {
        draw_emoji(buf, buf_w, buf_h, x, y, size, emotion_idx);
        return;
    }
    outfit_idx = clamp_i(outfit_idx, 0, FACE_COUNT - 1);
    const uint8_t *src = s_face_data + (size_t)outfit_idx * FACE_SIZE * FACE_SIZE;
    for (int dy = 0; dy < size; dy++) {
        int src_y = dy * FACE_SIZE / size;
        for (int dx = 0; dx < size; dx++) {
            int src_x = dx * FACE_SIZE / size;
            int px = x + dx, py = y + dy;
            if (px >= 0 && px < buf_w && py >= 0 && py < buf_h) {
                buf[py * buf_w + px] = src[src_y * FACE_SIZE + src_x];
            }
        }
    }
    stroke_rect(buf, buf_w, buf_h, x - 1, y - 1, size + 2, size + 2, UI_WHITE);
}

static int clamp_i(int value, int lo, int hi)
{
    if (value < lo) return lo;
    if (value > hi) return hi;
    return value;
}

static int beans_progress_pct(int beans)
{
    int clamped = clamp_i(beans, 0, AURA_BEANS_MAX);
    return (clamped * 100 + (AURA_BEANS_MAX / 2)) / AURA_BEANS_MAX;
}

static int affinity_progress_pct(const aura_state_t *state)
{
    static const int thresholds[] = {0, 50, 150, 400, 1000, 1000};
    int level = clamp_i(state->affinity_level, 1, 5);
    int xp = state->affinity;
    int current_floor = thresholds[level - 1];
    int next_floor = thresholds[level];

    if (level >= 5) return 100;

    if (xp <= current_floor) return 0;
    if (xp >= next_floor) return 100;

    int span = next_floor - current_floor;
    if (span <= 0) return 0;
    return clamp_i(((xp - current_floor) * 100) / span, 0, 100);
}

static void fill_rect(uint8_t *buf, int buf_w, int buf_h,
                      int x, int y, int w, int h, uint8_t color)
{
    if (w <= 0 || h <= 0) return;
    int x0 = clamp_i(x, 0, buf_w);
    int y0 = clamp_i(y, 0, buf_h);
    int x1 = clamp_i(x + w, 0, buf_w);
    int y1 = clamp_i(y + h, 0, buf_h);
    for (int py = y0; py < y1; py++)
        for (int px = x0; px < x1; px++)
            buf[py * buf_w + px] = color;
}

static void stroke_rect(uint8_t *buf, int buf_w, int buf_h,
                        int x, int y, int w, int h, uint8_t color)
{
    if (w < 2 || h < 2) return;
    fill_rect(buf, buf_w, buf_h, x, y, w, 1, color);
    fill_rect(buf, buf_w, buf_h, x, y + h - 1, w, 1, color);
    fill_rect(buf, buf_w, buf_h, x, y, 1, h, color);
    fill_rect(buf, buf_w, buf_h, x + w - 1, y, 1, h, color);
}

static void draw_light_box(uint8_t *buf, int buf_w, int buf_h,
                           int x, int y, int w, int h)
{
    fill_rect(buf, buf_w, buf_h, x, y, w, h, UI_WHITE);
    stroke_rect(buf, buf_w, buf_h, x, y, w, h, UI_BLACK);
}

static void draw_dark_box(uint8_t *buf, int buf_w, int buf_h,
                          int x, int y, int w, int h)
{
    fill_rect(buf, buf_w, buf_h, x, y, w, h, UI_BLACK);
    stroke_rect(buf, buf_w, buf_h, x, y, w, h, UI_WHITE);
    if (w >= 6 && h >= 6)
        stroke_rect(buf, buf_w, buf_h, x + 2, y + 2, w - 4, h - 4, UI_WHITE);
}

static void draw_progress_bar(uint8_t *buf, int buf_w, int buf_h,
                              int x, int y, int w, int pct)
{
    int fill_w = (w - 4) * clamp_i(pct, 0, 100) / 100;
    fill_rect(buf, buf_w, buf_h, x, y, w, 6, UI_WHITE);
    fill_rect(buf, buf_w, buf_h, x + 1, y + 1, w - 2, 4, UI_BLACK);
    fill_rect(buf, buf_w, buf_h, x + 2, y + 2, fill_w, 2, UI_WHITE);
}

static void draw_cut_corner_box(uint8_t *buf, int buf_w, int buf_h,
                                int x, int y, int w, int h, int cut)
{
    fill_rect(buf, buf_w, buf_h, x, y + cut, w, h - 2 * cut, UI_WHITE);
    fill_rect(buf, buf_w, buf_h, x + cut, y, w - 2 * cut, h, UI_WHITE);

    stroke_rect(buf, buf_w, buf_h, x, y + cut, w, h - 2 * cut, UI_BLACK);
    stroke_rect(buf, buf_w, buf_h, x + cut, y, w - 2 * cut, h, UI_BLACK);

    for (int i = 0; i < cut; i++) {
        fill_rect(buf, buf_w, buf_h, x + cut - 1 - i, y + i, 1, 1, UI_BLACK);
        fill_rect(buf, buf_w, buf_h, x + w - cut + i, y + i, 1, 1, UI_BLACK);
        fill_rect(buf, buf_w, buf_h, x + cut - 1 - i, y + h - 1 - i, 1, 1, UI_BLACK);
        fill_rect(buf, buf_w, buf_h, x + w - cut + i, y + h - 1 - i, 1, 1, UI_BLACK);
    }
}

static void draw_cut_corner_modal(uint8_t *buf, int buf_w, int buf_h,
                                  int x, int y, int w, int h, int cut)
{
    fill_rect(buf, buf_w, buf_h, x, y + cut, w, h - 2 * cut, UI_BLACK);
    fill_rect(buf, buf_w, buf_h, x + cut, y, w - 2 * cut, h, UI_BLACK);

    stroke_rect(buf, buf_w, buf_h, x, y + cut, w, h - 2 * cut, UI_WHITE);
    stroke_rect(buf, buf_w, buf_h, x + cut, y, w - 2 * cut, h, UI_WHITE);

    for (int i = 0; i < cut; i++) {
        fill_rect(buf, buf_w, buf_h, x + cut - 1 - i, y + i, 1, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + w - cut + i, y + i, 1, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + cut - 1 - i, y + h - 1 - i, 1, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + w - cut + i, y + h - 1 - i, 1, 1, UI_WHITE);
    }
}

static void draw_dotted_rule(uint8_t *buf, int buf_w, int buf_h,
                             int x, int y, int w, uint8_t color)
{
    for (int dx = 0; dx < w; dx += 4) {
        fill_rect(buf, buf_w, buf_h, x + dx, y, 2, 1, color);
    }
}

static void draw_segmented_bar(uint8_t *buf, int buf_w, int buf_h,
                               int x, int y, int w, int h, int pct, int segments)
{
    int filled = (segments * clamp_i(pct, 0, 100) + 99) / 100;
    if (segments < 1) segments = 1;
    draw_light_box(buf, buf_w, buf_h, x, y, w, h);
    int inner_x = x + 2;
    int inner_y = y + 2;
    int inner_w = w - 4;
    int inner_h = h - 4;
    int gap = 1;
    int seg_w = (inner_w - gap * (segments - 1)) / segments;
    if (seg_w < 2) seg_w = 2;
    for (int i = 0; i < segments; i++) {
        int sx = inner_x + i * (seg_w + gap);
        int sw = seg_w;
        if (sx + sw > x + w - 2) sw = (x + w - 2) - sx;
        if (sw <= 0) break;
        if (i < filled) {
            fill_rect(buf, buf_w, buf_h, sx, inner_y, sw, inner_h, UI_BLACK);
        } else {
            fill_rect(buf, buf_w, buf_h, sx, inner_y + inner_h / 2, sw, 1, UI_BLACK);
        }
    }
}

static void draw_wave_bars(uint8_t *buf, int buf_w, int buf_h,
                           int cx, int cy, int amp, uint8_t color, bool invert_phase)
{
    int max_h = 14;
    for (int i = 0; i < 3; i++) {
        int h = (amp * (3 - i)) / 3;
        if (invert_phase) h = (amp * (i + 1)) / 3;
        if (h > max_h) h = max_h;
        if (h < 2) h = 2;
        fill_rect(buf, buf_w, buf_h, cx + i * 6, cy - h / 2, 4, h, color);
    }
}

static int recording_meter_width(void)
{
    /* 21 bars × (3px + 2px gap) - trailing gap = 103px */
    return 21 * (3 + 2) - 2;
}

static void draw_recording_meter(uint8_t *buf, int buf_w, int buf_h,
                                 int x, int y, int amp, int tick)
{
    static const float weights[] = {
        0.18f, 0.24f, 0.30f, 0.36f, 0.42f, 0.50f, 0.58f, 0.68f, 0.78f, 0.88f,
        1.00f,
        0.88f, 0.78f, 0.68f, 0.58f, 0.50f, 0.42f, 0.36f, 0.30f, 0.24f, 0.18f
    };
    static float envelope = 0.0f;
    const int segments = (int)(sizeof(weights) / sizeof(weights[0]));
    const int bar_w = 3;
    const int gap = 2;
    const int max_h = 16;
    const int min_h = 4;
    float target = (float)clamp_i(amp, 0, 100) / 100.0f;

    if (tick <= 1) {
        envelope = target;
    } else if (target > envelope) {
        envelope += (target - envelope) * 0.40f;
    } else {
        envelope += (target - envelope) * 0.15f;
    }

    envelope = (envelope < 0.0f) ? 0.0f : ((envelope > 1.0f) ? 1.0f : envelope);

    int total_h = min_h + max_h;
    for (int i = 0; i < segments; i++) {
        uint32_t seed = (uint32_t)(tick * 1103515245u + (i + 1) * 12345u);
        float jitter = (((seed >> 16) & 0xFFu) / 255.0f) * 0.06f - 0.03f;
        float level = (0.12f + envelope * 1.85f) * weights[i] * (1.0f + jitter);
        int h = min_h + (int)(level * (float)max_h + 0.5f);
        int sx = x + i * (bar_w + gap);
        h = clamp_i(h, min_h, total_h);
        fill_rect(buf, buf_w, buf_h, sx, y + (total_h - h) / 2, bar_w, h, UI_WHITE);
    }
}

static void draw_large_mic(uint8_t *buf, int buf_w, int buf_h, int x, int y, uint8_t color)
{
    /*
     * 13×18 pixel-art microphone, rendered at 2x = 26×36 px.
     * Capsule head (rows 0-8) with grille lines,
     * U-shaped holder (rows 9-11), stem (rows 12-15), base (rows 16-17).
     */
    static const uint16_t bmp[] = {
        /* head top */
        0b0000111110000,  /* row  0: rounded cap      */
        0b0001111111000,  /* row  1                    */
        0b0011101011100,  /* row  2: grille line 1     */
        0b0011111111100,  /* row  3                    */
        0b0011101011100,  /* row  4: grille line 2     */
        0b0011111111100,  /* row  5                    */
        0b0011101011100,  /* row  6: grille line 3     */
        0b0001111111000,  /* row  7                    */
        0b0000111110000,  /* row  8: rounded bottom    */
        /* holder */
        0b0100000000010,  /* row  9: U-holder arms     */
        0b0100000000010,  /* row 10                    */
        0b0010000000100,  /* row 11                    */
        0b0001111111000,  /* row 12: holder bottom     */
        /* stem */
        0b0000001000000,  /* row 13                    */
        0b0000001000000,  /* row 14                    */
        /* base */
        0b0000111110000,  /* row 15                    */
        0b0001111111000,  /* row 16                    */
    };
    const int rows = (int)(sizeof(bmp) / sizeof(bmp[0]));
    for (int row = 0; row < rows; row++) {
        for (int col = 0; col < 13; col++) {
            if (bmp[row] & (1 << (12 - col))) {
                int px = x + col * 2;
                int py = y + row * 2;
                fill_rect(buf, buf_w, buf_h, px, py, 2, 2, color);
            }
        }
    }
}

static void draw_centered_utf8(uint8_t *buf, int buf_w,
                               int box_x, int box_w, int y,
                               const char *text, uint8_t color)
{
    int tw = font_utf8_width(text);
    int tx = box_x + (box_w - tw) / 2;
    font_draw_utf8(buf, buf_w, tx, y, text, color);
}

static void draw_centered_ascii(uint8_t *buf, int buf_w,
                                int box_x, int box_w, int y,
                                const char *text, uint8_t color)
{
    int tw = font_string_width(text);
    int tx = box_x + (box_w - tw) / 2;
    font_draw_string(buf, buf_w, tx, y, text, color);
}

static void utf8_char_metrics(const char *p, int *clen, int *char_w)
{
    uint8_t c = (uint8_t)*p;
    *clen = 1;
    *char_w = 9;
    if ((c & 0xF8) == 0xF0) {
        *clen = 4;
        *char_w = 17;
    } else if ((c & 0xF0) == 0xE0) {
        *clen = 3;
        *char_w = 17;
    } else if ((c & 0xE0) == 0xC0) {
        *clen = 2;
        *char_w = 17;
    }
}

static int wrapped_utf8_page_count(const char *text, int x, int max_x, int max_lines)
{
    if (!text || !*text || max_lines <= 0) return 1;
    int tx = x;
    int line = 0;
    int pages = 1;
    const char *p = text;
    while (*p) {
        int clen = 1, char_w = 9;
        utf8_char_metrics(p, &clen, &char_w);
        if (tx + char_w > max_x) {
            tx = x;
            line++;
            if (line >= max_lines) {
                pages++;
                line = 0;
            }
        }
        tx += char_w;
        p += clen;
    }
    return pages;
}

static void draw_wrapped_utf8_page(uint8_t *buf, int buf_w, int x, int y,
                                   int max_x, int max_lines, const char *text,
                                   int page_index, uint8_t color)
{
    int tx = x;
    int line = 0;
    int page = 0;
    const char *p = text;
    while (*p) {
        int clen = 1, char_w = 9;
        utf8_char_metrics(p, &clen, &char_w);
        if (tx + char_w > max_x) {
            tx = x;
            line++;
            if (line >= max_lines) {
                page++;
                line = 0;
                if (page > page_index) break;
            }
        }
        if (page == page_index) {
            char tmp[5] = {0};
            memcpy(tmp, p, clen);
            font_draw_utf8(buf, buf_w, tx, y + line * 16, tmp, color);
        }
        tx += char_w;
        p += clen;
    }
}

/* ── 7x7 心形（实心） ── */
static const uint8_t HEART_FILLED[7][7] = {
    {0,1,1,0,1,1,0},
    {1,1,1,1,1,1,1},
    {1,1,1,1,1,1,1},
    {1,1,1,1,1,1,1},
    {0,1,1,1,1,1,0},
    {0,0,1,1,1,0,0},
    {0,0,0,1,0,0,0},
};
/* ── 7x7 心形（空心） ── */
static const uint8_t HEART_OUTLINE[7][7] = {
    {0,1,1,0,1,1,0},
    {1,0,0,1,0,0,1},
    {1,0,0,0,0,0,1},
    {1,0,0,0,0,0,1},
    {0,1,0,0,0,1,0},
    {0,0,1,0,1,0,0},
    {0,0,0,1,0,0,0},
};

static void draw_heart_7(uint8_t *buf, int buf_w, int buf_h,
                         int x, int y, bool filled, uint8_t color)
{
    const uint8_t (*bmp)[7] = filled ? HEART_FILLED : HEART_OUTLINE;
    for (int dy = 0; dy < 7; dy++)
        for (int dx = 0; dx < 7; dx++)
            if (bmp[dy][dx]) {
                int px = x + dx, py = y + dy;
                if (px >= 0 && px < buf_w && py >= 0 && py < buf_h)
                    buf[py * buf_w + px] = color;
            }
}

static void load_mini_sprite(const char *path, mini_sprite_t *sprite)
{
    if (sprite->loaded || sprite->tried) return;
    sprite->tried = true;
    FILE *f = fopen(path, "rb");
    if (!f) { ESP_LOGW(TAG, "Sprite not found: %s", path); return; }
    uint32_t count = 0, w = 0, h = 0;
    if (fread(&count, 4, 1, f) != 1 || fread(&w, 4, 1, f) != 1 || fread(&h, 4, 1, f) != 1) {
        fclose(f); return;
    }
    size_t sz = (size_t)count * w * h;
    sprite->data = heap_caps_malloc(sz, MALLOC_CAP_SPIRAM);
    if (!sprite->data) { fclose(f); return; }
    if (fread(sprite->data, 1, sz, f) != sz) {
        fclose(f); heap_caps_free(sprite->data); sprite->data = NULL; return;
    }
    fclose(f);
    sprite->frame_count = (int)count;
    sprite->frame_w = (int)w;
    sprite->frame_h = (int)h;
    sprite->loaded = true;
    ESP_LOGI(TAG, "Sprite loaded: %s (%dx%d x %d)", path, (int)w, (int)h, (int)count);
}

static void draw_sprite_in_box(uint8_t *buf, int buf_w, int buf_h,
                               int x, int y, int w, int h,
                               const aura_state_t *state, bool invert)
{
    mini_sprite_t *sprite = (state->ui_mode == AURA_UI_SPEAKING)
        ? &s_talk_sprite : &s_work_sprite;
    load_mini_sprite("/spiffs/sprites/work_sprite.bin", &s_work_sprite);
    load_mini_sprite("/spiffs/sprites/talk_sprite.bin", &s_talk_sprite);

    if (!sprite->loaded || !sprite->data || sprite->frame_count <= 0) {
        const char *working = state->ui_language == 1 ? "Working" :
                              state->ui_language == 2 ? "処理中" : "工作中";
        font_draw_utf8(buf, buf_w, x + (w - font_utf8_width(working)) / 2,
                       y + h/2 - 8, working,
                       invert ? UI_WHITE : UI_BLACK);
        return;
    }
    int frame = (state->ui_anim_tick / 2) % sprite->frame_count;
    int ox = x + (w - sprite->frame_w) / 2;
    int oy = y + (h - sprite->frame_h) / 2;
    const uint8_t *src = sprite->data + frame * sprite->frame_w * sprite->frame_h;
    for (int py = 0; py < sprite->frame_h; py++)
        for (int px = 0; px < sprite->frame_w; px++) {
            uint8_t val = src[py * sprite->frame_w + px];
            int dx = ox + px, dy = oy + py;
            if (val == 0xFF) continue;
            if (dx < 0 || dx >= buf_w || dy < 0 || dy >= buf_h) continue;
            if (invert) {
                buf[dy * buf_w + dx] = (val < 180) ? UI_WHITE : UI_BLACK;
            } else {
                buf[dy * buf_w + dx] = val;
            }
        }
}

/* ================================================================== */
/*  左面板 — 属性小块                                                  */
/* ================================================================== */
void panels_draw_left(uint8_t *graybuf, int width, int height,
                      const aura_state_t *state)
{
    /*
     * Left status panel — clean, compact, same design language.
     *
     * ┌──────────────────────────┐
     * │ ▌▌▌ STATUS ▌▌▌   L2     │  dark title bar
     * │                          │
     * │  ☺  ████████░░░░  100   │  each stat: icon + fill bar + value
     * │  ⚡  ██████░░░░░░   89   │
     * │  YI  █████░░░░░░░   85   │
     * │  ◆  █████████░░░  141   │
     * │  · · · · · · · · · · ·  │  dotted rule
     * │  ⏱  ░░░░░░░░░░░░    0%  │
     * │  📅  ██████████░░  100%  │
     * └──────────────────────────┘
     *
     * 100×136, 4px from left, STATUS_BAR_HEIGHT+6 from top
     */
    int bx = BLOCK_MARGIN;
    int by = STATUS_BAR_HEIGHT + BLOCK_MARGIN + 2;
    int bw = 100;
    int panel_h = 136;
    char val[24];
    bool ready = state->companion_state_ready;

    /* ── Panel background: single thin border ── */
    int r = 3;
    fill_rect(graybuf, width, height, bx + r, by, bw - 2 * r, panel_h, UI_WHITE);
    fill_rect(graybuf, width, height, bx, by + r, bw, panel_h - 2 * r, UI_WHITE);
    fill_rect(graybuf, width, height, bx + 1, by + 1, r, r, UI_WHITE);
    fill_rect(graybuf, width, height, bx + bw - r - 1, by + 1, r, r, UI_WHITE);
    fill_rect(graybuf, width, height, bx + 1, by + panel_h - r - 1, r, r, UI_WHITE);
    fill_rect(graybuf, width, height, bx + bw - r - 1, by + panel_h - r - 1, r, r, UI_WHITE);
    /* Border */
    fill_rect(graybuf, width, height, bx + r, by, bw - 2 * r, 1, UI_BLACK);
    fill_rect(graybuf, width, height, bx + r, by + panel_h - 1, bw - 2 * r, 1, UI_BLACK);
    fill_rect(graybuf, width, height, bx, by + r, 1, panel_h - 2 * r, UI_BLACK);
    fill_rect(graybuf, width, height, bx + bw - 1, by + r, 1, panel_h - 2 * r, UI_BLACK);
    /* Corner pixels */
    fill_rect(graybuf, width, height, bx + 1, by + r - 1, 1, 1, UI_BLACK);
    fill_rect(graybuf, width, height, bx + r - 1, by + 1, 1, 1, UI_BLACK);
    fill_rect(graybuf, width, height, bx + bw - 2, by + r - 1, 1, 1, UI_BLACK);
    fill_rect(graybuf, width, height, bx + bw - r, by + 1, 1, 1, UI_BLACK);
    fill_rect(graybuf, width, height, bx + 1, by + panel_h - r, 1, 1, UI_BLACK);
    fill_rect(graybuf, width, height, bx + r - 1, by + panel_h - 2, 1, 1, UI_BLACK);
    fill_rect(graybuf, width, height, bx + bw - 2, by + panel_h - r, 1, 1, UI_BLACK);
    fill_rect(graybuf, width, height, bx + bw - r, by + panel_h - 2, 1, 1, UI_BLACK);

    /* ── Dark title bar ── */
    fill_rect(graybuf, width, height, bx + 2, by + 2, bw - 4, 12, UI_BLACK);
    draw_centered_ascii(graybuf, width, bx, bw - 22, by + 5, "STATUS", UI_WHITE);
    if (ready) {
        snprintf(val, sizeof(val), "L%d", state->affinity_level);
    } else {
        snprintf(val, sizeof(val), "--");
    }
    font_draw_string(graybuf, width, bx + bw - 20, by + 5, val, UI_WHITE);

    /* ── Stat rows ── */
    int icon_x = bx + 6;
    int bar_x  = bx + 18;
    int bar_w  = 44;
    int val_x  = bx + 66;
    int iy = by + 20;
    const int row_h = 15;

    /* Helper macro for each stat row */
    #define DRAW_STAT_ROW(draw_icon_fn, value, pct) do { \
        draw_icon_fn(graybuf, width, icon_x, iy, UI_BLACK); \
        draw_progress_bar(graybuf, width, height, bar_x, iy, bar_w, ready ? clamp_i(pct, 0, 100) : 0); \
        snprintf(val, sizeof(val), "%d", ready ? (value) : 0); \
        font_draw_string(graybuf, width, val_x, iy, ready ? val : "--", UI_BLACK); \
    } while(0)

    /* 1. Mood */
    DRAW_STAT_ROW(font_draw_smile, state->mood, state->mood);
    iy += row_h;

    /* 2. Energy */
    DRAW_STAT_ROW(font_draw_bolt, state->energy, state->energy);
    iy += row_h;

    /* 3. Satiety */
    DRAW_STAT_ROW(font_draw_fork_knife, state->satiety, state->satiety);
    iy += row_h;

    /* 4. Beans */
    font_draw_bean(graybuf, width, icon_x, iy, UI_BLACK);
    draw_progress_bar(graybuf, width, height, bar_x, iy, bar_w, ready ? beans_progress_pct(state->coins) : 0);
    snprintf(val, sizeof(val), "%d", ready ? clamp_i(state->coins, 0, AURA_BEANS_MAX) : 0);
    font_draw_string(graybuf, width, val_x, iy, ready ? val : "--", UI_BLACK);

    #undef DRAW_STAT_ROW

    /* ── Dotted rule ── */
    iy += row_h + 2;
    draw_dotted_rule(graybuf, width, height, bx + 6, iy, bw - 12, UI_BLACK);

    /* ── Quota rows (current token plan + real remaining windows) ── */
    /* Keep both quota bars inside the 136px status panel.  The previous
     * spacing put the second bar at y=157..162, outside the panel and into
     * the scene, which appeared as a fixed dark line on the display. */
    iy += 4;
    const char *quota_title =
        (state->quota_ready && state->quota_headline[0]) ? state->quota_headline :
        (state->quota_ready && state->quota_provider[0]) ? state->quota_provider :
        "TOKEN";
    const char *quota_primary_text =
        (state->quota_ready && state->quota_primary_text[0]) ? state->quota_primary_text :
        (state->quota_ready && state->quota_text[0]) ? state->quota_text :
        "--";
    const char *quota_secondary_text =
        (state->quota_ready && state->quota_secondary_text[0]) ? state->quota_secondary_text :
        (state->quota_ready && state->quota_secondary_label[0]) ? state->quota_secondary_label :
        "--";
    int quota_primary_pct = state->quota_ready ? clamp_i(state->quota_primary_percent, 0, 100) : 0;
    int quota_secondary_pct = state->quota_ready ? clamp_i(state->quota_secondary_percent, 0, 100) : 0;
    if (state->quota_ready && quota_primary_pct == 0 && strstr(quota_primary_text, "INCL")) {
        quota_primary_pct = 100;
    }
    draw_centered_ascii(graybuf, width, bx + 4, bw - 8, iy, quota_title, UI_BLACK);

    iy += 11;
    draw_centered_ascii(graybuf, width, bx + 6, bw - 12, iy, quota_primary_text, UI_BLACK);
    draw_progress_bar(graybuf, width, height, bx + 14, iy + 8, bw - 28, quota_primary_pct);

    iy += 16;
    draw_centered_ascii(graybuf, width, bx + 6, bw - 12, iy, quota_secondary_text, UI_BLACK);
    draw_progress_bar(graybuf, width, height, bx + 14, iy + 8, bw - 28, quota_secondary_pct);
}

/* ================================================================== */
/*  右面板 → 统一底部工作/结算面板 (暗底，同位置原地过渡)              */
/* ================================================================== */

/* New work sprite from work_row1.bin (8 frames, 120×100 grayscale) */
static uint8_t *s_work_row1_data = NULL;
static int s_work_row1_frames = 0;
static int s_work_row1_w = 0, s_work_row1_h = 0;
static bool s_work_row1_loaded = false;
static bool s_work_row1_tried = false;

static void load_work_row1(void)
{
    if (s_work_row1_loaded || s_work_row1_tried) return;
    s_work_row1_tried = true;
    FILE *f = fopen("/spiffs/work_row1.bin", "rb");
    if (!f) { ESP_LOGW(TAG, "work_row1.bin not found"); return; }
    uint32_t hdr[3];
    if (fread(hdr, 4, 3, f) != 3) { fclose(f); return; }
    s_work_row1_frames = (int)hdr[0];
    s_work_row1_w = (int)hdr[1];
    s_work_row1_h = (int)hdr[2];
    size_t total = (size_t)s_work_row1_frames * s_work_row1_w * s_work_row1_h;
    s_work_row1_data = heap_caps_malloc(total, MALLOC_CAP_SPIRAM);
    if (!s_work_row1_data) { fclose(f); return; }
    if (fread(s_work_row1_data, 1, total, f) != total) {
        fclose(f); heap_caps_free(s_work_row1_data);
        s_work_row1_data = NULL; return;
    }
    fclose(f);
    s_work_row1_loaded = true;
    ESP_LOGI(TAG, "Work sprites loaded: %d × %dx%d", s_work_row1_frames, s_work_row1_w, s_work_row1_h);
}

/* Draw work sprite frame into a dark-background area with scaling */
static void draw_work_sprite(uint8_t *buf, int buf_w, int buf_h,
                              int x, int y, int dst_w, int dst_h, int tick)
{
    load_work_row1();
    if (!s_work_row1_loaded || !s_work_row1_data || s_work_row1_frames <= 0) {
        /* Fallback: try old sprite system */
        return;
    }
    int frame = (tick / 3) % s_work_row1_frames;  /* 3fps */
    const uint8_t *src = s_work_row1_data + frame * s_work_row1_w * s_work_row1_h;
    int src_w = s_work_row1_w;
    int src_h = s_work_row1_h;

    for (int dy = 0; dy < dst_h; dy++) {
        int sy = dy * src_h / dst_h;
        for (int dx = 0; dx < dst_w; dx++) {
            int sx = dx * src_w / dst_w;
            int px = x + dx, py = y + dy;
            if (px >= 0 && px < buf_w && py >= 0 && py < buf_h) {
                buf[py * buf_w + px] = src[sy * src_w + sx];
            }
        }
    }
}

void panels_draw_right(uint8_t *graybuf, int width, int height,
                       const aura_state_t *state)
{
    if (!state->agent_panel_visible) return;

    bool is_settle = (strcmp(state->agent_title, "SETTLE") == 0);

    if (!is_settle && (state->ui_mode == AURA_UI_LISTENING || state->ui_mode == AURA_UI_SPEAKING)) {
        return;
    }

    const int panel_w = 126;
    const int panel_h = is_settle ? 80 : 136;
    const int r = 5;
    /* 右边框与底部对话框（376 宽居中，右边距 12）严格对齐 */
    const int panel_x = width - panel_w - 12;
    /* 位置固定：始终停在对话框上方（对话框 376×80、底边距 26、间隙 4），
     * 不管对话框当下是否可见——避免面板在两个位置之间跳动。 */
    const int panel_y = height - 80 - 26 - panel_h - 4;

    draw_rounded_dark_box(graybuf, width, height, panel_x, panel_y, panel_w, panel_h, r);

    if (is_settle) {
        /* ── SETTLE ── vertically centered layout
         * Total content: REWARD(8) + gap(4) + rule(1) + gap(6) + icons(10) + gap(6) + rule(1) + gap(6) + text(12) = 54
         * Margin: (80 - 54) / 2 = 13 top
         */
        int top_margin = (panel_h - 54) / 2;

        draw_centered_ascii(graybuf, width, panel_x, panel_w, panel_y + top_margin, "REWARD", UI_WHITE);
        fill_rect(graybuf, width, height, panel_x + 12, panel_y + top_margin + 12, panel_w - 24, 1, UI_WHITE);

        int iy = panel_y + top_margin + 19;
        int col_w = (panel_w - 24) / 3;  /* ~34px per column */
        int cx = panel_x + 12;
        char val[12];

        font_draw_bean(graybuf, width, cx + 2, iy, UI_WHITE);
        snprintf(val, sizeof(val), "%+d", state->settle_beans_delta);
        font_draw_string(graybuf, width, cx + 12, iy, val, UI_WHITE);

        cx += col_w;
        font_draw_bolt(graybuf, width, cx + 2, iy, UI_WHITE);
        snprintf(val, sizeof(val), "%+d", state->settle_energy_delta);
        font_draw_string(graybuf, width, cx + 12, iy, val, UI_WHITE);

        cx += col_w;
        font_draw_smile(graybuf, width, cx + 2, iy, UI_WHITE);
        snprintf(val, sizeof(val), "%+d", state->settle_mood_delta);
        font_draw_string(graybuf, width, cx + 12, iy, val, UI_WHITE);

        fill_rect(graybuf, width, height, panel_x + 12, iy + 15, panel_w - 24, 1, UI_WHITE);

        const char *continue_text = state->ui_language == 1 ? "Press key" :
                                    state->ui_language == 2 ? "ボタンで続ける" : "按键继续";
        draw_centered_utf8(graybuf, width, panel_x, panel_w,
                           panel_y + panel_h - top_margin - 12,
                           continue_text, UI_WHITE);

    } else {
        /* ── WORK ── sprite fills frame, hearts vertically centered below */

        int sp_sz = panel_w - 16;  /* 110px */
        int sp_x = panel_x + 8;
        int sp_y = panel_y + 6;
        draw_work_sprite(graybuf, width, height, sp_x, sp_y, sp_sz, sp_sz,
                         state->ui_anim_tick);

        /* Hearts vertically centered in remaining space */
        int prog = clamp_i(state->agent_progress, 0, 100);
        int n_hearts = 10;
        int solid = (n_hearts * prog) / 100;
        bool flash_next = (prog % 10) != 0 && solid < n_hearts;
        int heart_h = 7;
        int remaining = panel_h - (sp_y - panel_y) - sp_sz;  /* space below sprite */
        int hy = sp_y + sp_sz + (remaining - heart_h) / 2;
        int heart_spacing = 10;
        int hearts_w = n_hearts * heart_spacing;
        int hx = panel_x + (panel_w - hearts_w) / 2;
        for (int i = 0; i < n_hearts; i++) {
            bool filled = (i < solid);
            if (flash_next && i == solid && ((state->ui_anim_tick / 2) % 2) == 0)
                filled = true;
            draw_heart_7(graybuf, width, height, hx, hy, filled, UI_WHITE);
            hx += heart_spacing;
        }
    }
}

/* ================================================================== */
/*  录音胶囊                                                           */
/* ================================================================== */
static void draw_rounded_dark_box(uint8_t *buf, int buf_w, int buf_h,
                                  int x, int y, int w, int h, int r)
{
    /* Black-filled rectangle with rounded corners and white border */
    fill_rect(buf, buf_w, buf_h, x + r, y, w - 2 * r, h, UI_BLACK);
    fill_rect(buf, buf_w, buf_h, x, y + r, w, h - 2 * r, UI_BLACK);

    /* Fill corner rects */
    fill_rect(buf, buf_w, buf_h, x + 1, y + 1, r - 1, r - 1, UI_BLACK);
    fill_rect(buf, buf_w, buf_h, x + w - r, y + 1, r - 1, r - 1, UI_BLACK);
    fill_rect(buf, buf_w, buf_h, x + 1, y + h - r, r - 1, r - 1, UI_BLACK);
    fill_rect(buf, buf_w, buf_h, x + w - r, y + h - r, r - 1, r - 1, UI_BLACK);

    /* Outer border — horizontal */
    fill_rect(buf, buf_w, buf_h, x + r, y, w - 2 * r, 1, UI_WHITE);
    fill_rect(buf, buf_w, buf_h, x + r, y + h - 1, w - 2 * r, 1, UI_WHITE);
    /* Outer border — vertical */
    fill_rect(buf, buf_w, buf_h, x, y + r, 1, h - 2 * r, UI_WHITE);
    fill_rect(buf, buf_w, buf_h, x + w - 1, y + r, 1, h - 2 * r, UI_WHITE);

    /* Corner pixels for smooth rounding (r=3 or similar) */
    if (r >= 3) {
        /* top-left */
        fill_rect(buf, buf_w, buf_h, x + 1, y + r - 1, 1, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + r - 1, y + 1, 1, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + 2, y + 1, r - 3, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + 1, y + 2, 1, r - 3, UI_WHITE);
        /* top-right */
        fill_rect(buf, buf_w, buf_h, x + w - 2, y + r - 1, 1, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + w - r, y + 1, 1, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + w - r + 1, y + 1, r - 3, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + w - 2, y + 2, 1, r - 3, UI_WHITE);
        /* bottom-left */
        fill_rect(buf, buf_w, buf_h, x + 1, y + h - r, 1, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + r - 1, y + h - 2, 1, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + 2, y + h - 2, r - 3, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + 1, y + h - r + 1, 1, r - 3, UI_WHITE);
        /* bottom-right */
        fill_rect(buf, buf_w, buf_h, x + w - 2, y + h - r, 1, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + w - r, y + h - 2, 1, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + w - r + 1, y + h - 2, r - 3, 1, UI_WHITE);
        fill_rect(buf, buf_w, buf_h, x + w - 2, y + h - r + 1, 1, r - 3, UI_WHITE);
    }

    /* Inner border (3px inset) */
    int ix = x + 3, iy = y + 3, iw = w - 6, ih = h - 6;
    int ir = (r > 3) ? r - 2 : 1;
    fill_rect(buf, buf_w, buf_h, ix + ir, iy, iw - 2 * ir, 1, UI_WHITE);
    fill_rect(buf, buf_w, buf_h, ix + ir, iy + ih - 1, iw - 2 * ir, 1, UI_WHITE);
    fill_rect(buf, buf_w, buf_h, ix, iy + ir, 1, ih - 2 * ir, UI_WHITE);
    fill_rect(buf, buf_w, buf_h, ix + iw - 1, iy + ir, 1, ih - 2 * ir, UI_WHITE);
}

static void draw_recording_capsule(uint8_t *graybuf, int width, int height,
                                   const aura_state_t *state)
{
    /*
     * "Listening Bar" — the waveform is the message.
     *
     * ┌──────────────────────────────────────────────────────────┐
     * │  ●  ▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌▌  │
     * │                 按住录音 · 松开发送                       │
     * └──────────────────────────────────────────────────────────┘
     *
     * 280×44, centered, 6px from bottom.
     * Single thin border. No inner border. No mic icon. No title.
     * The waveform itself communicates "listening".
     */
    const int pw = 280;
    const int ph = 44;
    const int r = 5;
    int x = (width - pw) / 2;
    int y = height - ph - 28;  /* raised for better visual balance */
    int amp = clamp_i(state->mic_level, 0, 100);

    /* ── Dark capsule: single thin white border ── */
    fill_rect(graybuf, width, height, x + r, y, pw - 2 * r, ph, UI_BLACK);
    fill_rect(graybuf, width, height, x, y + r, pw, ph - 2 * r, UI_BLACK);
    fill_rect(graybuf, width, height, x + 2, y + 2, r - 1, r - 1, UI_BLACK);
    fill_rect(graybuf, width, height, x + pw - r - 1, y + 2, r - 1, r - 1, UI_BLACK);
    fill_rect(graybuf, width, height, x + 2, y + ph - r - 1, r - 1, r - 1, UI_BLACK);
    fill_rect(graybuf, width, height, x + pw - r - 1, y + ph - r - 1, r - 1, r - 1, UI_BLACK);
    /* Single border */
    fill_rect(graybuf, width, height, x + r, y, pw - 2 * r, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + r, y + ph - 1, pw - 2 * r, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x, y + r, 1, ph - 2 * r, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - 1, y + r, 1, ph - 2 * r, UI_WHITE);
    /* Smooth corners (r=5) */
    fill_rect(graybuf, width, height, x + 1, y + r - 1, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + r - 1, y + 1, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + 2, y + 1, r - 3, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + 1, y + 2, 1, r - 3, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - 2, y + r - 1, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - r, y + 1, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - r + 1, y + 1, r - 3, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - 2, y + 2, 1, r - 3, UI_WHITE);
    fill_rect(graybuf, width, height, x + 1, y + ph - r, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + r - 1, y + ph - 2, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + 2, y + ph - 2, r - 3, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + 1, y + ph - r + 1, 1, r - 3, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - 2, y + ph - r, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - r, y + ph - 2, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - r + 1, y + ph - 2, r - 3, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - 2, y + ph - r + 1, 1, r - 3, UI_WHITE);

    /* ── Pulsing indicator dot (5×5) ── */
    {
        int dx = x + 12;
        int dy = y + 9;
        bool filled = ((state->ui_anim_tick / 3) % 2) == 0;
        static const uint8_t dot_f[] = {0x0E,0x1F,0x1F,0x1F,0x0E};
        static const uint8_t dot_r[] = {0x0E,0x11,0x11,0x11,0x0E};
        const uint8_t *bmp = filled ? dot_f : dot_r;
        for (int row = 0; row < 5; row++)
            for (int col = 0; col < 5; col++)
                if (bmp[row] & (1 << (4 - col)))
                    graybuf[(dy + row) * width + dx + col] = UI_WHITE;
    }

    /* ── Wide waveform (32 bars) ── */
    {
        static const float w32[] = {
            0.15f,0.22f,0.30f,0.38f,0.46f,0.54f,0.62f,0.72f,
            0.80f,0.86f,0.92f,0.96f,0.99f,1.00f,0.99f,0.96f,
            0.96f,0.99f,1.00f,0.99f,0.96f,0.92f,0.86f,0.80f,
            0.72f,0.62f,0.54f,0.46f,0.38f,0.30f,0.22f,0.15f
        };
        static float env = 0.0f;
        const int bars = 32, bw = 3, bg = 2;
        const int max_h = 14, min_h = 2;
        float tgt = (float)clamp_i(amp, 0, 100) / 100.0f;
        int tick = state->ui_anim_tick;
        if (tick <= 1) {
            env = tgt;
        } else if (tgt > env) {
            env += (tgt - env) * 0.35f;
        } else {
            env += (tgt - env) * 0.12f;
        }
        if (env < 0) env = 0;
        if (env > 1) env = 1;

        int wave_total = bars * (bw + bg) - bg;  /* 158px */
        int wave_left = x + 24;
        int wave_right = x + pw - 10;
        int wave_x = wave_left + ((wave_right - wave_left) - wave_total) / 2;
        int wave_cy = y + 11;  /* vertical center of waveform row */

        for (int i = 0; i < bars; i++) {
            uint32_t seed = (uint32_t)(tick * 1103515245u + (i + 1) * 12345u);
            float jit = (((seed >> 16) & 0xFF) / 255.0f) * 0.05f - 0.025f;
            float lv = (0.08f + env * 1.9f) * w32[i] * (1.0f + jit);
            int h = min_h + (int)(lv * max_h + 0.5f);
            h = clamp_i(h, min_h, max_h + min_h);
            int sx = wave_x + i * (bw + bg);
            fill_rect(graybuf, width, height, sx, wave_cy - h / 2, bw, h, UI_WHITE);
        }
    }

    /* ── Hint — understated, centered ── */
    const char *record_hint = state->ui_language == 1 ? "Hold to record · Release to send" :
                              state->ui_language == 2 ? "長押しで録音・離して送信" :
                                                       "按住录音 · 松开发送";
    draw_centered_utf8(graybuf, width, x, pw, y + 25, record_hint, UI_WHITE);
}

/* ================================================================== */
/*  对话框 — 暗底 + Emoji 头像                                         */
/* ================================================================== */
void panels_draw_dialogue(uint8_t *graybuf, int width, int height,
                          const aura_state_t *state)
{
    if (state->ui_mode == AURA_UI_LISTENING) {
        draw_recording_capsule(graybuf, width, height, state);
        return;
    }
    if (state->ui_mode == AURA_UI_PROCESSING) return;
    if (state->display_text[0] == '\0' || state->dialogue_ticks_left <= 0) return;

    /*
     * Dialogue box — dark fill, same design language as listening bar.
     *
     * ┌──────────────────────────────────────────────────────────────────┐
     * │                                                                  │
     * │  北京现在多云，23度，                                 ┌──────┐  │
     * │  湿度42%。                                           │ emoji │  │
     * │                                              ▼       └──────┘  │
     * │                                                                  │
     * └──────────────────────────────────────────────────────────────────┘
     *
     * 376×80, centered, 8px from bottom
     */
    const int pw = 376;
    const int ph = 80;
    const int r = 5;
    int x = (width - pw) / 2;
    int y = height - ph - 26;

    /* ── Dark box with single thin white border (same as listening bar) ── */
    fill_rect(graybuf, width, height, x + r, y, pw - 2 * r, ph, UI_BLACK);
    fill_rect(graybuf, width, height, x, y + r, pw, ph - 2 * r, UI_BLACK);
    fill_rect(graybuf, width, height, x + 2, y + 2, r - 1, r - 1, UI_BLACK);
    fill_rect(graybuf, width, height, x + pw - r - 1, y + 2, r - 1, r - 1, UI_BLACK);
    fill_rect(graybuf, width, height, x + 2, y + ph - r - 1, r - 1, r - 1, UI_BLACK);
    fill_rect(graybuf, width, height, x + pw - r - 1, y + ph - r - 1, r - 1, r - 1, UI_BLACK);
    /* Border */
    fill_rect(graybuf, width, height, x + r, y, pw - 2 * r, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + r, y + ph - 1, pw - 2 * r, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x, y + r, 1, ph - 2 * r, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - 1, y + r, 1, ph - 2 * r, UI_WHITE);
    /* Smooth corners */
    fill_rect(graybuf, width, height, x + 1, y + r - 1, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + r - 1, y + 1, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + 2, y + 1, r - 3, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + 1, y + 2, 1, r - 3, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - 2, y + r - 1, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - r, y + 1, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - r + 1, y + 1, r - 3, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - 2, y + 2, 1, r - 3, UI_WHITE);
    fill_rect(graybuf, width, height, x + 1, y + ph - r, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + r - 1, y + ph - 2, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + 2, y + ph - 2, r - 3, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + 1, y + ph - r + 1, 1, r - 3, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - 2, y + ph - r, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - r, y + ph - 2, 1, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - r + 1, y + ph - 2, r - 3, 1, UI_WHITE);
    fill_rect(graybuf, width, height, x + pw - 2, y + ph - r + 1, 1, r - 3, UI_WHITE);

    /* ── Face portrait (bottom-right corner, follows current outfit) ── */
    const int emoji_sz = 52;  /* rendered size */
    int emoji_x = x + pw - emoji_sz - 10;
    int emoji_y = y + (ph - emoji_sz) / 2;
    int emo_idx = emotion_to_index(state->current_emotion);
    draw_face(graybuf, width, height, emoji_x, emoji_y, emoji_sz,
              state->current_outfit, emo_idx);

    /* ── Text area (white text on dark bg, left of portrait) ── */
    int txt_x = x + 14;
    int txt_y = y + 12;
    int txt_max_x = emoji_x - 14;
    int txt_max_y = y + ph - 12;
    int max_lines = (txt_max_y - txt_y) / 16;
    if (max_lines < 1) max_lines = 1;
    int page_count = wrapped_utf8_page_count(state->display_text, txt_x, txt_max_x, max_lines);
    int page_index = 0;
    if (page_count > 1) {
        int progress = state->dialogue_page_tick;
        if (progress < 0) progress = 0;
        if (progress > 999) progress = 999;
        page_index = (progress * page_count) / 1000;
        if (page_index >= page_count) page_index = page_count - 1;
    }
    draw_wrapped_utf8_page(graybuf, width,
                           txt_x, txt_y, txt_max_x, max_lines,
                           state->display_text, page_index, UI_WHITE);

    /* ── Pulsing ▼ indicator ── */
    if ((state->ui_anim_tick / 3) % 2 == 0) {
        int tri_cx = emoji_x - 14;
        int tri_y = y + ph - 16;
        fill_rect(graybuf, width, height, tri_cx - 3, tri_y,     7, 1, UI_WHITE);
        fill_rect(graybuf, width, height, tri_cx - 2, tri_y + 1, 5, 1, UI_WHITE);
        fill_rect(graybuf, width, height, tri_cx - 1, tri_y + 2, 3, 1, UI_WHITE);
        fill_rect(graybuf, width, height, tri_cx,     tri_y + 3, 1, 1, UI_WHITE);
    }
    if (page_count > 1) {
        char page_label[24];
        snprintf(page_label, sizeof(page_label), "%d/%d", page_index + 1, page_count);
        font_draw_string(graybuf, width, emoji_x - 38, y + ph - 15, page_label, UI_WHITE);
    }
}
