/**
 * minigame_render.c — 1-bit visual renderer for 白日梦大冒险
 *
 * Screen  400×300  ST7305 RLCD — 0=black, 255=white in graybuf.
 *
 * Draws to a private PSRAM graybuf, thresholds to the shared
 * renderer framebuf, then calls rlcd_flush().
 *
 * Layout summary
 * ─────────────────────────────────────────────────────
 *  DIFFICULTY   centered boxes, inverted selected item
 *  STORY_EVENT  left panel (stats) | right panel (narrative+choices)
 *  ENCOUNTER    full-screen dark box, enemy name, knight art
 *  BATTLE       header bar + vs area + HP bars + log + status
 *  WIN_DAY      stats summary, prompt to continue
 *  WIN_GAME     celebration with star icons
 *  GAME_OVER    all-black background, white text
 */
#include "minigame_render.h"
#include "minigame_state.h"
#include "minigame_data.h"
#include "renderer.h"
#include "rlcd_driver.h"
#include "font.h"
#include "aura_config.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

static const char *TAG = "mg_render";

/* ── Render buffer ───────────────────────────────────────────────── */
static uint8_t *s_buf = NULL;   /* 400×300 graybuf in PSRAM */

static void gb_fill(uint8_t *graybuf, int x, int y, int w, int h, uint8_t c);

#define DESSERT_ASSET_COUNT 8
typedef struct {
    uint8_t *data;
    int width;
    int height;
    bool loaded;
    bool tried;
} dessert_asset_t;

static dessert_asset_t s_dessert_assets[DESSERT_ASSET_COUNT] = {0};

static void load_dessert_asset(int index)
{
    static const char *const paths[DESSERT_ASSET_COUNT] = {
        "/spiffs/desserts/pudding.bin",
        "/spiffs/desserts/dorayaki.bin",
        "/spiffs/desserts/ice_cream.bin",
        "/spiffs/desserts/matcha_parfait.bin",
        "/spiffs/desserts/strawberry_cake.bin",
        "/spiffs/desserts/strawberry_tart.bin",
        "/spiffs/desserts/chocolate_sundae.bin",
        "/spiffs/desserts/celebration_mille.bin",
    };
    if (index < 0 || index >= DESSERT_ASSET_COUNT) return;
    dessert_asset_t *asset = &s_dessert_assets[index];
    if (asset->loaded || asset->tried) return;
    asset->tried = true;

    FILE *file = fopen(paths[index], "rb");
    if (!file) {
        ESP_LOGW(TAG, "Dessert asset not found: %s", paths[index]);
        return;
    }
    uint32_t width = 0, height = 0;
    if (fread(&width, sizeof(width), 1, file) != 1 ||
        fread(&height, sizeof(height), 1, file) != 1 ||
        width == 0 || height == 0 || width > RLCD_WIDTH || height > RLCD_HEIGHT) {
        fclose(file);
        ESP_LOGW(TAG, "Invalid dessert asset header: %s", paths[index]);
        return;
    }
    size_t size = (size_t)width * height;
    asset->data = heap_caps_malloc(size, MALLOC_CAP_SPIRAM);
    if (!asset->data || fread(asset->data, 1, size, file) != size) {
        fclose(file);
        if (asset->data) heap_caps_free(asset->data);
        asset->data = NULL;
        return;
    }
    fclose(file);
    asset->width = (int)width;
    asset->height = (int)height;
    asset->loaded = true;
    ESP_LOGI(TAG, "Dessert asset loaded: %s (%lux%lu)", paths[index],
             (unsigned long)width, (unsigned long)height);
}

static void draw_dessert_asset(uint8_t *graybuf, int index,
                               int x, int y, int box_w, int box_h)
{
    gb_fill(graybuf, x, y, box_w, box_h, 0xFE);
    load_dessert_asset(index);
    if (index < 0 || index >= DESSERT_ASSET_COUNT ||
        !s_dessert_assets[index].loaded) return;

    const dessert_asset_t *asset = &s_dessert_assets[index];
    int draw_w = box_w;
    int draw_h = (asset->height * draw_w) / asset->width;
    if (draw_h > box_h) {
        draw_h = box_h;
        draw_w = (asset->width * draw_h) / asset->height;
    }
    int ox = x + (box_w - draw_w) / 2;
    int oy = y + (box_h - draw_h) / 2;
    for (int dy = 0; dy < draw_h; dy++) {
        int sy = dy * asset->height / draw_h;
        for (int dx = 0; dx < draw_w; dx++) {
            int sx = dx * asset->width / draw_w;
            graybuf[(oy + dy) * RLCD_WIDTH + ox + dx] =
                asset->data[sy * asset->width + sx];
        }
    }
}

/* ── Low-level drawing helpers (all use s_buf internally) ─────────── */

static inline void _set(int x, int y, uint8_t c)
{
    if (x >= 0 && x < RLCD_WIDTH && y >= 0 && y < RLCD_HEIGHT)
        s_buf[y * RLCD_WIDTH + x] = c;
}

static void mg_fill(int x, int y, int w, int h, uint8_t c)
{
    int x1 = x + w, y1 = y + h;
    if (x  < 0)          x  = 0;
    if (y  < 0)          y  = 0;
    if (x1 > RLCD_WIDTH) x1 = RLCD_WIDTH;
    if (y1 > RLCD_HEIGHT)y1 = RLCD_HEIGHT;
    for (int row = y; row < y1; row++)
        memset(s_buf + row * RLCD_WIDTH + x, c, x1 - x);
}

static void mg_hline(int x, int y, int w, uint8_t c) { mg_fill(x, y, w, 1, c); }
static void mg_vline(int x, int y, int h, uint8_t c) { mg_fill(x, y, 1, h, c); }

static void mg_stroke(int x, int y, int w, int h, uint8_t c)
{
    mg_hline(x,       y,       w, c);
    mg_hline(x,       y+h-1,   w, c);
    mg_vline(x,       y,       h, c);
    mg_vline(x+w-1,   y,       h, c);
}

/* Helpers for overlay panels drawn onto the shared renderer graybuf.
 * Keep these separate from mg_fill/mg_stroke, which target the minigame's
 * private s_buf. */
static void gb_fill(uint8_t *gb, int x, int y, int w, int h, uint8_t c)
{
    int x1 = x + w, y1 = y + h;
    if (x  < 0)           x  = 0;
    if (y  < 0)           y  = 0;
    if (x1 > RLCD_WIDTH)  x1 = RLCD_WIDTH;
    if (y1 > RLCD_HEIGHT) y1 = RLCD_HEIGHT;
    if (x >= x1 || y >= y1) return;
    for (int row = y; row < y1; row++)
        memset(gb + row * RLCD_WIDTH + x, c, x1 - x);
}

static void gb_hline(uint8_t *gb, int x, int y, int w, uint8_t c)
{
    gb_fill(gb, x, y, w, 1, c);
}

static void gb_vline(uint8_t *gb, int x, int y, int h, uint8_t c)
{
    gb_fill(gb, x, y, 1, h, c);
}

