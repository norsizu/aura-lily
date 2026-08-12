/**
 * 渲染器实现 — 分层合成到 ST7305 1-bit 帧缓冲
 *
 * 渲染层次（从底到顶）：
 * 1. 场景背景基底
 * 2. 角色立绘（从 Atlas 提取，白色=透明）
 * 3. UI 元素（状态条、面板）
 * 4. 对话文本
 *
 * Floyd-Steinberg dither → rlcd_set_pixel 写入 ST7305 block 布局
 */
#include "renderer.h"
#include "aura_config.h"
#include "rlcd_driver.h"
#include "atlas.h"
#include "layout.h"
#include "status_bar.h"
#include "panels.h"
#include "panels_info_board.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include <string.h>
#include <stdlib.h>
#include <math.h>
#include <stdio.h>

static const char *TAG = "renderer";

/* --- Bayer ordered-dither tables (pixel is black when luma <= threshold) --- */
static const uint8_t s_bayer4[4][4] = {
    {   8, 136,  40, 168 },
    { 200,  72, 232, 104 },
    {  56, 184,  24, 152 },
    { 248, 120, 216,  88 },
};
static const uint8_t s_bayer8[8][8] = {
    {   2, 130,  34, 162,  10, 138,  42, 170 },
    { 194,  66, 226,  98, 202,  74, 234, 106 },
    {  50, 178,  18, 146,  58, 186,  26, 154 },
    { 242, 114, 210,  82, 250, 122, 218,  90 },
    {  14, 142,  46, 174,   6, 134,  38, 166 },
    { 206,  78, 238, 110, 198,  70, 230, 102 },
    {  62, 190,  30, 158,  54, 182,  22, 150 },
    { 254, 126, 222,  94, 246, 118, 214,  86 },
};

/* --- Gamma-1.6 lookup table (computed once at init) --- */
static uint8_t s_gamma_lut[256];

/* --- Active dither mode --- */
static render_dither_mode_t s_dither_mode = DITHER_HYBRID_INK;

/* --- Character vertical offset: +25px confirmed good --- */
static int s_char_y_offset = 25;

static const char *s_dither_names[DITHER_MODE_COUNT] = {
    "FS Floyd",
    "Thresh128",
    "Bayer 4x4",
    "Bayer 8x8",
    "Hybrid",
    "Hybrid Ink",
};

#define RENDERER_ENABLE_FLOYD_DITHER 0
#if RENDERER_ENABLE_FLOYD_DITHER
#define DITHER_BLACK_LOCK_THRESHOLD 8
#define DITHER_WHITE_LOCK_THRESHOLD 245
#endif

// 帧缓冲
static uint8_t *s_graybuf = NULL;     // 灰度工作缓冲 (400*300 bytes)
static uint8_t *s_framebuf = NULL;    // ST7305 block 格式 1-bit 输出 (15000 bytes)
static uint8_t *s_scene_buf = NULL;   // 场景缓存，避免动画时反复读 SPIFFS

// Atlas 缓存
static atlas_t s_atlas = {0};
static uint8_t *s_pose_buf = NULL;
static int16_t *s_char_err_a = NULL;
static int16_t *s_char_err_b = NULL;
static int s_loaded_outfit = -1;
static int s_loaded_scene = -1;
#define CUSTOM_OUTFIT_CACHE_TAG (-1000)
static char s_loaded_custom_path[128] = {0};

static void fill_fallback_background(uint8_t *buf)
{
    for (int y = 0; y < RLCD_HEIGHT; y++) {
        uint8_t val = (uint8_t)(255 - (y * 200 / RLCD_HEIGHT));
        memset(buf + y * RLCD_WIDTH, val, RLCD_WIDTH);
    }
}

