/**
 * 渲染器 — 场景合成 (背景 + 装饰 + 角色 + UI)
 */
#pragma once
#include <stdint.h>
#include <stdbool.h>

typedef enum {
    AURA_UI_IDLE = 0,
    AURA_UI_LISTENING,
    AURA_UI_PROCESSING,
    AURA_UI_SPEAKING,
} aura_ui_mode_t;

// 全局状态结构
typedef struct {
    // 显示
    int current_pose;       // 0-8
    int current_scene;      // living room, study, bedroom, kitchen, balcony, street, cafe, shop, park, mall
    int current_outfit;     // 0-16 (0-10=wardrobe, 11-16=shop)
    uint32_t outfit_unlocked; // bitmask: bit N=1 means outfit N owned
    bool dirty;             // 需要重绘

    // 传感器
    float temperature;
    float humidity;
    int hour;
    int minute;
    int month;              // 1-12
    int day;                // 1-31
    int weather_icon;       // 0=sun, 1=cloud, 2=rain, 3=snow
    char weather_city[32];
    bool weather_valid;

    // 状态
    int mood;               // 0-100
    int energy;             // 0-100
    int satiety;            // 0-100
    int affinity;           // 累计好感值
    int affinity_level;     // Lv1-5
    int coins;              // beans currency (legacy field name kept for compat)
    bool companion_state_ready;
    bool world_outfit_mode_ready;
    bool world_sleepwear_requested;
    bool world_sleeping;
    bool quota_ready;
    char quota_provider[20];
    char quota_headline[24];
    int quota_percent;
    char quota_text[20];
    char quota_primary_label[8];
    char quota_primary_text[24];
    int quota_primary_percent;
    char quota_secondary_label[8];
    char quota_secondary_text[24];
    int quota_secondary_percent;
    char info_notes[3][64];

    // 网络
    int wifi_strength;      // 0-4, 0=无信号/未连接
    bool ws_connected;

    // 对话
    char display_text[512]; // 当前显示的文本
    int text_char_index;    // 打字机效果的当前字符位
    int dialogue_ticks_left; // 底部对白剩余显示时长（10fps tick）
    int dialogue_page_tick;  // 当前语音段的播放进度（0-999）
    char current_emotion[16]; // 当前情绪 (neutral/proud/thinking/...)

    // UI 交互层
    int ui_language;        // 0=zh, 1=en, 2=ja
    aura_ui_mode_t ui_mode;
    int ui_anim_tick;       // 10fps 动画 tick
    int64_t idle_since_ms;  // monotonic start of the current settled idle period
    bool info_board_visible;
    int mic_level;          // 0-100, 录音强度
    bool agent_panel_visible;
    int agent_progress;     // 0-100
    char agent_title[32];
    char agent_status[48];

    // Settlement deltas (for unified work→settle panel)
    int settle_beans_delta;
    int settle_energy_delta;
    int settle_mood_delta;
    int settle_duration;
} aura_state_t;

// 外部全局状态
extern aura_state_t g_state;

/* ---------------------------------------------------------------------------
 * Dither mode — selects how the character layer is quantised to 1-bit.
 * Cycle at runtime with renderer_cycle_dither_mode() to compare on-device.
 * -------------------------------------------------------------------------*/
typedef enum {
    DITHER_FS = 0,       // Floyd-Steinberg (original, reference)
    DITHER_THRESH128,    // Plain threshold at 128
    DITHER_BAYER4,       // 4×4 Bayer ordered dither
    DITHER_BAYER8,       // 8×8 Bayer ordered dither
    DITHER_HYBRID_SOFT,  // Dark≤80→black, bright≥200→white, Bayer8 midtones
    DITHER_HYBRID_INK,   // Dark≤100→black, bright≥220→white, Bayer8 midtones
    DITHER_MODE_COUNT
} render_dither_mode_t;

void                 renderer_cycle_dither_mode(void);
render_dither_mode_t renderer_get_dither_mode(void);
const char          *renderer_get_dither_mode_name(void);

/* KEY long press: step character down 5 px (0→5→10→…→30→0). */
void renderer_adjust_char_y_down(void);
int  renderer_get_char_y_offset(void);

/* Outfit name lookup. */
int         renderer_get_outfit_count(void);
const char *renderer_get_outfit_name(int idx);
const char *renderer_get_outfit_name_localized(int idx, int language);

/**
 * Draw the character (pose 0, idle) at (dst_x, dst_y) in the internal graybuf.
 * Loads the specified outfit if not already cached.
 * Does NOT clear the buffer — caller must fill background first.
 */
void renderer_draw_character_preview(int outfit_idx, int dst_x, int dst_y);

/**
 * Draw pose 0 scaled to fit inside max_w × max_h.
 * This is intended for menu/shop previews where a full 200×300 character would
 * collide with headers and footers.
 */
void renderer_draw_character_preview_fit(int outfit_idx, int dst_x, int dst_y,
                                         int max_w, int max_h);

/**
 * Draw a pose at native atlas scale and crop it to the given viewport.
 * Useful for shop try-on previews where a large readable 2/3-body crop is
 * better than a tiny full-body fit. pose_idx 0-8 (out of range falls back to 0).
 */
void renderer_draw_character_preview_crop(int outfit_idx, int dst_x, int dst_y,
                                          int view_w, int view_h,
                                          int src_center_x, int src_top_y,
                                          int pose_idx);

void     renderer_init(void);

/** Full render: draw scene to graybuf, threshold to framebuf. */
void     renderer_draw(const aura_state_t *state);

/**
 * Full render with a temporary outfit override.
 *
 * If outfit_path is non-NULL, it is loaded as an atlas and used for this frame.
 * If loading fails, fallback_outfit_idx is used. This keeps setup/demo screens
 * from polluting the user's owned wardrobe.
 */
void     renderer_draw_with_outfit_override(const aura_state_t *state,
                                            int fallback_outfit_idx,
                                            const char *outfit_path);

/**
 * Render scene to graybuf only (no threshold step).
 * Call before drawing overlays, then call renderer_apply_threshold().
 */
void     renderer_draw_scene(const aura_state_t *state);

/** Apply threshold: convert graybuf → 1-bit framebuf. */
void     renderer_apply_threshold(void);

/** Return the 400×300 uint8 grayscale working buffer. */
uint8_t *renderer_get_graybuf(void);

/** Return the 15000-byte 1-bit ST7305 framebuffer. */
uint8_t *renderer_get_framebuffer(void);
bool aura_companion_state_cache_load(void);
void aura_companion_state_cache_save(void);