static void gb_stroke(uint8_t *gb, int x, int y, int w, int h, uint8_t c)
{
    gb_hline(gb, x,       y,       w, c);
    gb_hline(gb, x,       y+h-1,   w, c);
    gb_vline(gb, x,       y,       h, c);
    gb_vline(gb, x+w-1,   y,       h, c);
}

static void gb_dashed_hline(uint8_t *gb, int x, int y, int w, uint8_t c)
{
    for (int i = 0; i < w; i += 4)
        gb_fill(gb, x + i, y, 2, 1, c);
}

static void gb_corner_marks(uint8_t *gb, int x, int y, int w, int h, uint8_t c)
{
    const int l = 9;
    gb_hline(gb, x,         y,         l, c);
    gb_vline(gb, x,         y,         l, c);
    gb_hline(gb, x + w - l, y,         l, c);
    gb_vline(gb, x + w - 1, y,         l, c);
    gb_hline(gb, x,         y + h - 1, l, c);
    gb_vline(gb, x,         y + h - l, l, c);
    gb_hline(gb, x + w - l, y + h - 1, l, c);
    gb_vline(gb, x + w - 1, y + h - l, l, c);
}

/* Progress bar: white bg + black border + left-fill */
static void mg_bar(int x, int y, int w, int h, int val, int maxv)
{
    mg_fill(x, y, w, h, 255);
    mg_stroke(x, y, w, h, 0);
    if (maxv > 0 && val > 0) {
        int fill = val * (w - 2) / maxv;
        if (fill > w - 2) fill = w - 2;
        if (fill > 0) mg_fill(x + 1, y + 1, fill, h - 2, 0);
    }
}

/* Text helpers wrapping font.h — all draw to s_buf */
static void mg_cn(int x, int y, const char *s, uint8_t c)
{
    font_draw_utf8(s_buf, RLCD_WIDTH, x, y, s, c);
}
static void mg_ascii(int x, int y, const char *s, uint8_t c)
{
    font_draw_string(s_buf, RLCD_WIDTH, x, y, s, c);
}
static void mg_ascii2x(int x, int y, const char *s, uint8_t c)
{
    font_draw_string_2x(s_buf, RLCD_WIDTH, x, y, s, c);
}
static void mg_cn_cx(int cx, int y, const char *s, uint8_t c)
{
    int w = font_utf8_width(s);
    mg_cn(cx - w / 2, y, s, c);
}
static void mg_ascii_cx(int cx, int y, const char *s, uint8_t c)
{
    int w = font_string_width(s);
    mg_ascii(cx - w / 2, y, s, c);
}
static void mg_ascii2x_cx(int cx, int y, const char *s, uint8_t c)
{
    int w = font_string_width_2x(s);
    mg_ascii2x(cx - w / 2, y, s, c);
}

static const char *move_name(int move_id)
{
    switch (move_id) {
    case MG_MOVE_DOUBLE_STRIKE: return "连击";
    case MG_MOVE_COUNTER:       return "反弹";
    case MG_MOVE_FIRST_BURST:   return "首击";
    case MG_MOVE_QI_SURGE:      return "气势";
    default:                    return "";
    }
}

static void draw_delta_line(int *y, const char *label, int delta)
{
    if (delta == 0) return;
    char buf[48];
    snprintf(buf, sizeof(buf), "%s %+d", label, delta);
    mg_ascii(92, *y, buf, 0);
    *y += 16;
}

/* ── Threshold s_buf → shared framebuf → rlcd_flush ─────────────── */
static void mg_flush(void)
{
    uint8_t *fb = renderer_get_framebuffer();
    memset(fb, 0xFF, RLCD_FB_SIZE);
    for (int y = 0; y < RLCD_HEIGHT; y++) {
        for (int x = 0; x < RLCD_WIDTH; x++) {
            if (s_buf[y * RLCD_WIDTH + x] < 128)
                rlcd_set_pixel(fb, x, y, true);
        }
    }
    rlcd_flush(fb);
}

/* ── Placeholder character art ───────────────────────────────────── */

/* Hollow circle outline (±1 px tolerance) */
static void circle_ring(int cx, int cy, int r)
{
    for (int dy = -r - 1; dy <= r + 1; dy++) {
        for (int dx = -r - 1; dx <= r + 1; dx++) {
            int d2 = dx * dx + dy * dy;
            if (d2 >= (r - 1) * (r - 1) && d2 <= (r + 1) * (r + 1))
                _set(cx + dx, cy + dy, 0);
        }
    }
}

/* Filled circle */
static void circle_fill(int cx, int cy, int r, uint8_t c)
{
    for (int dy = -r; dy <= r; dy++)
        for (int dx = -r; dx <= r; dx++)
            if (dx * dx + dy * dy <= r * r)
                _set(cx + dx, cy + dy, c);
}

/* 豆豆: round cat-ear character, 36×52 px footprint */
static void draw_doudou(int cx, int cy)
{
    /* White face disc */
    circle_fill(cx, cy, 16, 255);
    /* Head outline */
    circle_ring(cx, cy, 16);
    /* Cat ears (two small wedges) */
    for (int i = 0; i < 6; i++) {
        mg_hline(cx - 14 + i, cy - 16 - i, 3, 0);  /* left ear  */
        mg_hline(cx + 11 - i, cy - 16 - i, 3, 0);  /* right ear */
    }
    /* Eyes (4×4 squares) */
    mg_fill(cx - 8, cy - 5, 5, 5, 0);
    mg_fill(cx + 3, cy - 5, 5, 5, 0);
    /* Nose dot */
    mg_fill(cx - 1, cy + 2, 3, 2, 0);
    /* Mouth (U-shape) */
    mg_fill(cx - 5, cy + 8,  2, 3, 0);
    mg_fill(cx + 3, cy + 8,  2, 3, 0);
    mg_hline(cx - 3, cy + 10, 7, 0);
    /* Body (rounded rectangle) */
    mg_fill(cx - 11, cy + 18, 22, 22, 0);
    mg_fill(cx - 9,  cy + 20, 18, 18, 255);  /* white interior */
    /* Tiny button on shirt */
    mg_fill(cx - 1, cy + 24, 3, 3, 0);
}

/* 霓虹骑士: angular armoured figure, ~28×56 px footprint */
static void draw_knight(int cx, int cy)
{
    /* Helmet box */
    mg_fill(cx - 12, cy - 28, 24, 20, 0);
    mg_fill(cx - 8,  cy - 24, 16, 10, 255);  /* visor opening */
    mg_fill(cx - 6,  cy - 20, 12,  4, 0);   /* visor slit */
    /* Shoulder guards */
    mg_fill(cx - 16, cy - 8,  6, 7, 0);
    mg_fill(cx + 10, cy - 8,  6, 7, 0);
    /* Torso armour */
    mg_fill(cx - 10, cy - 8,  20, 26, 0);
    mg_fill(cx - 7,  cy - 5,  14, 20, 255);  /* chest plate cutout */
    mg_fill(cx - 3,  cy - 2,   6,  6, 0);   /* centre emblem */
    /* Legs */
    mg_fill(cx - 10, cy + 18,  8, 16, 0);
    mg_fill(cx + 2,  cy + 18,  8, 16, 0);
    /* Lance (right) */
    mg_vline(cx + 16, cy - 34, 62, 0);
    mg_fill(cx + 13,  cy - 36,  8,  5, 0);   /* spear tip */
}