static void ensure_scene_loaded(int scene)
{
    const char *bg_files[] = {
        ASSETS_BASE_PATH "/scenes/home_living_room.bin",
        ASSETS_BASE_PATH "/scenes/home_study.bin",
        ASSETS_BASE_PATH "/scenes/home_bedroom.bin",
        ASSETS_BASE_PATH "/scenes/home_kitchen.bin",
        ASSETS_BASE_PATH "/scenes/home_balcony.bin",
        ASSETS_BASE_PATH "/scenes/street.bin",
        ASSETS_BASE_PATH "/scenes/outside_cafe.bin",
        ASSETS_BASE_PATH "/scenes/outside_shop.bin",
        ASSETS_BASE_PATH "/scenes/outside_park.bin",
        ASSETS_BASE_PATH "/scenes/outside_mall.bin",
    };

    if (!s_scene_buf) return;
    if (scene < 0 || scene >= (int)(sizeof(bg_files) / sizeof(bg_files[0]))) scene = 0;
    if (scene == s_loaded_scene) return;

    FILE *f = fopen(bg_files[scene], "rb");
    if (!f) {
        const char *legacy_fallback = scene == 1
            ? ASSETS_BASE_PATH "/scenes/study.bin"
            : (scene == 2 ? ASSETS_BASE_PATH "/scenes/bedroom.bin"
                          : ASSETS_BASE_PATH "/scenes/living_room.bin");
        ESP_LOGW(TAG, "Scene not found: %s; using %s", bg_files[scene], legacy_fallback);
        f = fopen(legacy_fallback, "rb");
        if (!f) {
            fill_fallback_background(s_scene_buf);
            s_loaded_scene = scene;
            return;
        }
    }

    uint32_t file_w = 0;
    uint32_t file_h = 0;
    if (fread(&file_w, 4, 1, f) != 1 || fread(&file_h, 4, 1, f) != 1) {
        ESP_LOGW(TAG, "Scene header read failed: %s", bg_files[scene]);
        fill_fallback_background(s_scene_buf);
        fclose(f);
        s_loaded_scene = scene;
        return;
    }

    if (file_w == RLCD_WIDTH && file_h == RLCD_HEIGHT &&
        fread(s_scene_buf, 1, RLCD_WIDTH * RLCD_HEIGHT, f) == RLCD_WIDTH * RLCD_HEIGHT) {
        ESP_LOGI(TAG, "Scene %d loaded: %lux%lu", scene, (unsigned long)file_w, (unsigned long)file_h);
    } else {
        ESP_LOGW(TAG, "Scene size/data mismatch: %lux%lu vs %dx%d",
                 (unsigned long)file_w, (unsigned long)file_h, RLCD_WIDTH, RLCD_HEIGHT);
        fill_fallback_background(s_scene_buf);
    }

    fclose(f);
    s_loaded_scene = scene;
}

void renderer_init(void)
{
    s_graybuf = heap_caps_calloc(1, RLCD_WIDTH * RLCD_HEIGHT, MALLOC_CAP_SPIRAM);
    s_framebuf = heap_caps_calloc(1, RLCD_FB_SIZE, MALLOC_CAP_SPIRAM);
    s_scene_buf = heap_caps_calloc(1, RLCD_WIDTH * RLCD_HEIGHT, MALLOC_CAP_SPIRAM);
    s_pose_buf = heap_caps_calloc(1, 220 * 320, MALLOC_CAP_SPIRAM);
    s_char_err_a = heap_caps_calloc(222, sizeof(int16_t), MALLOC_CAP_SPIRAM);
    s_char_err_b = heap_caps_calloc(222, sizeof(int16_t), MALLOC_CAP_SPIRAM);

    if (!s_graybuf || !s_framebuf || !s_scene_buf || !s_pose_buf ||
        !s_char_err_a || !s_char_err_b) {
        ESP_LOGE(TAG, "Failed to allocate framebuffers!");
        return;
    }

    /* Precompute gamma-1.6 LUT used by DITHER_THRESH_BOOST */
    s_gamma_lut[0] = 0;
    for (int i = 1; i < 256; i++) {
        float v = powf(i / 255.0f, 1.6f) * 255.0f + 0.5f;
        s_gamma_lut[i] = (v > 255.0f) ? 255 : (uint8_t)v;
    }

    ESP_LOGI(TAG, "Renderer initialized (graybuf=%p, framebuf=%p, scenebuf=%p)",
             s_graybuf, s_framebuf, s_scene_buf);
}

static void render_background(const aura_state_t *state)
{
    int scene = state->current_scene;
    if (scene < 0 || scene > 9) scene = 0;
    ensure_scene_loaded(scene);
    memcpy(s_graybuf, s_scene_buf, RLCD_WIDTH * RLCD_HEIGHT);
}

static inline bool pose_pixel_visible(uint8_t pixel)
{
    return pixel != 0xFF;
}

