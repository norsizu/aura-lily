#include "panels_info_board.h"

#include "aura_config.h"
#include "font.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

#define BOARD_WHITE 255
#define BOARD_BLACK 0

static int clamp_int(int value, int low, int high)
{
    return value < low ? low : (value > high ? high : value);
}

static void fill_rect(uint8_t *buf, int bw, int bh, int x, int y,
                      int w, int h, uint8_t color)
{
    int x0 = clamp_int(x, 0, bw);
    int y0 = clamp_int(y, 0, bh);
    int x1 = clamp_int(x + w, 0, bw);
    int y1 = clamp_int(y + h, 0, bh);
    for (int py = y0; py < y1; ++py) {
        memset(buf + py * bw + x0, color, (size_t)(x1 - x0));
    }
}

static void stroke_rect(uint8_t *buf, int bw, int bh, int x, int y,
                        int w, int h, uint8_t color)
{
    fill_rect(buf, bw, bh, x, y, w, 1, color);
    fill_rect(buf, bw, bh, x, y + h - 1, w, 1, color);
    fill_rect(buf, bw, bh, x, y, 1, h, color);
    fill_rect(buf, bw, bh, x + w - 1, y, 1, h, color);
}

static void centered_utf8(uint8_t *buf, int bw, int x, int w, int y,
                          const char *text, uint8_t color)
{
    font_draw_utf8(buf, bw, x + (w - font_utf8_width(text)) / 2, y, text, color);
}

static void note_text(char *dst, size_t dst_size, const char *src, int max_width)
{
    if (!dst || dst_size == 0) return;
    dst[0] = '\0';
    if (!src || !src[0]) return;

    size_t used = 0;
    const char *cursor = src;
    while (*cursor && used + 1 < dst_size) {
        size_t bytes = 1;
        unsigned char lead = (unsigned char)*cursor;
        if ((lead & 0xE0) == 0xC0) bytes = 2;
        else if ((lead & 0xF0) == 0xE0) bytes = 3;
        else if ((lead & 0xF8) == 0xF0) bytes = 4;
        if (used + bytes >= dst_size) break;
        memcpy(dst + used, cursor, bytes);
        dst[used + bytes] = '\0';
        if (font_utf8_width(dst) > max_width) {
            dst[used] = '\0';
            if (used + 3 < dst_size) {
                memcpy(dst + used, "...", 4);
            }
            return;
        }
        used += bytes;
        cursor += bytes;
    }
}

static int weekday_for_state(const aura_state_t *state)
{
    time_t now = time(NULL);
    struct tm local = {0};
    if (now > 0 && localtime_r(&now, &local)) return local.tm_wday;
    return 0;
}

static void draw_weather_icon(uint8_t *buf, int bw, int bh, int x, int y,
                              int icon, uint8_t color)
{
    if (icon == 0) {
        stroke_rect(buf, bw, bh, x + 8, y + 8, 18, 18, color);
        fill_rect(buf, bw, bh, x + 16, y, 2, 6, color);
        fill_rect(buf, bw, bh, x + 16, y + 28, 2, 6, color);
        fill_rect(buf, bw, bh, x, y + 16, 6, 2, color);
        fill_rect(buf, bw, bh, x + 28, y + 16, 6, 2, color);
        return;
    }
    fill_rect(buf, bw, bh, x + 4, y + 16, 27, 10, color);
    fill_rect(buf, bw, bh, x + 10, y + 10, 17, 13, color);
    if (icon == 2) {
        for (int i = 0; i < 3; ++i) fill_rect(buf, bw, bh, x + 8 + i * 9, y + 29, 2, 5, color);
    } else if (icon == 3) {
        for (int i = 0; i < 3; ++i) stroke_rect(buf, bw, bh, x + 7 + i * 9, y + 29, 4, 4, color);
    }
}