/* ── Screen: DIFFICULTY ──────────────────────────────────────────── */
static void draw_difficulty(const mg_run_state_t *r)
{
    mg_fill(0, 0, RLCD_WIDTH, RLCD_HEIGHT, 255);
    mg_stroke(2, 2, 396, 296, 0);

    /* Title */
    mg_cn_cx(200, 26, "白日梦大冒险", 0);
    mg_ascii_cx(200, 50, "- Daydream Adventure -", 0);

    /* Decorative stars */
    font_draw_star(s_buf, RLCD_WIDTH,  18, 26, 0);
    font_draw_star(s_buf, RLCD_WIDTH, 370, 26, 0);

    /* Divider */
    mg_hline(30, 72, 340, 0);

    /* Option boxes */
    static const char *names[3] = {"轻松  8日", "普通 20日", "困难 30日"};
    static const char *stats[3] = {
        "HP:200  ATK:30  DEF:10",
        "HP:180  ATK:32  DEF:9",
        "HP:160  ATK:34  DEF:8",
    };
    const int bx = 100, bw = 200, bh = 34;
    const int ys[3] = {84, 126, 168};

    for (int i = 0; i < 3; i++) {
        int by = ys[i];
        if (i == r->sel) {
            mg_fill(bx, by, bw, bh, 0);
            mg_stroke(bx, by, bw, bh, 0);
            mg_cn_cx(bx + bw / 2, by + 9, names[i], 255);
        } else {
            mg_fill(bx, by, bw, bh, 255);
            mg_stroke(bx, by, bw, bh, 0);
            mg_cn_cx(bx + bw / 2, by + 9, names[i], 0);
        }
    }

    /* Selected difficulty stat hint */
    mg_ascii_cx(200, 218, stats[r->sel], 0);

    /* Bottom hint */
    mg_ascii_cx(200, 244, "Right: switch   Left: confirm", 0);
    mg_ascii_cx(200, 256, "Left long: exit", 0);
}

/* ── Screen: STORY_EVENT ─────────────────────────────────────────── */
static void draw_story_event(const mg_run_state_t *r)
{
    const mg_event_t  *ev = &mg_events[r->event_index];
    const mg_player_t *p  = &r->player;

    mg_fill(0, 0, RLCD_WIDTH, RLCD_HEIGHT, 255);

    /* Vertical divider (double line for visual weight) */
    mg_vline(180, 0, RLCD_HEIGHT, 0);
    mg_vline(181, 0, RLCD_HEIGHT, 0);

    /* ── Left panel ────────────────────────────────────────────── */

    /* Day header */
    char buf[48];
    snprintf(buf, sizeof(buf), "Day %d / %d", r->day_current, r->days_total);
    mg_ascii(4, 4, buf, 0);
    mg_hline(0, 16, 180, 0);

    /* Character art centred in left panel, y=20..140 */
    draw_doudou(88, 80);

    /* Resource bars (y=152 downwards, 18 px per row) */
    int by = 152;

    font_draw_heart(s_buf, RLCD_WIDTH, 4, by, 0);
    snprintf(buf, sizeof(buf), "%d", p->hp);
    mg_ascii(18, by, buf, 0);
    mg_bar(52, by, 122, 9, p->hp, p->hp_max);
    by += 18;

    font_draw_bolt(s_buf, RLCD_WIDTH, 4, by, 0);
    snprintf(buf, sizeof(buf), "%d", p->mp);
    mg_ascii(18, by, buf, 0);
    mg_bar(52, by, 122, 9, p->mp, p->mp_max);
    by += 18;

    font_draw_sparkle(s_buf, RLCD_WIDTH, 4, by, 0);
    snprintf(buf, sizeof(buf), "%d", p->en);
    mg_ascii(18, by, buf, 0);
    mg_bar(52, by, 122, 9, p->en, p->en_max);
    by += 18;

    mg_ascii(4, by, "QI", 0);
    snprintf(buf, sizeof(buf), "%d", p->qi);
    mg_ascii(18, by, buf, 0);
    mg_bar(52, by, 122, 9, p->qi, 100);
    by += 18;

    font_draw_star(s_buf, RLCD_WIDTH, 4, by, 0);
    snprintf(buf, sizeof(buf), "x%d", p->shard);
    mg_ascii(18, by, buf, 0);
    by += 18;

    /* Unlocked moves */
    if (p->moves[MG_MOVE_DOUBLE_STRIKE]) {
        mg_ascii(4, by, "[连击]", 0);
        by += 10;
    }
    if (p->moves[MG_MOVE_COUNTER]) {
        mg_ascii(4, by, "[反弹]", 0);
        by += 10;
    }
    if (p->moves[MG_MOVE_FIRST_BURST]) {
        mg_ascii(4, by, "[首击]", 0);
        by += 10;
    }

    /* ── Right panel (x=184..399, w=216, cx=292) ───────────────── */
    const int rx = 184, rcx = 292;

    /* Title bar (inverted) */
    mg_fill(182, 0, 218, 22, 0);
    mg_cn_cx(rcx, 3, ev->title, 255);

    /* Narrative (4 lines, CN16 = 16 px each, +4 px gap) */
    for (int i = 0; i < 4; i++) {
        if (ev->lines[i]) {
            mg_cn(rx, 26 + i * 20, ev->lines[i], 0);
        }
    }

    /* Separator */
    mg_hline(182, 108, 218, 0);

    /* Action header */
    mg_ascii(rx, 112, "ACTION:", 0);

    /* Choice boxes (each 42 px tall, with 4 px gap between) */
    /* Choice A  y=122..163 */
    {
        int cy = 122, ch = 42;
        if (r->sel == 0) {
            mg_fill(182, cy, 218, ch, 0);
            mg_cn(rx, cy + 2, ev->choices[0].tag,  255);
            mg_cn(rx, cy + 22, ev->choices[0].text, 255);
        } else {
            mg_stroke(182, cy, 218, ch, 0);
            mg_cn(rx, cy + 2, ev->choices[0].tag,  0);
            mg_cn(rx, cy + 22, ev->choices[0].text, 0);
        }
    }

    /* Choice B  y=169..210 */
    {
        int cy = 169, ch = 42;
        if (r->sel == 1) {
            mg_fill(182, cy, 218, ch, 0);
            mg_cn(rx, cy + 2, ev->choices[1].tag,  255);
            mg_cn(rx, cy + 22, ev->choices[1].text, 255);
        } else {
            mg_stroke(182, cy, 218, ch, 0);
            mg_cn(rx, cy + 2, ev->choices[1].tag,  0);
            mg_cn(rx, cy + 22, ev->choices[1].text, 0);
        }
    }

    /* Bottom hint */
    mg_ascii_cx(rcx, 222, "Right: switch   Left: confirm", 0);
}