static void prepare_character_error_row(int16_t *row, const uint8_t *src,
                                        int y, int src_w, int src_h)
{
    if (y < 0 || y >= src_h) {
        memset(row, 0, (size_t)(src_w + 2) * sizeof(int16_t));
        return;
    }

    row[0] = 0;
    row[src_w + 1] = 0;
    for (int x = 0; x < src_w; x++) {
        uint8_t pixel = src[y * src_w + x];
        row[x + 1] = pose_pixel_visible(pixel) ? (int16_t)pixel : 255;
    }
}

static inline void add_character_error(int16_t *row, const uint8_t *src,
                                       int x, int y, int src_w, int src_h,
                                       int16_t weighted_error)
{
    if (x < 0 || x >= src_w || y < 0 || y >= src_h) return;
    if (!pose_pixel_visible(src[y * src_w + x])) return;
    row[x + 1] += weighted_error;
}

static void dither_character_to_graybuf(const uint8_t *src,
                                        int src_w, int src_h,
                                        int x_off, int y_off)
{
    if (!src || src_w <= 0 || src_h <= 0 || src_w > 220 ||
        !s_char_err_a || !s_char_err_b) {
        return;
    }

    int16_t *cur = s_char_err_a;
    int16_t *next = s_char_err_b;
    prepare_character_error_row(cur, src, 0, src_w, src_h);

    for (int y = 0; y < src_h; y++) {
        prepare_character_error_row(next, src, y + 1, src_w, src_h);

        int screen_y = y_off + y;
        for (int x = 0; x < src_w; x++) {
            uint8_t pixel = src[y * src_w + x];
            if (!pose_pixel_visible(pixel)) {
                continue;
            }

            int screen_x = x_off + x;
            if (screen_x < 0 || screen_x >= RLCD_WIDTH ||
                screen_y < 0 || screen_y >= RLCD_HEIGHT) {
                continue;
            }

            int16_t old_val = cur[x + 1];
            if (old_val < 0) old_val = 0;
            if (old_val > 254) old_val = 254;

            /*
             * Outfit atlases now keep original grayscale. Dither only the
             * character while compositing, so backgrounds and UI remain clean.
             */
            bool black = old_val < 128;
            int16_t new_val = black ? 0 : 254;
            int16_t error = old_val - new_val;
            s_graybuf[screen_y * RLCD_WIDTH + screen_x] = black ? 0 : 254;

            add_character_error(cur,  src, x + 1, y,     src_w, src_h, (int16_t)(error * 7 / 16));
            add_character_error(next, src, x - 1, y + 1, src_w, src_h, (int16_t)(error * 3 / 16));
            add_character_error(next, src, x,     y + 1, src_w, src_h, (int16_t)(error * 5 / 16));
            add_character_error(next, src, x + 1, y + 1, src_w, src_h, (int16_t)(error * 1 / 16));
        }

        int16_t *tmp = cur;
        cur = next;
        next = tmp;
    }
}

/* Per-pixel character compositing for non-FS modes.
 * No error buffer needed; each pixel is quantised independently. */
static void composite_by_pixel(const uint8_t *src, int src_w, int src_h,
                                int x_off, int y_off)
{
    for (int y = 0; y < src_h; y++) {
        int screen_y = y_off + y;
        if (screen_y < 0 || screen_y >= RLCD_HEIGHT) continue;
        for (int x = 0; x < src_w; x++) {
            uint8_t pixel = src[y * src_w + x];
            if (pixel == 0xFF) continue;   // transparent

            int screen_x = x_off + x;
            if (screen_x < 0 || screen_x >= RLCD_WIDTH) continue;

            bool black;
            switch (s_dither_mode) {
                case DITHER_THRESH128:
                    black = (pixel < 128);
                    break;
                case DITHER_BAYER4:
                    black = (pixel <= s_bayer4[y & 3][x & 3]);
                    break;
                case DITHER_BAYER8:
                    black = (pixel <= s_bayer8[y & 7][x & 7]);
                    break;
                case DITHER_HYBRID_SOFT:
                    if (pixel < 80)        black = true;
                    else if (pixel > 200)  black = false;
                    else                   black = (pixel <= s_bayer8[y & 7][x & 7]);
                    break;
                case DITHER_HYBRID_INK:
                    if (pixel < 100)       black = true;
                    else if (pixel > 220)  black = false;
                    else                   black = (pixel <= s_bayer8[y & 7][x & 7]);
                    break;
                default:
                    black = (pixel < 128);
                    break;
            }
            s_graybuf[screen_y * RLCD_WIDTH + screen_x] = black ? 0 : 254;
        }
    }
}