void panels_draw_info_board(uint8_t *graybuf, int width, int height,
                            const aura_state_t *state)
{
    if (!graybuf || !state || width < 300 || height <= STATUS_BAR_HEIGHT) return;

    const uint8_t bg = state->world_sleeping ? BOARD_BLACK : BOARD_WHITE;
    const uint8_t fg = state->world_sleeping ? BOARD_WHITE : BOARD_BLACK;
    fill_rect(graybuf, width, height, 0, STATUS_BAR_HEIGHT,
              width, height - STATUS_BAR_HEIGHT, bg);

    stroke_rect(graybuf, width, height, 8, 27, 194, 195, fg);
    stroke_rect(graybuf, width, height, 210, 27, 182, 93, fg);
    stroke_rect(graybuf, width, height, 210, 128, 182, 94, fg);
    stroke_rect(graybuf, width, height, 8, 230, 384, 63, fg);

    char date[20];
    snprintf(date, sizeof(date), "%02d.%02d", clamp_int(state->month, 1, 12),
             clamp_int(state->day, 1, 31));
    centered_utf8(graybuf, width, 8, 194, 47, date, fg);

    char clock[8];
    snprintf(clock, sizeof(clock), "%02d:%02d", clamp_int(state->hour, 0, 23),
             clamp_int(state->minute, 0, 59));
    int clock_x = 8 + (194 - font_string_width_large(clock)) / 2;
    font_draw_string_large(graybuf, width, clock_x, 91, clock, fg);

    static const char *const zh[] = {"星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"};
    static const char *const en[] = {"SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"};
    static const char *const ja[] = {"日曜日", "月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日"};
    int weekday = clamp_int(weekday_for_state(state), 0, 6);
    const char *weekday_text = state->ui_language == 1 ? en[weekday] :
                               (state->ui_language == 2 ? ja[weekday] : zh[weekday]);
    centered_utf8(graybuf, width, 8, 194, 145, weekday_text, fg);

    char stats[64];
    if (state->ui_language == 1) {
        snprintf(stats, sizeof(stats), "MOOD %d  ENERGY %d", state->mood, state->energy);
    } else if (state->ui_language == 2) {
        snprintf(stats, sizeof(stats), "気分%d  体力%d", state->mood, state->energy);
    } else {
        snprintf(stats, sizeof(stats), "心情%d  体力%d", state->mood, state->energy);
    }
    centered_utf8(graybuf, width, 8, 194, 184, stats, fg);

    font_draw_utf8(graybuf, width, 222, 39,
                   state->weather_valid && state->weather_city[0] ? state->weather_city : "--", fg);
    if (state->weather_valid) draw_weather_icon(graybuf, width, height, 226, 65, state->weather_icon, fg);
    char temp[16];
    snprintf(temp, sizeof(temp), state->weather_valid ? "%.0f*C" : "--*C", state->temperature);
    font_draw_string_2x(graybuf, width, 282, 71, temp, fg);

    const char *quota_label = state->ui_language == 2 ? "利用枠" :
                              (state->ui_language == 1 ? "QUOTA" : "额度");
    centered_utf8(graybuf, width, 210, 182, 142, quota_label, fg);
    const char *quota_value = state->quota_ready && state->quota_primary_text[0]
        ? state->quota_primary_text : "--";
    centered_utf8(graybuf, width, 210, 182, 180, quota_value, fg);

    const char *empty_note = state->ui_language == 2 ? "メモなし" :
                             (state->ui_language == 1 ? "NO NOTE" : "无便签");
    for (int i = 0; i < 3; ++i) {
        int cell_x = 8 + i * 128;
        if (i > 0) fill_rect(graybuf, width, height, cell_x, 238, 1, 47, fg);
        char note[64];
        note_text(note, sizeof(note), state->info_notes[i], 118);
        centered_utf8(graybuf, width, cell_x, 128, 253,
                      note[0] ? note : empty_note, fg);
    }
}