/* ── Screen: CHOICE_RESULT ──────────────────────────────────────── */
static void draw_choice_result(const mg_run_state_t *r)
{
    const mg_event_t *ev = &mg_events[r->event_index];
    int choice = r->last_choice;
    if (choice < 0 || choice > 1) choice = 0;
    const mg_choice_t *ch = &ev->choices[choice];
    char buf[64];

    mg_fill(0, 0, RLCD_WIDTH, RLCD_HEIGHT, 255);
    mg_stroke(2, 2, 396, 296, 0);
    mg_stroke(6, 6, 388, 288, 0);

    mg_fill(0, 0, RLCD_WIDTH, 26, 0);
    mg_cn_cx(200, 5, "选择结果", 255);

    snprintf(buf, sizeof(buf), "Day %d / %d", r->day_current, r->days_total);
    mg_ascii(14, 36, buf, 0);
    mg_cn(14, 58, ev->title, 0);

    mg_hline(14, 82, 372, 0);
    mg_cn(28, 96, ch->tag, 0);
    mg_cn(28, 118, ch->text, 0);

    mg_hline(14, 148, 372, 0);
    mg_ascii(28, 162, "CHANGE:", 0);

    int y = 184;
    draw_delta_line(&y, "HP", r->last_hp_delta);
    if (r->last_hpmax_delta != 0) {
        snprintf(buf, sizeof(buf), "MaxHP %+d", r->last_hpmax_delta);
        mg_ascii(92, y, buf, 0);  y += 16;
    }
    draw_delta_line(&y, "MP", r->last_mp_delta);
    draw_delta_line(&y, "EN", r->last_en_delta);
    draw_delta_line(&y, "QI", r->last_qi_delta);
    draw_delta_line(&y, "Shard", r->last_shard_delta);
    if (r->last_unlocked_move >= 0) {
        snprintf(buf, sizeof(buf), "Move unlocked: %s", move_name(r->last_unlocked_move));
        mg_ascii(92, y, buf, 0);
        y += 16;
    }
    if (y == 184) {
        mg_cn(92, y, "没有明显变化", 0);
    }

    mg_hline(30, 252, 340, 0);
    mg_ascii_cx(200, 266, "Left: continue", 0);
}

/* ── Screen: ENCOUNTER ───────────────────────────────────────────── */
static void draw_encounter(const mg_run_state_t *r)
{
    const mg_event_t *ev = &mg_events[r->event_index];

    mg_fill(0, 0, RLCD_WIDTH, RLCD_HEIGHT, 255);

    /* Heavy outer frame */
    for (int t = 0; t < 4; t++)
        mg_stroke(t, t, RLCD_WIDTH - t * 2, RLCD_HEIGHT - t * 2, 0);

    /* Horizontal drama lines */
    mg_fill(0, 52, RLCD_WIDTH, 4, 0);
    mg_fill(0, 244, RLCD_WIDTH, 4, 0);

    /* Alert box  300×88  centred */
    const int bx = 50, by = 106, bw = 300, bh = 88;
    mg_fill(bx, by, bw, bh, 0);
    mg_stroke(bx - 2, by - 2, bw + 4, bh + 4, 0);  /* double border */

    /* "遭遇战！" */
    mg_cn_cx(200, by + 4, "遭遇战！", 255);
    /* Enemy name */
    mg_cn_cx(200, by + 28, ev->title, 255);
    /* Sub-label */
    mg_ascii_cx(200, by + 52, "White Day Dream Battle!", 255);
    mg_ascii_cx(200, by + 66, "Prepare...", 255);

    /* Knight art, left and right flanking */
    draw_knight(100, 176);
    draw_knight(300, 176);

    /* Bottom caption */
    mg_ascii_cx(200, 264, "Dream combat imminent...", 0);
}

/* ── Screen: BATTLE ──────────────────────────────────────────────── */
static void draw_battle(const mg_run_state_t *r)
{
    const mg_battle_t *b = &r->battle;
    const mg_player_t *p = &r->player;
    char buf[64];

    mg_fill(0, 0, RLCD_WIDTH, RLCD_HEIGHT, 255);

    /* Header bar (inverted) */
    mg_fill(0, 0, RLCD_WIDTH, 20, 0);
    snprintf(buf, sizeof(buf), "Round %d", b->round);
    mg_ascii(6, 7, buf, 255);
    mg_cn_cx(200, 2, "豆豆 vs 霓虹骑士", 255);

    mg_hline(0, 20, RLCD_WIDTH, 0);
    mg_hline(0, 21, RLCD_WIDTH, 0);

    /* VS area divider */
    mg_vline(199, 22, 96, 0);
    mg_vline(200, 22, 96, 0);

    /* Left: 豆豆 */
    mg_cn(6, 24, "豆豆", 0);
    draw_doudou(90, 72);

    /* Right: enemy */
    mg_cn(212, 24, b->enemy.name, 0);
    draw_knight(295, 72);

    mg_hline(0, 118, RLCD_WIDTH, 0);

    /* HP bars row */
    mg_ascii(4, 122, "HP", 0);
    snprintf(buf, sizeof(buf), "%d/%d", p->hp, p->hp_max);
    mg_ascii(20, 122, buf, 0);
    mg_bar(4, 134, 186, 9, p->hp, p->hp_max);

    mg_ascii(206, 122, "HP", 0);
    snprintf(buf, sizeof(buf), "%d/%d", b->enemy_hp, b->enemy.hp);
    mg_ascii(222, 122, buf, 0);
    mg_bar(206, 134, 186, 9, b->enemy_hp, b->enemy.hp);

    /* QI bar (player only) */
    mg_ascii(4, 147, "QI", 0);
    snprintf(buf, sizeof(buf), "%d", p->qi);
    mg_ascii(20, 147, buf, 0);
    mg_bar(4, 159, 186, 6, p->qi, 100);

    mg_hline(0, 169, RLCD_WIDTH, 0);
    mg_hline(0, 170, RLCD_WIDTH, 0);

    /* Battle log (3 lines, CN16) */
    for (int i = 0; i < MG_BATTLE_LOG_LINES; i++) {
        int idx = (b->log_head - MG_BATTLE_LOG_LINES + i + 100 * MG_BATTLE_LOG_LINES)
                  % MG_BATTLE_LOG_LINES;
        if (b->log[idx][0]) {
            font_draw_utf8(s_buf, RLCD_WIDTH, 6, 173 + i * 17, b->log[idx], 0);
        }
    }

    mg_hline(0, 226, RLCD_WIDTH, 0);

    /* Status / result */
    if (b->battle_over) {
        if (b->player_won) {
            mg_fill(0, 228, RLCD_WIDTH, 26, 0);
            mg_cn_cx(200, 233, "豆豆获胜！梦境碎片+5", 255);
        } else {
            mg_cn_cx(200, 233, "豆豆倒下了...", 0);
        }
        mg_ascii_cx(200, 264, "Left: confirm result", 0);
        return;
    } else {
        mg_ascii_cx(200, 234, "Auto battle in progress...", 0);
    }

    /* Ultimate banner (if fired this round — check most recent log) */
    bool ult = false;
    for (int i = 0; i < MG_BATTLE_LOG_LINES; i++) {
        if (b->log[i][0] && strstr(b->log[i], "大招")) {
            ult = true;
            break;
        }
    }
    if (ult && !b->battle_over) {
        mg_fill(80, 258, 240, 22, 0);
        mg_ascii_cx(200, 264, "** Daydream Strike!! **", 255);
    } else {
        snprintf(buf, sizeof(buf), "ATK:%d  DEF:%d  Shard:%d",
                 p->atk, p->def, p->shard);
        mg_ascii_cx(200, 264, buf, 0);
    }
}