/* ---------------------------------------------------------------------------
 * Outfit atlas table (shared by render_character and preview helpers)
 * -------------------------------------------------------------------------*/
static const char * const s_outfit_files[] = {
    ASSETS_BASE_PATH "/outfits/pajama.bin",     /* 0 睡衣   免费默认 */
    ASSETS_BASE_PATH "/outfits/dress.bin",      /* 1 洋装   免费     */
    ASSETS_BASE_PATH "/outfits/nightdress.bin", /* 2 睡裙   50       */
    ASSETS_BASE_PATH "/outfits/casual_a.bin",   /* 3 休闲装1 50      */
    ASSETS_BASE_PATH "/outfits/casual_b.bin",   /* 4 休闲装2 60      */
    ASSETS_BASE_PATH "/outfits/winter.bin",     /* 5 冬装   70       */
    ASSETS_BASE_PATH "/outfits/qipao.bin",      /* 6 旗袍   80       */
    ASSETS_BASE_PATH "/outfits/mamian.bin",     /* 7 马面裙 80       */
    ASSETS_BASE_PATH "/outfits/hanfu.bin",      /* 8 汉服   90       */
};
#define OUTFIT_FILE_COUNT ((int)(sizeof(s_outfit_files) / sizeof(s_outfit_files[0])))

static void render_character(const aura_state_t *state)
{
    if (state->current_outfit != s_loaded_outfit) {
        atlas_free(&s_atlas);
        int idx = state->current_outfit;
        if (idx < 0 || idx >= OUTFIT_FILE_COUNT) idx = 0;
        if (atlas_load(s_outfit_files[idx], &s_atlas) == ESP_OK) {
            s_loaded_outfit = state->current_outfit;
            s_loaded_custom_path[0] = '\0';
        }
    }

    if (!s_atlas.data || !s_pose_buf) return;

    atlas_extract_pose(&s_atlas, state->current_pose, s_pose_buf);

    int src_w = s_atlas.cell_width;
    int src_h = s_atlas.cell_height;
    int dst_w = src_w;
    int dst_h = src_h;

    // 水平：居中后左移 25px
    int x_off = (RLCD_WIDTH - dst_w) / 2 - 25;
    // 垂直：底部对齐，允许微调偏移（头离顶部黑条太近时向下移）
    int y_off = RLCD_HEIGHT - dst_h + s_char_y_offset;
    // 按当前 dither 模式合成角色层；背景/UI 不参与误差扩散
    if (s_dither_mode == DITHER_FS) {
        dither_character_to_graybuf(s_pose_buf, dst_w, dst_h, x_off, y_off);
    } else {
        composite_by_pixel(s_pose_buf, dst_w, dst_h, x_off, y_off);
    }
}

static bool render_character_path(const char *path, int pose)
{
    if (!path || !path[0] || !s_pose_buf) return false;

    if (s_loaded_outfit != CUSTOM_OUTFIT_CACHE_TAG ||
        strcmp(s_loaded_custom_path, path) != 0 ||
        !s_atlas.data) {
        atlas_free(&s_atlas);
        if (atlas_load(path, &s_atlas) != ESP_OK) {
            s_loaded_outfit = -1;
            s_loaded_custom_path[0] = '\0';
            return false;
        }
        s_loaded_outfit = CUSTOM_OUTFIT_CACHE_TAG;
        snprintf(s_loaded_custom_path, sizeof(s_loaded_custom_path), "%s", path);
    }

    if (!s_atlas.data) return false;
    if (pose < 0 || pose >= POSE_COUNT) pose = 0;

    atlas_extract_pose(&s_atlas, pose, s_pose_buf);

    int src_w = s_atlas.cell_width;
    int src_h = s_atlas.cell_height;
    int x_off = (RLCD_WIDTH - src_w) / 2 - 25;
    int y_off = RLCD_HEIGHT - src_h + s_char_y_offset;

    if (s_dither_mode == DITHER_FS) {
        dither_character_to_graybuf(s_pose_buf, src_w, src_h, x_off, y_off);
    } else {
        composite_by_pixel(s_pose_buf, src_w, src_h, x_off, y_off);
    }
    return true;
}

