/**
 * 顶部状态栏 — STATUS_BAR_HEIGHT(19px) 高，纯黑底白字
 * 左: 日期 Apr 10  |  中: 时间  |  右: 天气图标+温度+WiFi
 */
#include "status_bar.h"
#include "font.h"
#include "aura_config.h"
#include <string.h>
#include <stdio.h>

/* ── 7×7 天气像素图标 ── */
static const uint8_t ICON_SUN[7][7] = {
    {0,0,1,0,1,0,0},
    {0,0,0,0,0,0,0},
    {1,0,1,1,1,0,1},
    {0,0,1,1,1,0,0},
    {1,0,1,1,1,0,1},
    {0,0,0,0,0,0,0},
    {0,0,1,0,1,0,0},
};
static const uint8_t ICON_CLOUD[7][7] = {
    {0,0,1,1,0,0,0},
    {0,1,1,1,1,0,0},
    {1,1,1,1,1,1,0},
    {1,1,1,1,1,1,1},
    {0,1,1,1,1,1,0},
    {0,0,0,0,0,0,0},
    {0,0,0,0,0,0,0},
};
static const uint8_t ICON_RAIN[7][7] = {
    {0,0,1,1,1,0,0},
    {0,1,1,1,1,1,0},
    {1,1,1,1,1,1,1},
    {0,0,0,0,0,0,0},
    {0,1,0,1,0,1,0},
    {0,0,0,0,0,0,0},
    {1,0,1,0,1,0,0},
};
static const uint8_t ICON_SNOW[7][7] = {
    {0,0,1,1,1,0,0},
    {0,1,1,1,1,1,0},
    {1,1,1,1,1,1,1},
    {0,0,0,0,0,0,0},
    {0,1,0,1,0,1,0},
    {1,0,1,0,1,0,0},
    {0,1,0,1,0,1,0},
};

static void draw_weather_icon(uint8_t *buf, int buf_w, int x, int y,
                              int icon, uint8_t color)
{
    const uint8_t (*bmp)[7];
    switch (icon) {
        case 1:  bmp = ICON_CLOUD; break;
        case 2:  bmp = ICON_RAIN;  break;
        case 3:  bmp = ICON_SNOW;  break;
        default: bmp = ICON_SUN;   break;
    }
    for (int dy = 0; dy < 7; dy++) {
        for (int dx = 0; dx < 7; dx++) {
            if (bmp[dy][dx]) {
                int px = x + dx;
                int py = y + dy;
                if (px >= 0 && px < buf_w && py >= 0 && py < 300)
                    buf[py * buf_w + px] = color;
            }
        }
    }
}

static const char *MONTH_ABBR[] = {
    "", "Jan","Feb","Mar","Apr","May","Jun",
    "Jul","Aug","Sep","Oct","Nov","Dec"
};

void status_bar_draw(uint8_t *graybuf, int width, const aura_state_t *state)
{
    /* 1. 纯黑背景 */
    for (int y = 0; y < STATUS_BAR_HEIGHT; y++) {
        for (int x = 0; x < width; x++) {
            graybuf[y * width + x] = 0;
        }
    }

    int cy = (STATUS_BAR_HEIGHT - 7) / 2;

    /* 2. 左: 日期 "Apr 10" */
    int lx = 4;
    int m = state->month;
    if (m < 1 || m > 12) m = 1;
    char date_str[12];
    snprintf(date_str, sizeof(date_str), "%s %d", MONTH_ABBR[m], state->day);
    font_draw_string(graybuf, width, lx, cy, date_str, 255);

    /* 3. 中: 时间 */
    char time_str[8];
    snprintf(time_str, sizeof(time_str), "%02d:%02d", state->hour, state->minute);
    int tw = font_string_width(time_str);
    int tx = (width - tw) / 2;
    font_draw_string(graybuf, width, tx, cy, time_str, 255);

    /* 4. 右: 天气图标 + 温度 + WiFi */
    /* WiFi 最右 */
    int rx = width - 19;   /* 新外壳右侧有遮挡，WiFi 图标（含左侧温度/天气）整体左移 5px */
    font_draw_wifi(graybuf, width, rx, cy - 1, state->wifi_strength, 255);

    /* 温度 */
    char temp_str[12];
    if (state->temperature > -40.0f) {
        snprintf(temp_str, sizeof(temp_str), "%.0f*", state->temperature);
    } else {
        snprintf(temp_str, sizeof(temp_str), "--*");
    }
    int temp_w = font_string_width(temp_str);
    rx -= (temp_w + 4);
    font_draw_string(graybuf, width, rx, cy, temp_str, 255);

    /* 天气图标 */
    rx -= 10;
    draw_weather_icon(graybuf, width, rx, cy, state->weather_icon, 255);
}