/* ── Screen: LEVEL_UP ────────────────────────────────────────────── */
static const char *s_upg_name[MG_UPGRADE_COUNT] = {
    "强化拳法",   /* ATK_PLUS   */
    "体魄强化",   /* HPMAX_PLUS */
    "梦境续命",   /* HEAL_FULL  */
    "连击本能",   /* PERIODIC   */
    "铠甲强化",   /* DEF_PLUS   */
};
static const char *s_upg_desc[MG_UPGRADE_COUNT] = {
    " ATK +5",
    " MaxHP+30",
    " HP Full",
    " /3rd +hit",
    " DEF +3",
};

static void draw_level_up(const mg_run_state_t *r)
{
    const mg_player_t *p = &r->player;
    char buf[48];

    mg_fill(0, 0, RLCD_WIDTH, RLCD_HEIGHT, 255);
    mg_stroke(2, 2, 396, 296, 0);
    mg_stroke(6, 6, 388, 288, 0);

    font_draw_star(s_buf, RLCD_WIDTH, 24, 22, 0);
    font_draw_star(s_buf, RLCD_WIDTH, 362, 22, 0);
    mg_cn_cx(200, 18, "梦境升级", 0);
    snprintf(buf, sizeof(buf), "Lv.%d  -> Lv.%d  Choose your power!", p->level, p->level + 1);
    mg_ascii_cx(200, 42, buf, 0);
    mg_hline(16, 60, 368, 0);

    /* 3 option cards (stacked vertically) */
    for (int i = 0; i < 3; i++) {
        mg_upgrade_t upg  = r->upgrade_choices[i];
        int           ry   = 70 + i * 60;   /* top of card */
        bool          sel  = (r->sel == i);
        uint8_t       bg   = sel ? 0   : 255;
        uint8_t       fg   = sel ? 255 : 0;

        mg_fill(16, ry, 368, 52, bg);
        mg_stroke(16, ry, 368, 52, sel ? 255 : 0);

        /* selector arrow */
        if (sel) mg_ascii(24, ry + 18, ">", fg);

        /* upgrade name (CN16) + desc (ASCII8) on the same baseline */
        mg_cn   (44, ry + 18, s_upg_name[upg], fg);
        mg_ascii(44 + 64 + 4, ry + 22, s_upg_desc[upg], fg);  /* 4 CN chars = 64px */
    }

    mg_hline(16, 252, 368, 0);
    snprintf(buf, sizeof(buf), "HP %d/%d  ATK %d  DEF %d  Shard %d",
             p->hp, p->hp_max, p->atk, p->def, p->shard);
    mg_ascii_cx(200, 264, buf, 0);
    mg_ascii_cx(200, 280, "Right: move   Left: pick", 0);
}

/* ── Screen: WIN_DAY ─────────────────────────────────────────────── */
static void draw_win_day(const mg_run_state_t *r)
{
    const mg_player_t *p = &r->player;
    char buf[48];

    mg_fill(0, 0, RLCD_WIDTH, RLCD_HEIGHT, 255);
    mg_stroke(2, 2, 396, 296, 0);
    mg_stroke(6, 6, 388, 288, 0);

    font_draw_star(s_buf, RLCD_WIDTH, 24, 28, 0);
    font_draw_star(s_buf, RLCD_WIDTH, 360, 28, 0);

    snprintf(buf, sizeof(buf), "Day %d  Clear!", r->day_current - 1);
    mg_ascii2x_cx(200, 44, buf, 0);

    mg_cn_cx(200, 76, "通关！继续前进", 0);

    mg_hline(30, 108, 340, 0);

    /* Stats */
    snprintf(buf, sizeof(buf), "HP : %d / %d", p->hp, p->hp_max);
    mg_ascii_cx(200, 124, buf, 0);
    snprintf(buf, sizeof(buf), "QI : %d      ATK : %d", p->qi, p->atk);
    mg_ascii_cx(200, 138, buf, 0);
    snprintf(buf, sizeof(buf), "Shard : %d", p->shard);
    mg_ascii_cx(200, 152, buf, 0);

    /* Moves */
    if (p->moves[MG_MOVE_DOUBLE_STRIKE]) mg_ascii_cx(200, 172, "[DOUBLE STRIKE unlocked]", 0);

    mg_hline(30, 195, 340, 0);
    mg_ascii_cx(200, 248, "Left: continue to next day", 0);
}

/* ── Screen: WIN_GAME ────────────────────────────────────────────── */
static void draw_win_game(const mg_run_state_t *r)
{
    const mg_player_t *p = &r->player;
    char buf[48];

    mg_fill(0, 0, RLCD_WIDTH, RLCD_HEIGHT, 255);
    for (int t = 0; t < 4; t++)
        mg_stroke(t, t, RLCD_WIDTH - t * 2, RLCD_HEIGHT - t * 2, 0);

    /* Stars scattered */
    font_draw_star(s_buf, RLCD_WIDTH, 24, 28, 0);
    font_draw_star(s_buf, RLCD_WIDTH, 46, 18, 0);
    font_draw_star(s_buf, RLCD_WIDTH, 340, 28, 0);
    font_draw_star(s_buf, RLCD_WIDTH, 362, 18, 0);
    font_draw_sparkle(s_buf, RLCD_WIDTH, 192, 14, 0);
    font_draw_sparkle(s_buf, RLCD_WIDTH, 210, 14, 0);

    mg_cn_cx(200, 36, "白日梦大冒险", 0);
    mg_ascii2x_cx(200, 66, "ALL CLEAR!", 0);

    mg_cn_cx(200, 96, "豆豆从梦中醒来", 0);
    mg_ascii_cx(200, 120, "The adventure ends safely.", 0);

    mg_hline(30, 140, 340, 0);

    snprintf(buf, sizeof(buf), "Final HP  : %d / %d", p->hp, p->hp_max);
    mg_ascii_cx(200, 156, buf, 0);
    snprintf(buf, sizeof(buf), "Dream Shards : %d", p->shard);
    mg_ascii_cx(200, 170, buf, 0);
    snprintf(buf, sizeof(buf), "Days cleared : %d", r->day_current - 1);
    mg_ascii_cx(200, 184, buf, 0);

    mg_ascii_cx(200, 252, "Left: return to Doudou", 0);
}