static void render_ui(const aura_state_t *state)
{
    status_bar_draw(s_graybuf, RLCD_WIDTH, state);
    if (state->info_board_visible &&
        state->ui_mode == AURA_UI_IDLE &&
        state->dialogue_ticks_left <= 0 &&
        !state->agent_panel_visible) {
        panels_draw_info_board(s_graybuf, RLCD_WIDTH, RLCD_HEIGHT, state);
        return;
    }
    panels_draw_left(s_graybuf, RLCD_WIDTH, RLCD_HEIGHT, state);
    panels_draw_right(s_graybuf, RLCD_WIDTH, RLCD_HEIGHT, state);
    panels_draw_dialogue(s_graybuf, RLCD_WIDTH, RLCD_HEIGHT, state);
}

static void set_frame_white(uint8_t *fb)
{
    memset(fb, 0xFF, RLCD_FB_SIZE);
}

static void threshold_to_st7305(const uint8_t *gray, uint8_t *fb)
{
    set_frame_white(fb);
    for (int y = 0; y < RLCD_HEIGHT; y++) {
        for (int x = 0; x < RLCD_WIDTH; x++) {
            if (gray[y * RLCD_WIDTH + x] < 128) {
                rlcd_set_pixel(fb, x, y, true);
            }
        }
    }
}

#if RENDERER_ENABLE_FLOYD_DITHER
static void dither_to_st7305_floyd(const uint8_t *gray, uint8_t *fb)
{
    set_frame_white(fb);

    // 误差缓冲 (只需要当前行+下一行)
    // 固定分配，用 swap 标记切换
    int16_t *err_a = heap_caps_calloc(RLCD_WIDTH + 2, sizeof(int16_t), MALLOC_CAP_SPIRAM);
    int16_t *err_b = heap_caps_calloc(RLCD_WIDTH + 2, sizeof(int16_t), MALLOC_CAP_SPIRAM);

    if (!err_a || !err_b) {
        // 回退到简单阈值
        ESP_LOGW(TAG, "Dither alloc failed, using threshold");
        for (int y = 0; y < RLCD_HEIGHT; y++) {
            for (int x = 0; x < RLCD_WIDTH; x++) {
                if (gray[y * RLCD_WIDTH + x] < 128) {
                    rlcd_set_pixel(fb, x, y, true);
                }
            }
        }
        if (err_a) heap_caps_free(err_a);
        if (err_b) heap_caps_free(err_b);
        return;
    }

    // +1 偏移方便 x-1 访问
    int16_t *cur  = err_a + 1;
    int16_t *next = err_b + 1;

    // 初始化第一行
    for (int x = 0; x < RLCD_WIDTH; x++) {
        cur[x] = gray[x];
    }

    for (int y = 0; y < RLCD_HEIGHT; y++) {
        // 预加载下一行灰度值到 next
        if (y + 1 < RLCD_HEIGHT) {
            for (int x = 0; x < RLCD_WIDTH; x++) {
                next[x] = gray[(y + 1) * RLCD_WIDTH + x];
            }
        } else {
            memset(next - 1, 0, (RLCD_WIDTH + 2) * sizeof(int16_t));
        }

        for (int x = 0; x < RLCD_WIDTH; x++) {
            uint8_t original = gray[y * RLCD_WIDTH + x];

            // Preserve clean UI/background whites and blacks. Full-frame error
            // diffusion makes near-white panels look dirty by spreading noise.
            if (original >= DITHER_WHITE_LOCK_THRESHOLD) {
                continue;
            }
            if (original <= DITHER_BLACK_LOCK_THRESHOLD) {
                rlcd_set_pixel(fb, x, y, true);
                continue;
            }

            int16_t old_val = cur[x];

            // Clamp
            if (old_val < 0) old_val = 0;
            if (old_val > 255) old_val = 255;

            // 阈值化
            bool is_black = (old_val < 128);
            int16_t new_val = is_black ? 0 : 255;
            int16_t error = old_val - new_val;

            // 写入 ST7305 block 格式
            if (is_black) {
                rlcd_set_pixel(fb, x, y, true);
            }

            // Floyd-Steinberg 误差扩散
            if (x + 1 < RLCD_WIDTH)
                cur[x + 1]  += error * 7 / 16;
            if (y + 1 < RLCD_HEIGHT) {
                if (x > 0)
                    next[x - 1] += error * 3 / 16;
                next[x]     += error * 5 / 16;
                if (x + 1 < RLCD_WIDTH)
                    next[x + 1] += error * 1 / 16;
            }
        }

        // 滚动: swap cur ↔ next
        int16_t *tmp = cur;
        cur = next;
        next = tmp;
    }

    heap_caps_free(err_a);
    heap_caps_free(err_b);
}
#endif