/* ── Screen: GAME_OVER ───────────────────────────────────────────── */
static void draw_game_over(const mg_run_state_t *r)
{
    char buf[48];

    /* All-black for drama */
    mg_fill(0, 0, RLCD_WIDTH, RLCD_HEIGHT, 0);

    /* White double frame */
    mg_stroke(4, 4, 392, 292, 255);
    mg_stroke(8, 8, 384, 284, 255);

    mg_cn_cx(200, 56, "豆豆倒下了", 255);
    mg_ascii2x_cx(200, 86, "GAME OVER", 255);
    mg_ascii_cx(200, 116, "The daydream ends here...", 255);

    mg_hline(40, 140, 320, 255);

    snprintf(buf, sizeof(buf), "Reached Day %d", r->day_current);
    mg_ascii_cx(200, 156, buf, 255);
    snprintf(buf, sizeof(buf), "Dream Shards : %d", r->player.shard);
    mg_ascii_cx(200, 170, buf, 255);

    mg_ascii_cx(200, 248, "Left: return to Doudou", 255);
}

/* ── Public API ──────────────────────────────────────────────────── */

esp_err_t mg_render_init(void)
{
    s_buf = heap_caps_calloc(1, RLCD_WIDTH * RLCD_HEIGHT, MALLOC_CAP_SPIRAM);
    if (!s_buf) {
        ESP_LOGE(TAG, "Failed to alloc minigame graybuf (120KB PSRAM)");
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "Minigame renderer ready (%d bytes PSRAM)", RLCD_WIDTH * RLCD_HEIGHT);
    return ESP_OK;
}

void mg_render_frame(void)
{
    if (!s_buf) return;
    const mg_run_state_t *r = mg_get_run();

    memset(s_buf, 255, RLCD_WIDTH * RLCD_HEIGHT);

    switch (r->screen) {
    case MG_SCREEN_DIFFICULTY:  draw_difficulty(r);  break;
    case MG_SCREEN_STORY_EVENT: draw_story_event(r); break;
    case MG_SCREEN_CHOICE_RESULT: draw_choice_result(r); break;
    case MG_SCREEN_ENCOUNTER:   draw_encounter(r);   break;
    case MG_SCREEN_BATTLE:      draw_battle(r);      break;
    case MG_SCREEN_LEVEL_UP:    draw_level_up(r);    break;
    case MG_SCREEN_WIN_DAY:     draw_win_day(r);     break;
    case MG_SCREEN_WIN_GAME:    draw_win_game(r);    break;
    case MG_SCREEN_GAME_OVER:   draw_game_over(r);   break;
    default:                                          break;
    }

    mg_flush();
}

/* Draw a menu label without allowing a long SSID to cross the panel border. */
static void mg_draw_menu_label_clipped(uint8_t *graybuf, int x, int y,
                                       int max_width, const char *label,
                                       uint8_t color)
{
    if (!label || max_width <= 0) return;
    if (font_utf8_width(label) <= max_width) {
        font_draw_utf8(graybuf, RLCD_WIDTH, x, y, label, color);
        return;
    }

    /* Keep whole UTF-8 code points and reserve room for an ASCII ellipsis. */
    char clipped[128];
    size_t out = 0;
    int used = 0;
    const int ellipsis_width = 27; /* three 8x16 ASCII glyphs plus gaps */
    int content_width = max_width > ellipsis_width ? max_width - ellipsis_width : 0;
    const unsigned char *p = (const unsigned char *)label;
    while (*p && out + 4 < sizeof(clipped) && used < content_width) {
        size_t char_len = (*p < 0x80) ? 1 : ((*p & 0xE0) == 0xC0 ? 2 :
                            ((*p & 0xF0) == 0xE0 ? 3 : 4));
        int char_width = (*p < 0x80) ? 9 : 17;
        if (used + char_width > content_width) break;
        memcpy(clipped + out, p, char_len);
        out += char_len;
        used += char_width;
        p += char_len;
    }
    memcpy(clipped + out, "...", 4);
    font_draw_utf8(graybuf, RLCD_WIDTH, x, y, clipped, color);
}

/* Dessert effects use two short rows so all four stat axes stay readable. */
static void mg_draw_effect_lines(uint8_t *graybuf, int x, int y,
                                 int max_width, const char *effect,
                                 uint8_t color)
{
    if (!effect || !*effect) return;
    const char *line = effect;
    for (int row = 0; row < 2 && *line; row++) {
        const char *end = strchr(line, '\n');
        size_t len = end ? (size_t)(end - line) : strlen(line);
        char text[96];
        if (len >= sizeof(text)) len = sizeof(text) - 1;
        memcpy(text, line, len);
        text[len] = '\0';
        mg_draw_menu_label_clipped(graybuf, x, y + row * 16,
                                   max_width, text, color);
        if (!end) break;
        line = end + 1;
    }
}

/* ── Main-menu overlay (drawn on the renderer's own graybuf) ─────── */
void mg_draw_main_menu(uint8_t *graybuf, int sel, bool volume_mode,
                       const char *header, const char *const *opts,
                       int opt_count)
{
    const int item_count = opt_count > 0 ? opt_count : (volume_mode ? 3 : 5);
    /* Submenus include saved SSIDs, so give them room to be identifiable. */
    /* Keep the main menu fully inside the 400px display while leaving enough
     * width for "Language" and other ASCII labels without ellipsizing them. */
    const int bx = volume_mode ? 222 : 270;
    const int by = volume_mode ? 74 : 48;
    const int bw = volume_mode ? 172 : 118;
    const int bh = 24 + item_count * 24 + 8;

    /* White box background */
    for (int y = by; y < by + bh; y++)
        memset(graybuf + y * RLCD_WIDTH + bx, 255, bw);

    /* 2-px black border */
    for (int y = by; y < by + bh; y++) {
        for (int x = bx; x < bx + bw; x++) {
            if (y < by + 2 || y >= by + bh - 2 ||
                x < bx + 2 || x >= bx + bw - 2)
                graybuf[y * RLCD_WIDTH + x] = 0;
        }
    }

    /* Inverted header. */
    for (int y = by + 2; y < by + 20; y++)
        memset(graybuf + y * RLCD_WIDTH + bx + 2, 0, bw - 4);
    font_draw_utf8(graybuf, RLCD_WIDTH, bx + 4, by + 2,
                   header ? header : (volume_mode ? "音量设置" : "菜单"), 255);
    if (sel < 0) sel = 0;
    if (sel >= item_count) sel = item_count - 1;

    for (int i = 0; i < item_count; i++) {
        int oy = by + 22 + i * 24;
        const char *label = (opts && opts[i]) ? opts[i] : "";
        if (i == sel) {
            for (int y = oy; y < oy + 22; y++)
                memset(graybuf + y * RLCD_WIDTH + bx + 2, 0, bw - 4);
            font_draw_string(graybuf, RLCD_WIDTH, bx + 6,  oy + 8, ">", 255);
            mg_draw_menu_label_clipped(graybuf, bx + 18, oy + 3, bw - 24, label, 255);
        } else {
            mg_draw_menu_label_clipped(graybuf, bx + 18, oy + 3, bw - 24, label, 0);
        }
    }
}

/* ── 服装店：设备可落地版 ───────────────────────────────────────────
 *
 * Reference art wants a dense shop grid, but the real 400x300 1-bit panel
 * needs fewer decisions per screen. This keeps a focused "try-on + product
 * sheet" layout while borrowing the shop-like chrome: section title, carousel
 * arrows, receipt-style price rows, and a clear action strip.
 * ----------------------------------------------------------------- */
void mg_draw_shop_panel(uint8_t *graybuf, int sel, int coins, bool owned,
                        const char *name, int price, const char *tag,
                        const char *title, const char *currency,
                        const char *owned_label, const char *new_label,
                        const char *price_label, const char *style_label,
                        const char *status_label, const char *owned_status,
                        const char *not_owned_status, const char *balance_label,
                        const char *after_buy_label, const char *short_label,
                        const char *not_enough_label, const char *wear_label,
                        const char *buy_label, const char *footer)
{
    for (int y = 0; y < RLCD_HEIGHT; y++)
        memset(graybuf + y * RLCD_WIDTH, 0xFE, RLCD_WIDTH);

    if (sel < 0) sel = 0;
    if (sel > 5) sel = 5;
    if (!name) name = "?";
    if (!tag) tag = "-";

    /* Top status bar. */
    gb_fill(graybuf, 0, 0, RLCD_WIDTH, 24, 0x00);
    font_draw_string(graybuf, RLCD_WIDTH, 8, 8, "* Aura", 0xFF);
    font_draw_utf8(graybuf, RLCD_WIDTH, 164, 4, title ? title : "服装商店", 0xFF);
    char coin_buf[24];
    snprintf(coin_buf, sizeof(coin_buf), "%d %s", coins, currency ? currency : "豆");
    font_draw_utf8(graybuf, RLCD_WIDTH, 318, 4, coin_buf, 0xFF);
    gb_hline(graybuf, 0, 25, RLCD_WIDTH, 0x00);

    /* Left preview room. Character is drawn by caller after this panel. */
    const int px = 4, py = 30, pw = 198, ph = 244;
    gb_stroke(graybuf, px, py, pw, ph, 0x00);
    gb_stroke(graybuf, px + 3, py + 3, pw - 6, ph - 6, 0x00);
    gb_corner_marks(graybuf, px + 8, py + 8, pw - 16, ph - 16, 0x00);
    /* Small carousel hints, matching the single-button navigation model. */
    font_draw_string(graybuf, RLCD_WIDTH, 202, 145, ">", 0x00);

    /* Right product receipt card. */
    const int cx = 210, cy = 36, cw = 184, ch = 234;
    gb_stroke(graybuf, cx, cy, cw, ch, 0x00);
    gb_corner_marks(graybuf, cx + 4, cy + 4, cw - 8, ch - 8, 0x00);
    gb_fill(graybuf, cx + 1, cy + 1, cw - 2, 27, 0x00);
    char page_buf[24];
    snprintf(page_buf, sizeof(page_buf), "%d/6", sel + 1);
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 10, cy + 5,
                   owned ? (owned_label ? owned_label : "已拥有") : (new_label ? new_label : "新品推荐"),
                   0xFF);
    font_draw_string(graybuf, RLCD_WIDTH, cx + 147, cy + 10, page_buf, 0xFF);

    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 16, cy + 46, name, 0x00);
    gb_dashed_hline(graybuf, cx + 14, cy + 70, cw - 28, 0x00);

    char price_buf[32];
    snprintf(price_buf, sizeof(price_buf), "%s      %d %s",
             price_label ? price_label : "价格", price, currency ? currency : "豆");
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 14, cy + 84, price_buf, 0x00);

    char tag_buf[32];
    snprintf(tag_buf, sizeof(tag_buf), "%s      %s",
             style_label ? style_label : "风格", tag);
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 14, cy + 110, tag_buf, 0x00);

    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 14, cy + 134,
                   status_label ? status_label : "状态", 0x00);
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 82, cy + 134,
                   owned ? (owned_status ? owned_status : "已拥有") : (not_owned_status ? not_owned_status : "未购买"),
                   0x00);

    gb_dashed_hline(graybuf, cx + 14, cy + 156, cw - 28, 0x00);
    char balance_buf[40];
    if (owned) {
        snprintf(balance_buf, sizeof(balance_buf), "%s      %d %s",
                 balance_label ? balance_label : "余额", coins, currency ? currency : "豆");
    } else if (coins >= price) {
        snprintf(balance_buf, sizeof(balance_buf), "%s    %d %s",
                 after_buy_label ? after_buy_label : "购买后", coins - price, currency ? currency : "豆");
    } else {
        snprintf(balance_buf, sizeof(balance_buf), "%s      %d %s",
                 short_label ? short_label : "还差", price - coins, currency ? currency : "豆");
    }
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 14, cy + 168, balance_buf, 0x00);

    if (!owned && coins < price) {
        gb_stroke(graybuf, cx + 12, cy + 194, cw - 24, 28, 0x00);
        font_draw_utf8(graybuf, RLCD_WIDTH, cx + 34, cy + 200,
                       not_enough_label ? not_enough_label : "豆豆不够", 0x00);
    } else {
        gb_fill(graybuf, cx + 12, cy + 194, cw - 24, 28, 0x00);
        font_draw_utf8(graybuf, RLCD_WIDTH, cx + 44, cy + 200,
                       owned ? (wear_label ? wear_label : "左键穿上") : (buy_label ? buy_label : "左键购买"),
                       0xFF);
    }

    gb_fill(graybuf, 0, 280, RLCD_WIDTH, 20, 0x00);
    font_draw_utf8(graybuf, RLCD_WIDTH, 8, 284,
                   footer ? footer : "右键翻页  左键确认  左键长按退出", 0xFF);
}