void renderer_draw_scene(const aura_state_t *state)
{
    render_background(state);
    render_character(state);
    render_ui(state);
}

void renderer_apply_threshold(void)
{
#if RENDERER_ENABLE_FLOYD_DITHER
    dither_to_st7305_floyd(s_graybuf, s_framebuf);
#else
    threshold_to_st7305(s_graybuf, s_framebuf);
#endif
}

uint8_t *renderer_get_graybuf(void)
{
    return s_graybuf;
}

void renderer_draw(const aura_state_t *state)
{
    // 1. 背景
    render_background(state);

    // 2. 角色
    render_character(state);

    // 3. UI
    render_ui(state);

    // 4. 灰度 → 1-bit：默认保持简单阈值，优先保证 UI/墙纸/动画背景干净稳定。
#if RENDERER_ENABLE_FLOYD_DITHER
    dither_to_st7305_floyd(s_graybuf, s_framebuf);
#else
    threshold_to_st7305(s_graybuf, s_framebuf);
#endif
}

void renderer_draw_with_outfit_override(const aura_state_t *state,
                                        int fallback_outfit_idx,
                                        const char *outfit_path)
{
    render_background(state);

    bool custom_drawn = false;
    if (outfit_path && outfit_path[0]) {
        custom_drawn = render_character_path(outfit_path, state->current_pose);
    }
    if (!custom_drawn) {
        aura_state_t tmp = *state;
        if (fallback_outfit_idx >= 0) {
            tmp.current_outfit = fallback_outfit_idx;
        }
        render_character(&tmp);
    }

    render_ui(state);

#if RENDERER_ENABLE_FLOYD_DITHER
    dither_to_st7305_floyd(s_graybuf, s_framebuf);
#else
    threshold_to_st7305(s_graybuf, s_framebuf);
#endif
}

uint8_t *renderer_get_framebuffer(void)
{
    return s_framebuf;
}

/* --------------------------------------------------------------------------
 * Dither mode public API
 * -------------------------------------------------------------------------*/

void renderer_cycle_dither_mode(void)
{
    s_dither_mode = (render_dither_mode_t)((s_dither_mode + 1) % DITHER_MODE_COUNT);
    ESP_LOGI(TAG, "Dither mode → %d: %s", s_dither_mode, s_dither_names[s_dither_mode]);
}

render_dither_mode_t renderer_get_dither_mode(void)
{
    return s_dither_mode;
}

const char *renderer_get_dither_mode_name(void)
{
    return s_dither_names[s_dither_mode];
}

/* ---------------------------------------------------------------------------
 * Character Y-offset public API
 * KEY long press steps through offsets to fine-tune head clearance.
 * -------------------------------------------------------------------------*/

void renderer_adjust_char_y_down(void)
{
    /* Step through 0, 5, 10, 15, 20, 25, 30 px then wrap to 0 */
    s_char_y_offset += 5;
    if (s_char_y_offset > 30) s_char_y_offset = 0;
    ESP_LOGI(TAG, "Char Y offset → %d px", s_char_y_offset);
}

int renderer_get_char_y_offset(void)
{
    return s_char_y_offset;
}

/* ---------------------------------------------------------------------------
 * Outfit name lookup (matches outfit_files[] order in render_character)
 * -------------------------------------------------------------------------*/
static const char * const s_outfit_names[] = {
    "睡衣", "洋装", "睡裙", "休闲装1", "休闲装2",
    "冬装", "旗袍", "马面裙", "汉服",
};
static const char * const s_outfit_names_en[] = {
    "Pajamas", "Dress", "Nightdress", "Casual A", "Casual B",
    "Winter", "Qipao", "Mamian Skirt", "Hanfu",
};
static const char * const s_outfit_names_ja[] = {
    "パジャマ", "ワンピース", "ネグリジェ", "カジュアルA", "カジュアルB",
    "冬服", "チャイナ", "馬面裙", "漢服",
};
#define OUTFIT_NAME_COUNT ((int)(sizeof(s_outfit_names) / sizeof(s_outfit_names[0])))