void mg_draw_dessert_panel(uint8_t *graybuf, int sel, int item_count, int coins,
                           const char *name, int price, const char *effect,
                           const char *title, const char *currency,
                           const char *price_label, const char *effect_label,
                           const char *balance_label, const char *buy_label,
                           const char *not_enough_label, const char *pending_label,
                           bool pending, const char *footer)
{
    for (int y = 0; y < RLCD_HEIGHT; y++)
        memset(graybuf + y * RLCD_WIDTH, 0xFE, RLCD_WIDTH);
    if (item_count < 1) item_count = 1;
    if (sel < 0) sel = 0;
    if (sel >= item_count) sel = item_count - 1;
    if (!name) name = "?";
    if (!effect) effect = "-";

    gb_fill(graybuf, 0, 0, RLCD_WIDTH, 24, 0x00);
    font_draw_string(graybuf, RLCD_WIDTH, 8, 8, "* Aura", 0xFF);
    font_draw_utf8(graybuf, RLCD_WIDTH, 160, 4, title ? title : "甜品店", 0xFF);
    char coin_buf[24];
    snprintf(coin_buf, sizeof(coin_buf), "%d %s", coins, currency ? currency : "豆");
    font_draw_utf8(graybuf, RLCD_WIDTH, 318, 4, coin_buf, 0xFF);
    gb_hline(graybuf, 0, 25, RLCD_WIDTH, 0x00);

    const int px = 4, py = 30, pw = 198, ph = 244;
    gb_stroke(graybuf, px, py, pw, ph, 0x00);
    gb_stroke(graybuf, px + 3, py + 3, pw - 6, ph - 6, 0x00);
    gb_corner_marks(graybuf, px + 8, py + 8, pw - 16, ph - 16, 0x00);
    /* Effects are already listed in the detail column. Keep this card for
     * the selected dessert only instead of repeating decorative copy. */
    draw_dessert_asset(graybuf, sel, px + 20, py + 18, pw - 40, ph - 36);
    font_draw_string(graybuf, RLCD_WIDTH, 202, 145, ">", 0x00);

    const int cx = 210, cy = 36, cw = 184, ch = 234;
    gb_stroke(graybuf, cx, cy, cw, ch, 0x00);
    gb_corner_marks(graybuf, cx + 4, cy + 4, cw - 8, ch - 8, 0x00);
    gb_fill(graybuf, cx + 1, cy + 1, cw - 2, 27, 0x00);
    char page_buf[24];
    snprintf(page_buf, sizeof(page_buf), "%d/%d", sel + 1, item_count);
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 10, cy + 5, "MENU", 0xFF);
    font_draw_string(graybuf, RLCD_WIDTH, cx + 147, cy + 10, page_buf, 0xFF);
    mg_draw_menu_label_clipped(graybuf, cx + 14, cy + 46, cw - 28, name, 0x00);
    gb_dashed_hline(graybuf, cx + 14, cy + 70, cw - 28, 0x00);
    char price_buf[32];
    snprintf(price_buf, sizeof(price_buf), "%s  %d %s",
             price_label ? price_label : "价格", price, currency ? currency : "豆");
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 14, cy + 84, price_buf, 0x00);
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 14, cy + 112,
                   effect_label ? effect_label : "效果", 0x00);
    mg_draw_effect_lines(graybuf, cx + 14, cy + 128, cw - 28,
                         effect, 0x00);
    gb_dashed_hline(graybuf, cx + 14, cy + 162, cw - 28, 0x00);
    char balance_buf[40];
    snprintf(balance_buf, sizeof(balance_buf), "%s  %d %s",
             balance_label ? balance_label : "余额", coins, currency ? currency : "豆");
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 14, cy + 168, balance_buf, 0x00);
    if (pending) {
        gb_stroke(graybuf, cx + 12, cy + 194, cw - 24, 28, 0x00);
        font_draw_utf8(graybuf, RLCD_WIDTH, cx + 34, cy + 200,
                       pending_label ? pending_label : "处理中...", 0x00);
    } else if (coins < price) {
        gb_stroke(graybuf, cx + 12, cy + 194, cw - 24, 28, 0x00);
        font_draw_utf8(graybuf, RLCD_WIDTH, cx + 26, cy + 200,
                       not_enough_label ? not_enough_label : "豆豆不够", 0x00);
    } else {
        gb_fill(graybuf, cx + 12, cy + 194, cw - 24, 28, 0x00);
        font_draw_utf8(graybuf, RLCD_WIDTH, cx + 44, cy + 200,
                       buy_label ? buy_label : "购买", 0xFF);
    }
    gb_fill(graybuf, 0, 280, RLCD_WIDTH, 20, 0x00);
    font_draw_utf8(graybuf, RLCD_WIDTH, 8, 284,
                   footer ? footer : "右键翻页  左键购买  长按返回", 0xFF);
}

/* ── 衣柜：与商店统一的大预览详情页，只浏览已拥有服装 ───────────── */
void mg_draw_wardrobe_panel(uint8_t *graybuf, int outfit_idx,
                            int page, int page_count, const char *name,
                            const char *title, const char *owned_label,
                            const char *id_label, const char *status_label,
                            const char *wearable_label, const char *source_label,
                            const char *wardrobe_label, const char *wear_label,
                            const char *footer)
{
    for (int y = 0; y < RLCD_HEIGHT; y++)
        memset(graybuf + y * RLCD_WIDTH, 0xFE, RLCD_WIDTH);

    if (!name) name = "?";
    if (page_count < 1) page_count = 1;
    if (page < 0) page = 0;
    if (page >= page_count) page = page_count - 1;

    gb_fill(graybuf, 0, 0, RLCD_WIDTH, 24, 0x00);
    font_draw_string(graybuf, RLCD_WIDTH, 8, 8, "* Aura", 0xFF);
    font_draw_utf8(graybuf, RLCD_WIDTH, 164, 4, title ? title : "我的衣柜", 0xFF);
    char page_buf[24];
    snprintf(page_buf, sizeof(page_buf), "%d/%d", page + 1, page_count);
    font_draw_string(graybuf, RLCD_WIDTH, 338, 10, page_buf, 0xFF);
    gb_hline(graybuf, 0, 25, RLCD_WIDTH, 0x00);

    const int px = 4, py = 30, pw = 198, ph = 244;
    gb_stroke(graybuf, px, py, pw, ph, 0x00);
    gb_stroke(graybuf, px + 3, py + 3, pw - 6, ph - 6, 0x00);
    gb_corner_marks(graybuf, px + 8, py + 8, pw - 16, ph - 16, 0x00);
    font_draw_string(graybuf, RLCD_WIDTH, 202, 145, ">", 0x00);

    const int cx = 210, cy = 36, cw = 184, ch = 234;
    gb_stroke(graybuf, cx, cy, cw, ch, 0x00);
    gb_corner_marks(graybuf, cx + 4, cy + 4, cw - 8, ch - 8, 0x00);
    gb_fill(graybuf, cx + 1, cy + 1, cw - 2, 27, 0x00);
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 10, cy + 5,
                   owned_label ? owned_label : "已拥有", 0xFF);
    font_draw_string(graybuf, RLCD_WIDTH, cx + 147, cy + 10, page_buf, 0xFF);

    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 16, cy + 48, name, 0x00);
    gb_dashed_hline(graybuf, cx + 14, cy + 74, cw - 28, 0x00);
    char idx_buf[32];
    snprintf(idx_buf, sizeof(idx_buf), "%s      %02d",
             id_label ? id_label : "编号", outfit_idx);
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 14, cy + 92, idx_buf, 0x00);
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 14, cy + 122,
                   status_label ? status_label : "状态", 0x00);
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 82, cy + 122,
                   wearable_label ? wearable_label : "可穿戴", 0x00);
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 14, cy + 152,
                   source_label ? source_label : "来源", 0x00);
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 82, cy + 152,
                   wardrobe_label ? wardrobe_label : "衣柜", 0x00);
    gb_dashed_hline(graybuf, cx + 14, cy + 176, cw - 28, 0x00);
    gb_fill(graybuf, cx + 12, cy + 202, cw - 24, 28, 0x00);
    font_draw_utf8(graybuf, RLCD_WIDTH, cx + 44, cy + 208,
                   wear_label ? wear_label : "左键穿上", 0xFF);

    gb_fill(graybuf, 0, 280, RLCD_WIDTH, 20, 0x00);
    font_draw_utf8(graybuf, RLCD_WIDTH, 8, 284,
                   footer ? footer : "右键换衣  左键穿上  左键长按返回", 0xFF);
}