int renderer_get_outfit_count(void)
{
    return OUTFIT_NAME_COUNT;
}

const char *renderer_get_outfit_name(int idx)
{
    if (idx < 0 || idx >= OUTFIT_NAME_COUNT) return "?";
    return s_outfit_names[idx];
}

const char *renderer_get_outfit_name_localized(int idx, int language)
{
    if (idx < 0 || idx >= OUTFIT_NAME_COUNT) return "?";
    if (language == 1) return s_outfit_names_en[idx];
    if (language == 2) return s_outfit_names_ja[idx];
    return s_outfit_names[idx];
}

/* ---------------------------------------------------------------------------
 * renderer_draw_character_preview — draw character (pose 0) at a specific
 * position in the graybuf WITHOUT any background/UI.
 * Call after filling graybuf yourself. graybuf is NOT cleared here.
 *
 * @param outfit_idx  Which outfit to show (0-8)
 * @param dst_x       Left edge pixel in graybuf
 * @param dst_y       Top edge pixel in graybuf
 * -------------------------------------------------------------------------*/
void renderer_draw_character_preview(int outfit_idx, int dst_x, int dst_y)
{
    if (outfit_idx < 0 || outfit_idx >= OUTFIT_FILE_COUNT) outfit_idx = 0;
    if (!s_pose_buf) return;

    if (outfit_idx != s_loaded_outfit) {
        atlas_free(&s_atlas);
        if (atlas_load(s_outfit_files[outfit_idx], &s_atlas) == ESP_OK) {
            s_loaded_outfit = outfit_idx;
        }
    }
    if (!s_atlas.data) return;

    /* Extract pose 0 (idle front-facing) */
    atlas_extract_pose(&s_atlas, 0, s_pose_buf);

    int src_w = s_atlas.cell_width;
    int src_h = s_atlas.cell_height;

    /* Composite into graybuf at (dst_x, dst_y) */
    for (int y = 0; y < src_h; y++) {
        int sy = dst_y + y;
        if (sy < 0 || sy >= RLCD_HEIGHT) continue;
        for (int x = 0; x < src_w; x++) {
            int sx = dst_x + x;
            if (sx < 0 || sx >= RLCD_WIDTH) continue;
            uint8_t pixel = s_pose_buf[y * src_w + x];
            if (pixel == 0xFF) continue;  /* transparent */
            bool black;
            switch (s_dither_mode) {
                case DITHER_BAYER4:
                    black = (pixel <= s_bayer4[y & 3][x & 3]); break;
                case DITHER_BAYER8:
                    black = (pixel <= s_bayer8[y & 7][x & 7]); break;
                case DITHER_HYBRID_SOFT:
                    if (pixel < 80)       black = true;
                    else if (pixel > 200) black = false;
                    else                  black = (pixel <= s_bayer8[y & 7][x & 7]);
                    break;
                case DITHER_HYBRID_INK:
                default:
                    if (pixel < 100)       black = true;
                    else if (pixel > 220)  black = false;
                    else                   black = (pixel <= s_bayer8[y & 7][x & 7]);
                    break;
            }
            s_graybuf[sy * RLCD_WIDTH + sx] = black ? 0 : 254;
        }
    }
}

void renderer_draw_character_preview_fit(int outfit_idx, int dst_x, int dst_y,
                                         int max_w, int max_h)
{
    if (outfit_idx < 0 || outfit_idx >= OUTFIT_FILE_COUNT) outfit_idx = 0;
    if (!s_pose_buf || max_w <= 0 || max_h <= 0) return;

    if (outfit_idx != s_loaded_outfit) {
        atlas_free(&s_atlas);
        if (atlas_load(s_outfit_files[outfit_idx], &s_atlas) == ESP_OK) {
            s_loaded_outfit = outfit_idx;
        }
    }
    if (!s_atlas.data) return;

    atlas_extract_pose(&s_atlas, 0, s_pose_buf);

    int src_w = s_atlas.cell_width;
    int src_h = s_atlas.cell_height;
    if (src_w <= 0 || src_h <= 0) return;

    int dst_w = max_w;
    int dst_h = (src_h * dst_w + src_w / 2) / src_w;
    if (dst_h > max_h) {
        dst_h = max_h;
        dst_w = (src_w * dst_h + src_h / 2) / src_h;
    }
    if (dst_w <= 0 || dst_h <= 0) return;

    int x0 = dst_x + (max_w - dst_w) / 2;
    int y0 = dst_y + max_h - dst_h;

    for (int y = 0; y < dst_h; y++) {
        int sy = y0 + y;
        if (sy < 0 || sy >= RLCD_HEIGHT) continue;
        int src_y = (y * src_h + dst_h / 2) / dst_h;
        if (src_y >= src_h) src_y = src_h - 1;

        for (int x = 0; x < dst_w; x++) {
            int sx = x0 + x;
            if (sx < 0 || sx >= RLCD_WIDTH) continue;
            int src_x = (x * src_w + dst_w / 2) / dst_w;
            if (src_x >= src_w) src_x = src_w - 1;

            uint8_t pixel = s_pose_buf[src_y * src_w + src_x];
            if (pixel == 0xFF) continue;

            bool black;
            switch (s_dither_mode) {
                case DITHER_BAYER4:
                    black = (pixel <= s_bayer4[y & 3][x & 3]); break;
                case DITHER_BAYER8:
                    black = (pixel <= s_bayer8[y & 7][x & 7]); break;
                case DITHER_HYBRID_SOFT:
                    if (pixel < 80)       black = true;
                    else if (pixel > 200) black = false;
                    else                  black = (pixel <= s_bayer8[y & 7][x & 7]);
                    break;
                case DITHER_HYBRID_INK:
                default:
                    if (pixel < 100)       black = true;
                    else if (pixel > 220)  black = false;
                    else                   black = (pixel <= s_bayer8[y & 7][x & 7]);
                    break;
            }
            s_graybuf[sy * RLCD_WIDTH + sx] = black ? 0 : 254;
        }
    }
}

void renderer_draw_character_preview_crop(int outfit_idx, int dst_x, int dst_y,
                                          int view_w, int view_h,
                                          int src_center_x, int src_top_y,
                                          int pose_idx)
{
    if (outfit_idx < 0 || outfit_idx >= OUTFIT_FILE_COUNT) outfit_idx = 0;
    if (!s_pose_buf || view_w <= 0 || view_h <= 0) return;

    if (outfit_idx != s_loaded_outfit) {
        atlas_free(&s_atlas);
        if (atlas_load(s_outfit_files[outfit_idx], &s_atlas) == ESP_OK) {
            s_loaded_outfit = outfit_idx;
        }
    }
    if (!s_atlas.data) return;

    if (pose_idx < 0 || pose_idx >= 9) pose_idx = 0;
    atlas_extract_pose(&s_atlas, pose_idx, s_pose_buf);

    int src_w = s_atlas.cell_width;
    int src_h = s_atlas.cell_height;
    if (src_w <= 0 || src_h <= 0) return;

    if (src_center_x < 0) src_center_x = src_w / 2;
    int src_x0 = src_center_x - view_w / 2;
    int src_y0 = src_top_y;

    for (int y = 0; y < view_h; y++) {
        int sy = dst_y + y;
        if (sy < 0 || sy >= RLCD_HEIGHT) continue;
        int src_y = src_y0 + y;
        if (src_y < 0 || src_y >= src_h) continue;

        for (int x = 0; x < view_w; x++) {
            int sx = dst_x + x;
            if (sx < 0 || sx >= RLCD_WIDTH) continue;
            int src_x = src_x0 + x;
            if (src_x < 0 || src_x >= src_w) continue;

            uint8_t pixel = s_pose_buf[src_y * src_w + src_x];
            if (pixel == 0xFF) continue;

            bool black;
            switch (s_dither_mode) {
                case DITHER_BAYER4:
                    black = (pixel <= s_bayer4[y & 3][x & 3]); break;
                case DITHER_BAYER8:
                    black = (pixel <= s_bayer8[y & 7][x & 7]); break;
                case DITHER_HYBRID_SOFT:
                    if (pixel < 80)       black = true;
                    else if (pixel > 200) black = false;
                    else                  black = (pixel <= s_bayer8[y & 7][x & 7]);
                    break;
                case DITHER_HYBRID_INK:
                default:
                    if (pixel < 100)       black = true;
                    else if (pixel > 220)  black = false;
                    else                   black = (pixel <= s_bayer8[y & 7][x & 7]);
                    break;
            }
            s_graybuf[sy * RLCD_WIDTH + sx] = black ? 0 : 254;
        }
    }
}
