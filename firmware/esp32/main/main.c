/**
 * Aura 豆豆 — 主入口
 * 创建所有 FreeRTOS 任务，初始化硬件
 *
 * Voice interaction pipeline:
 *   IDLE → (click or "豆豆") → LISTENING → (click) → PROCESSING → (AI reply) → SPEAKING → IDLE
 */
#include <stdio.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <sys/time.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_random.h"
#include "esp_psram.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "driver/i2c.h"
#include "driver/i2s_std.h"
#include "driver/gpio.h"

#include "aura_config.h"
#include "rlcd_driver.h"
#include "renderer.h"
#include "esp_heap_caps.h"
#include "audio_pipeline.h"
#include "es8311.h"
#include "sfx.h"
#include "wifi_manager.h"
#include "net_discovery.h"
#include "ws_client.h"
#include "ota_update.h"
#include "shtc3.h"
#include "battery.h"
#include "pcf85063.h"
#include "sd_card.h"
#include "buttons.h"
#include "layout.h"
#include "font.h"
#include "state_helpers.h"
#include "messages.h"
#include "esp_spiffs.h"
#include "core/state_machine.h"
#include "wake_word.h"
#include "audio/opus_bridge.h"
#include "audio/music_player.h"
#include "minigame_state.h"
#include "minigame_render.h"

/* ═══════════════════════════════════════════════════════
 * MIC LOOPBACK TEST — 本地录放测试，不走网络
 * 按住录音，松手播放。每次切换不同 SLOT/增益配置。
 * 编译时设为 0 恢复正常模式。
 * ═══════════════════════════════════════════════════════ */
#define MIC_LOOPBACK_TEST  0

static const char *TAG = "aura_main";

// 全局事件组
EventGroupHandle_t g_event_group;
#define EVT_WIFI_CONNECTED  BIT0
#define EVT_WS_CONNECTED    BIT1
#define EVT_DISPLAY_READY   BIT2

// 全局状态
aura_state_t g_state = {0};

// SPEAKING 超时定时器
static esp_timer_handle_t s_speak_timer = NULL;
static int64_t s_wake_resume_after_ms = 0;  /* 播报结束后的唤醒词恢复冷却时间 */
static bool s_ignore_listening_release = false;
static int s_output_volume = AURA_DEFAULT_OUTPUT_VOLUME;
static volatile bool s_audio_ready = false;
static bool s_continuous_session_active = false;
static uint32_t s_continuous_session_turns = 0;
static int64_t s_continuous_session_started_ms = 0;

static void ui_set_dialogue(const char *text, int ttl_ticks);

static void continuous_session_begin(void)
{
    s_continuous_session_active = true;
    s_continuous_session_turns = 0;
    s_continuous_session_started_ms = esp_timer_get_time() / 1000;
    ws_client_set_continuous_listening_enabled(true);
}

static void continuous_session_end(void)
{
    s_continuous_session_active = false;
    s_continuous_session_turns = 0;
    s_continuous_session_started_ms = 0;
    ws_client_set_continuous_listening_enabled(false);
    aura_ui_set_continuous_dialogue(false);
}

/* ── 主菜单弹窗状态 (display_task / input_task 共享) ─────────────── */
static volatile bool s_menu_open = false;
static volatile int  s_menu_sel  = 0;
static volatile bool s_volume_menu_open = false;
static volatile int  s_volume_menu_sel  = 0;
static volatile bool s_wifi_menu_open = false;
static volatile int  s_wifi_menu_sel  = 0;
static volatile bool s_wifi_manager_ready = false;
static volatile bool s_ota_manual_requested = false;

/* ── 服装店 / 衣柜 状态 ─────────────────────────────────────────── */
static bool s_shop_open      = false;
static int  s_shop_sel       = 0;       /* 0-5: shop item index           */
static int  s_shop_saved_outfit = 0;    /* restore on exit without buying */
static bool s_dessert_open   = false;
static int  s_dessert_sel    = 0;

static bool s_wardrobe_open  = false;
static int  s_wardrobe_sel   = 0;       /* currently selected owned outfit */

/* ── 换衣帘幕动画 ───────────────────────────────────────────────── */
static int  s_curtain_tick   = 0;       /* 0=off, 1-6=close, 7-12=open */
static int  s_curtain_target = -1;      /* outfit idx to swap at midpoint */

/* ── 自动换装（由世界模型的睡眠/日间阶段驱动） ───────────────────── */
static int s_outfit_pin_md      = -1;   /* 用户当天手动指定：month*100+day，次日失效（NVS 持久化） */
static int s_auto_outfit_mode   = -1;   /* -1=未知，0=日装，1=睡衣 */

/* 睡衣(0)/睡裙(2) 是隐藏服装：不进商店/衣柜，只在夜晚自动换上，
 * 因此默认全部解锁。dress(1) 是免费日装。 */
#define BASIC_OUTFIT_UNLOCK_MASK 0x7u    /* pajama/dress/nightdress */
#define SHOP_OUTFIT_UNLOCK_MASK 0x1F8u   /* casual_a..hanfu (idx 3-8) */
#define SHOP_ITEM_COUNT 6

/* Lily test firmware: boot straight into Wi-Fi/backend instead of demo setup. */
#define AURA_DEMO_FORCE_LANGUAGE_SELECT 0
/* 网关地址不再编译期写死；由 net_discovery（mDNS→缓存→配网→默认）解析。 */
#define LANGUAGE_COUNT 3
typedef enum {
    AURA_LANG_ZH = 0,
    AURA_LANG_EN,
    AURA_LANG_JA,
} aura_language_t;

typedef struct {
    const char *label;
    const char *voice_path;
    const char *subtitle;
    const char *outfit_path;
    int outfit_idx;
} language_option_t;

static const language_option_t s_language_options[LANGUAGE_COUNT] = {
    { "中文",    ASSETS_BASE_PATH "/sounds/lang_zh.pcm",
      "你好，我是 Aura。以后我会用中文和你说话。", NULL, -1 },
    { "English", ASSETS_BASE_PATH "/sounds/lang_en.pcm",
      "Hi, I'm Aura. I'll speak with you in English.",
      NULL, -1 },
    { "日本語",  ASSETS_BASE_PATH "/sounds/lang_ja.pcm",
      "こんにちは、Auraです。これから日本語で話します。",
      NULL, -1 },
};

static volatile bool s_language_select_open = AURA_DEMO_FORCE_LANGUAGE_SELECT != 0;
static volatile int  s_language_sel = -1;
static volatile int  s_language_confirmed = -1;
static volatile aura_language_t s_ui_language = AURA_LANG_ZH;

typedef struct {
    const char *text[LANGUAGE_COUNT];
} i18n_text_t;

static const char *tr_text(const i18n_text_t *entry)
{
    int lang = (int)s_ui_language;
    if (lang < 0 || lang >= LANGUAGE_COUNT) lang = AURA_LANG_ZH;
    return entry->text[lang] ? entry->text[lang] : entry->text[AURA_LANG_ZH];
}

static int tr_lang_index(void)
{
    int lang = (int)s_ui_language;
    return (lang >= 0 && lang < LANGUAGE_COUNT) ? lang : AURA_LANG_ZH;
}

static const char *ui_language_code(void)
{
    switch (s_ui_language) {
        case AURA_LANG_EN: return "en";
        case AURA_LANG_JA: return "ja";
        case AURA_LANG_ZH:
        default: return "zh";
    }
}

static const i18n_text_t T_CONNECTING     = {{ "连接中...", "Connecting...", "接続中..." }};
static const i18n_text_t T_DESSERT_TITLE  = {{ "甜品店", "Dessert shop", "おやつ屋" }};
static const i18n_text_t T_DESSERT_PRICE  = {{ "价格", "Price", "価格" }};
static const i18n_text_t T_DESSERT_EFFECT = {{ "效果", "Effect", "効果" }};
static const i18n_text_t T_DESSERT_BALANCE = {{ "余额", "Balance", "残高" }};
static const i18n_text_t T_DESSERT_BUY    = {{ "购买", "Buy", "買う" }};
static const i18n_text_t T_DESSERT_PENDING = {{ "结算中...", "Processing...", "処理中..." }};
static const i18n_text_t T_DESSERT_OFFLINE = {{ "联网后才能购买甜品", "Connect to buy desserts", "接続してから購入できます" }};
static const i18n_text_t T_BUY_OK         = {{ "购买成功！已穿上~", "Bought. Wearing it now.", "買いました。着替えました" }};
static const i18n_text_t T_NOT_ENOUGH     = {{ "余额不够哦 >_<", "Not enough balance.", "残高が足りません" }};
static const i18n_text_t T_AUTO_SLEEPWEAR = {{ "夜深了，换上睡衣~", "Getting late, pajama time~", "夜だから、パジャマに着替えるね" }};
static const i18n_text_t T_AUTO_DAYWEAR   = {{ "今天穿这套怎么样？", "How about this outfit today?", "今日はこの服にしたよ" }};
static const i18n_text_t T_PAUSED         = {{ "已暂停", "Paused", "一時停止" }};
static const i18n_text_t T_RESUMED        = {{ "继续播放", "Resumed", "再開しました" }};
static const i18n_text_t T_STOPPED        = {{ "已停止播放", "Stopped", "停止しました" }};
static const i18n_text_t T_HEARD_WAKE     = {{ "听到豆豆，请说...", "I heard you. Please speak.", "聞こえました。話してください" }};
static const i18n_text_t T_CANCELLED      = {{ "已取消", "Cancelled", "キャンセルしました" }};
static const i18n_text_t T_CHANGING       = {{ "换装中", "Changing", "着替え中" }};
static const i18n_text_t T_BOOT_HELLO     = {{ "你好，我是 Aura。", "Hi, I'm Aura.", "こんにちは、Auraです。" }};
static const i18n_text_t T_VOICE_OFFLINE  = {{ "离线中，语音稍后可用", "Offline. Voice is unavailable.", "オフラインです" }};
static const i18n_text_t T_WIFI_STARTING  = {{ "Wi-Fi 正在启动", "Wi-Fi is starting", "Wi-Fi起動中" }};
static const i18n_text_t T_WIFI_OFFLINE   = {{ "未连接 Wi-Fi，本地功能可用", "Wi-Fi offline. Local features work.", "Wi-Fi未接続。ローカル可" }};
static const i18n_text_t T_WIFI_RECONNECT = {{ "正在重新连接 Wi-Fi", "Reconnecting Wi-Fi", "Wi-Fi再接続中" }};
static const i18n_text_t T_WIFI_SETUP     = {{ "配网模式", "Wi-Fi setup", "Wi-Fi設定" }};
static const i18n_text_t T_WIFI_SETUP_ERR = {{ "配网启动失败", "Wi-Fi setup failed", "Wi-Fi設定失敗" }};
static const i18n_text_t T_VOICE_NOT_READY = {{ "语音未就绪 ws=%d ready=%d", "Voice not ready (ws=%d ready=%d)", "音声未準備 ws=%d ready=%d" }};
static const i18n_text_t T_AGENT_SENDING  = {{ "发送中", "Sending", "送信中" }};
static const i18n_text_t T_AGENT_REPLYING = {{ "回复中", "Replying", "返信中" }};
static const i18n_text_t T_LANGUAGE_TITLE = {{ "语言", "Language", "言語" }};

static const i18n_text_t T_MENU_TITLE     = {{ "菜单", "Menu", "メニュー" }};
static const i18n_text_t T_MENU_SHOP      = {{ "服装店", "Shop", "お店" }};
static const i18n_text_t T_MENU_WARDROBE  = {{ "衣柜", "Wardrobe", "衣装" }};
static const i18n_text_t T_MENU_DESSERT   = {{ "甜品", "Dessert", "おやつ" }};
static const i18n_text_t T_MENU_VOLUME    = {{ "音量", "Volume", "音量" }};
/* 语言项三语固定用英文，保证任何语种下都能找到换语言入口 */
static const i18n_text_t T_MENU_LANGUAGE  = {{ "Language", "Language", "Language" }};
static const i18n_text_t T_MENU_WIFI      = {{ "Wi-Fi", "Wi-Fi", "Wi-Fi" }};
static const i18n_text_t T_MENU_UPDATE    = {{ "系统更新", "Update", "更新" }};
static const i18n_text_t T_MENU_BACK      = {{ "返回", "Back", "戻る" }};

static const i18n_text_t T_VOLUME_DOWN    = {{ "音量 -", "Vol -", "音量 -" }};
static const i18n_text_t T_VOLUME_UP      = {{ "音量 +", "Vol +", "音量 +" }};
static const i18n_text_t T_VOLUME_LABEL   = {{ "音量", "Volume", "音量" }};
static const i18n_text_t T_WIFI_ACTION_RECONNECT = {{ "重新连接", "Reconnect", "再接続" }};
static const i18n_text_t T_WIFI_ACTION_SETUP     = {{ "添加网络", "Add network", "ネットワーク追加" }};
static const i18n_text_t T_OTA_CHECKING           = {{ "正在检查更新", "Checking for updates", "更新を確認中" }};
static const i18n_text_t T_OTA_RESOURCES          = {{ "正在更新资源", "Updating resources", "素材を更新中" }};
static const i18n_text_t T_OTA_APP                = {{ "正在更新系统", "Updating system", "システム更新中" }};
static const i18n_text_t T_OTA_VERIFYING          = {{ "正在校验更新", "Verifying update", "更新を検証中" }};
static const i18n_text_t T_OTA_CURRENT            = {{ "已经是最新版本", "Already up to date", "最新バージョンです" }};
static const i18n_text_t T_OTA_RESTARTING         = {{ "更新完成，即将重启", "Updated. Restarting", "更新完了。再起動します" }};
static const i18n_text_t T_OTA_FAILED             = {{ "更新失败，请稍后重试", "Update failed. Try again", "更新失敗。再試行してください" }};
static const i18n_text_t T_OTA_BUSY               = {{ "更新正在进行", "Update in progress", "更新中です" }};

static const i18n_text_t T_SHOP_TITLE      = {{ "服装商店", "Shop", "お店" }};
static const i18n_text_t T_SHOP_CURRENCY   = {{ "豆", "pt", "pt" }};
static const i18n_text_t T_SHOP_OWNED      = {{ "已拥有", "Owned", "所持" }};
static const i18n_text_t T_SHOP_NEW        = {{ "新品", "New", "新作" }};
static const i18n_text_t T_SHOP_PRICE      = {{ "价格", "Price", "価格" }};
static const i18n_text_t T_SHOP_STYLE      = {{ "风格", "Style", "系統" }};
static const i18n_text_t T_SHOP_STATUS     = {{ "状态", "Status", "状態" }};
static const i18n_text_t T_SHOP_LOCKED     = {{ "未购买", "Locked", "未購入" }};
static const i18n_text_t T_SHOP_BALANCE    = {{ "余额", "Balance", "残高" }};
static const i18n_text_t T_SHOP_AFTER      = {{ "购买后", "After", "購入後" }};
static const i18n_text_t T_SHOP_SHORT      = {{ "还差", "Need", "不足" }};
static const i18n_text_t T_SHOP_NOT_ENOUGH = {{ "余额不足", "Not enough", "不足" }};
static const i18n_text_t T_SHOP_WEAR       = {{ "穿上", "Wear", "着る" }};
static const i18n_text_t T_SHOP_BUY        = {{ "购买", "Buy", "買う" }};
static const i18n_text_t T_SHOP_FOOTER     = {{ "右键翻页  左键确认  长按退出", "Right next  Left OK  Hold back", "右:次  左:決定  長押し:戻る" }};

static const i18n_text_t T_WARDROBE_TITLE    = {{ "我的衣柜", "Wardrobe", "衣装" }};
static const i18n_text_t T_WARDROBE_ID       = {{ "编号", "ID", "番号" }};
static const i18n_text_t T_WARDROBE_WEARABLE = {{ "可穿戴", "Ready", "着用可" }};
static const i18n_text_t T_WARDROBE_SOURCE   = {{ "来源", "Source", "入手" }};
static const i18n_text_t T_WARDROBE_HOME     = {{ "衣柜", "Closet", "衣装" }};
static const i18n_text_t T_WARDROBE_FOOTER   = {{ "右键换衣  左键穿上  长按返回", "Right next  Left wear  Hold back", "右:次  左:着る  長押し:戻る" }};

typedef struct {
    int outfit_idx;
    const char *name[LANGUAGE_COUNT];
    int price;
    const char *tag[LANGUAGE_COUNT];
} shop_item_t;
static const shop_item_t s_shop_catalog[SHOP_ITEM_COUNT] = {
    { 3, { "休闲装1", "Casual A",  "カジュアルA" }, 50, { "日常", "Daily",   "日常" } },
    { 4, { "职业装", "Professional",  "仕事着" }, 60, { "工作", "Work",   "仕事" } },
    { 5, { "冬装",   "Winter",     "冬服" },       70, { "保暖", "Warm",    "あったか" } },
    { 6, { "旗袍",   "Qipao",      "チャイナ" },   80, { "优雅", "Elegant", "優雅" } },
    { 7, { "马面裙", "Mamian",     "馬面裙" },     80, { "古风", "Classic", "古風" } },
    { 8, { "汉服",   "Hanfu",      "漢服" },       90, { "古风", "Classic", "古風" } },
};

typedef struct {
    const char *id;
    const char *name[LANGUAGE_COUNT];
    int price;
    const char *effect[LANGUAGE_COUNT];
} dessert_item_t;

#define DESSERT_ITEM_COUNT 8
static const dessert_item_t s_dessert_catalog[DESSERT_ITEM_COUNT] = {
    { "pudding", { "焦糖布丁", "Caramel pudding", "カスタードプリン" }, 8,
      { "饱腹+8 心情+3\n压力-1 能量+5", "Sat+8 Mood+3\nStress-1 Energy+5", "満腹+8 気分+3\nストレス-1 元気+5" } },
    { "dorayaki", { "红豆铜锣烧", "Red bean dorayaki", "どら焼き" }, 10,
      { "饱腹+11 心情+7\n压力-3 能量+2", "Sat+11 Mood+7\nStress-3 Energy+2", "満腹+11 気分+7\nストレス-3 元気+2" } },
    { "ice_cream", { "云朵冰淇淋", "Cloud ice cream", "雲のアイス" }, 12,
      { "饱腹+5 心情+12\n压力-8 能量-1", "Sat+5 Mood+12\nStress-8 Energy-1", "満腹+5 気分+12\nストレス-8 元気-1" } },
    { "matcha_parfait", { "抹茶芭菲", "Matcha parfait", "抹茶パフェ" }, 15,
      { "饱腹+15 心情+6\n压力-4 能量+6", "Sat+15 Mood+6\nStress-4 Energy+6", "満腹+15 気分+6\nストレス-4 元気+6" } },
    { "strawberry_cake", { "草莓蛋糕", "Strawberry cake", "いちごケーキ" }, 18,
      { "饱腹+13 心情+16\n压力-3 能量+0", "Sat+13 Mood+16\nStress-3 Energy+0", "満腹+13 気分+16\nストレス-3 元気+0" } },
    { "strawberry_tart", { "莓果挞", "Berry tart", "ベリータルト" }, 22,
      { "饱腹+9 心情+10\n压力-12 能量+2", "Sat+9 Mood+10\nStress-12 Energy+2", "満腹+9 気分+10\nストレス-12 元気+2" } },
    { "chocolate_sundae", { "巧克力圣代", "Chocolate sundae", "チョコサンデー" }, 25,
      { "饱腹+22 心情+11\n压力-5 能量-3", "Sat+22 Mood+11\nStress-5 Energy-3", "満腹+22 気分+11\nストレス-5 元気-3" } },
    { "celebration_mille_crepe", { "庆祝千层", "Celebration mille crepe", "お祝いミルクレープ" }, 30,
      { "饱腹+16 心情+22\n压力-10 能量+4", "Sat+16 Mood+22\nStress-10 Energy+4", "満腹+16 気分+22\nストレス-10 元気+4" } },
};

static void gb_fill_rect(uint8_t *gb, int x, int y, int w, int h, uint8_t color)
{
    if (!gb || w <= 0 || h <= 0) return;
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x + w > RLCD_WIDTH) w = RLCD_WIDTH - x;
    if (y + h > RLCD_HEIGHT) h = RLCD_HEIGHT - y;
    if (w <= 0 || h <= 0) return;
    for (int yy = 0; yy < h; yy++) {
        memset(gb + (y + yy) * RLCD_WIDTH + x, color, w);
    }
}

static void gb_draw_rect(uint8_t *gb, int x, int y, int w, int h, uint8_t color)
{
    gb_fill_rect(gb, x, y, w, 1, color);
    gb_fill_rect(gb, x, y + h - 1, w, 1, color);
    gb_fill_rect(gb, x, y, 1, h, color);
    gb_fill_rect(gb, x + w - 1, y, 1, h, color);
}

static void draw_centered_utf8_on_width(uint8_t *gb, int x, int y, int w,
                                        const char *text, uint8_t color)
{
    int tx = x + (w - font_utf8_width(text)) / 2;
    if (tx < x + 2) tx = x + 2;
    font_draw_utf8(gb, RLCD_WIDTH, tx, y, text, color);
}

static void language_select_draw(uint8_t *gb, int selected)
{
    const int panel_x = 308;
    const int panel_y = 28;
    const int panel_w = 84;
    const int panel_h = 154;
    gb_fill_rect(gb, panel_x, panel_y, panel_w, panel_h, 0xFF);
    gb_draw_rect(gb, panel_x, panel_y, panel_w, panel_h, 0x00);
    gb_draw_rect(gb, panel_x + 2, panel_y + 2, panel_w - 4, panel_h - 4, 0x00);

    draw_centered_utf8_on_width(gb, panel_x, panel_y + 8, panel_w,
                                tr_text(&T_LANGUAGE_TITLE), 0x00);
    gb_fill_rect(gb, panel_x + 14, panel_y + 29, panel_w - 28, 1, 0x00);

    for (int i = 0; i < LANGUAGE_COUNT; i++) {
        int bx = panel_x + 8;
        int by = panel_y + 39 + i * 36;
        int bw = panel_w - 16;
        int bh = 30;
        bool active = (selected == i);
        gb_fill_rect(gb, bx, by, bw, bh, active ? 0x00 : 0xFF);
        gb_draw_rect(gb, bx, by, bw, bh, 0x00);
        draw_centered_utf8_on_width(gb, bx, by + 7, bw,
                                    s_language_options[i].label,
                                    active ? 0xFF : 0x00);
        if (active) {
            gb_fill_rect(gb, bx + 5, by + bh - 5, bw - 10, 1, 0xFF);
        }
    }
}

static const language_option_t *language_selected_option(void)
{
    int sel = (int)s_language_sel;
    if (sel < 0 || sel >= LANGUAGE_COUNT) return NULL;
    return &s_language_options[sel];
}

static void language_select_move_next(void)
{
    int next = ((int)s_language_sel + 1) % LANGUAGE_COUNT;
    s_language_sel = next;
    const language_option_t *option = &s_language_options[next];
    if (s_audio_ready) {
        audio_stop_playback();
        sfx_play_file(option->voice_path);
    }
    ui_set_dialogue(option->subtitle, 80);
    aura_ui_mark_dirty();
}

static void ui_language_save(void)
{
    nvs_handle_t nvs = 0;
    if (nvs_open("companion", NVS_READWRITE, &nvs) != ESP_OK) return;
    nvs_set_u8(nvs, "ui_lang", (uint8_t)s_ui_language);
    nvs_commit(nvs);
    nvs_close(nvs);
}

static void ui_language_load(void)
{
    nvs_handle_t nvs = 0;
    uint8_t lang = 0;
    if (nvs_open("companion", NVS_READONLY, &nvs) != ESP_OK) return;
    if (nvs_get_u8(nvs, "ui_lang", &lang) == ESP_OK && lang < LANGUAGE_COUNT) {
        s_ui_language = (aura_language_t)lang;
    }
    nvs_close(nvs);
    g_state.ui_language = (int)s_ui_language;
}

static bool language_select_confirm(void)
{
    int sel = (int)s_language_sel;
    if (sel < 0 || sel >= LANGUAGE_COUNT) {
        sfx_play(SFX_ERROR);
        return false;
    }
    s_ui_language = (aura_language_t)sel;
    g_state.ui_language = sel;
    ws_client_set_language(ui_language_code());
    s_language_confirmed = sel;
    s_language_select_open = false;
    ui_language_save();
    if (s_audio_ready) {
        audio_stop_playback();
    }
    aura_ui_clear_dialogue();
    aura_ui_mark_dirty();
    return true;
}

static bool outfit_is_unlocked(int outfit_idx)
{
    if (outfit_idx < 0 || outfit_idx >= 32) return false;
    return ((g_state.outfit_unlocked >> outfit_idx) & 1u) != 0;
}

/* 睡衣(0)/睡裙(2) 是隐藏服装：夜晚自动换上，不在衣柜/商店露出 */
static bool outfit_is_hidden_sleepwear(int outfit_idx)
{
    return outfit_idx == 0 || outfit_idx == 2;
}

/* 衣柜可见 = 已解锁且非隐藏睡衣类 */
static bool outfit_in_wardrobe(int outfit_idx)
{
    return outfit_is_unlocked(outfit_idx) && !outfit_is_hidden_sleepwear(outfit_idx);
}

static int next_unlocked_outfit(int current)
{
    int total = renderer_get_outfit_count();
    if (total <= 0) return 0;
    for (int step = 1; step <= total; step++) {
        int idx = (current + step) % total;
        if (outfit_in_wardrobe(idx)) return idx;
    }
    return 1;
}

static int unlocked_outfit_page(int current)
{
    int total = renderer_get_outfit_count();
    int page = 0;
    for (int idx = 0; idx < total; idx++) {
        if (!outfit_in_wardrobe(idx)) continue;
        if (idx == current) return page;
        page++;
    }
    return 0;
}

static int unlocked_outfit_count(void)
{
    int total = renderer_get_outfit_count();
    int count = 0;
    for (int idx = 0; idx < total; idx++) {
        if (outfit_in_wardrobe(idx)) count++;
    }
    return count > 0 ? count : 1;
}

static int normalize_unlocked_outfit(int current)
{
    if (outfit_in_wardrobe(current)) return current;
    int total = renderer_get_outfit_count();
    if (total <= 0) return 1;
    for (int idx = 0; idx < total; idx++) {
        if (outfit_in_wardrobe(idx)) return idx;
    }
    return 1;
}

/* ── 自动换装 ──────────────────────────────────────────────────────
 * 规则：
 *  - 世界模型进入 sleep/night_settle 阶段后自动换睡衣类。
 *  - 世界模型离开睡眠阶段后按温度/天气换回常服。
 *  - 白天按温度/天气从已拥有的常服里挑：雪或 <12℃ 优先冬装；
 *    <22℃ 或下雨挑休闲/古风；否则挑洋装/旗袍/休闲。
 *  - 用户手动指定当天优先（白天不自动换），次日 Aura 重新自选。
 */
/* ── 商店/衣柜预览姿势：加权随机（高兴/傲娇为主，倾听少量点缀）──
 * 只在切换选中项时重新随机，避免逐帧抖动。 */
static int preview_pose_roll(void)
{
    /* 权重: 高兴7(x3) 傲娇8(x3) 倾听2(x1) 倾听3(x1) */
    static const int pool[] = { 7, 7, 7, 8, 8, 8, 2, 3 };
    return pool[esp_random() % (sizeof(pool) / sizeof(pool[0]))];
}

static int s_preview_pose     = 0;
static int s_preview_pose_key = -1;     /* outfit_idx，变化时重掷 */

static int preview_pose_for(int outfit_idx)
{
    if (outfit_idx != s_preview_pose_key) {
        s_preview_pose_key = outfit_idx;
        /* 保证每次切换选中项姿势必换：重掷直到与上一个不同
         * （池中有 4 种取值，循环必然很快结束） */
        int p;
        do {
            p = preview_pose_roll();
        } while (p == s_preview_pose);
        s_preview_pose = p;
    }
    return s_preview_pose;
}

static bool auto_outfit_is_sleepwear(int idx)
{
    return idx == 0 || idx == 2;    /* 睡衣 / 睡裙 */
}

static int auto_outfit_pick(const int *cands, int n)
{
    int owned[9];
    int m = 0;
    for (int i = 0; i < n && m < 9; i++) {
        if (outfit_is_unlocked(cands[i])) owned[m++] = cands[i];
    }
    if (m == 0) return -1;
    return owned[esp_random() % m];
}

static int auto_outfit_choose_day(void)
{
    float t = g_state.temperature;
    int icon = g_state.weather_icon;    /* 0=sun 1=cloud 2=rain 3=snow */
    int pick = -1;
    if (icon == 3 || t < 12.0f) {
        static const int cold[] = { 5 };                 /* 冬装 */
        pick = auto_outfit_pick(cold, 1);
        if (pick < 0) {
            static const int cold_alt[] = { 3, 4, 8 };   /* 没冬装退休闲/汉服 */
            pick = auto_outfit_pick(cold_alt, 3);
        }
    } else if (icon == 2 || t < 22.0f) {
        static const int mild[] = { 3, 4, 7, 8 };        /* 休闲/马面裙/汉服 */
        pick = auto_outfit_pick(mild, 4);
    } else {
        static const int warm[] = { 1, 6, 3, 4 };        /* 洋装/旗袍/休闲 */
        pick = auto_outfit_pick(warm, 4);
    }
    if (pick < 0) {
        static const int any_day[] = { 1, 3, 4, 5, 6, 7, 8 };
        pick = auto_outfit_pick(any_day, 7);
    }
    return pick;
}

/* 由 input_task 的 IDLE 分支调用（无菜单/商店/衣柜/帘幕时）。 */
static void auto_outfit_tick(void)
{
    if (!g_state.companion_state_ready) return;
    if (!g_state.world_outfit_mode_ready) return;
    /* 开机初期天气还是默认值，等 15 秒再开始判定 */
    if (esp_timer_get_time() < 15 * 1000000LL) return;

    int md = g_state.month * 100 + g_state.day;
    bool sleepwear = g_state.world_sleepwear_requested;
    int mode = sleepwear ? 1 : 0;
    if (mode == s_auto_outfit_mode) return;

    /* 白天用户今天手动指定过则不自动换；进入睡眠时强制换睡衣，
     * 不受当天手动服装选择阻止。 */
    if (s_outfit_pin_md == md && !sleepwear) {
        s_auto_outfit_mode = mode;
        return;
    }

    int target = -1;
    if (sleepwear) {
        /* 已经穿着睡衣类就不动。 */
        if (!auto_outfit_is_sleepwear(g_state.current_outfit)) {
            /* 睡衣/睡裙是隐藏服装（默认解锁）：默认睡衣，
             * 好感度 Lv4+ 时有一半概率换上睡裙 */
            target = 0;
            if (g_state.affinity_level >= 4 && (esp_random() % 2) == 1) {
                target = 2;
            }
        }
    } else {
        /* 白天绝不主动换睡衣类；醒来时若还穿着睡衣，换回日装 */
        target = auto_outfit_choose_day();
    }
    s_auto_outfit_mode = mode;
    if (target < 0 || target == g_state.current_outfit) return;

    ESP_LOGI(TAG, "World outfit: %s -> idx %d (scene=%d temp=%.1f icon=%d)",
             sleepwear ? "sleepwear" : "daywear", target, g_state.current_scene,
             g_state.temperature, g_state.weather_icon);
    s_curtain_target = target;
    s_curtain_tick   = 1;
    g_state.current_outfit = target;
    ui_set_dialogue(tr_text(sleepwear ? &T_AUTO_SLEEPWEAR : &T_AUTO_DAYWEAR), 20);
    aura_companion_state_cache_save();
}

#define SPEAKING_SAFETY_TIMEOUT_MS 120000
#define PROCESSING_TIMEOUT_TICKS   300
#define CANCEL_HOLD_MS             700
#define PTT_MIN_HOLD_MS            600   /* 按住说话：不足 600ms 松开视为误触，取消发送 */
#define PTT_MIN_RECORD_MS          300   /* 开始录音后过快松手视为取消，避免空 ASR 报错 */
#define PTT_START_TONE_TIMEOUT_MS  1500  /* 等待开始音完整播放，避免录音静音流抢占 I2S TX */
#define CONTINUOUS_VAD_SILENCE_THRESHOLD 220
#define CONTINUOUS_VAD_SPEECH_START_RMS  280
#define CONTINUOUS_VAD_SPEECH_START_PEAK 1200
#define CONTINUOUS_VAD_MIN_SPEECH_MS      240
#define CONTINUOUS_VAD_SILENCE_MS         2000
#define CONTINUOUS_VAD_NO_SPEECH_MS       4500
#define CONTINUOUS_VAD_MAX_RECORD_MS      30000
#define CONTINUOUS_SESSION_MAX_MS         (10 * 60 * 1000)
#define CONTINUOUS_SESSION_MAX_TURNS      20
#define DIALOGUE_PAGE_TICKS        36

#define OPUS_FRAME_SAMPLES 960
#define OPUS_MAX_PACKET_BYTES 1000
#define PCM_UPLOAD_START_TIMEOUT_MS 3500
#define PCM_UPLOAD_START_RETRY_LOG_MS 500
#define PCM_UPLOAD_DRAIN_TIMEOUT_MS 2500

typedef enum {
    /* v0.1: 白日梦(Quest)暂不上菜单，底层 minigame 代码保留 */
    MAIN_MENU_SHOP = 0,
    MAIN_MENU_WARDROBE,
    MAIN_MENU_DESSERT,
    MAIN_MENU_VOLUME,
    MAIN_MENU_LANGUAGE,
    MAIN_MENU_WIFI,
    MAIN_MENU_UPDATE,
    MAIN_MENU_COUNT,
} main_menu_item_t;

typedef enum {
    VOLUME_MENU_DOWN = 0,
    VOLUME_MENU_UP,
    VOLUME_MENU_BACK,
    VOLUME_MENU_COUNT,
} volume_menu_item_t;

typedef enum {
    WIFI_MENU_SLOT_1 = 0,
    WIFI_MENU_SLOT_2,
    WIFI_MENU_RECONNECT,
    WIFI_MENU_PROVISION,
    WIFI_MENU_BACK,
    WIFI_MENU_COUNT,
} wifi_menu_item_t;

static char s_wifi_slot_menu_labels[2][72] = {{0}};

static bool task_wdt_add_current(const char *name)
{
    esp_err_t err = esp_task_wdt_add(NULL);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Task watchdog subscribed: %s", name ? name : "?");
        return true;
    }
    if (err == ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "Task watchdog not initialized for %s", name ? name : "?");
        return false;
    }
    ESP_LOGW(TAG, "Task watchdog subscribe failed for %s: 0x%x", name ? name : "?", err);
    return false;
}

static void task_wdt_reset_if(bool enabled)
{
    if (enabled) {
        (void)esp_task_wdt_reset();
    }
}

static void task_wdt_reset_current(void)
{
    (void)esp_task_wdt_reset();
}

#if AURA_GPIO_SCAN_DIAG
static void gpio_scan_diag_init(void)
{
    ESP_LOGI(TAG, "DIAG gpio read-only scan enabled for candidate button pins");
}

static void gpio_scan_diag_poll(void)
{
    static const gpio_num_t pins[] = {
        GPIO_NUM_0,   /* Waveshare RLCD 4.2 BOOT/chat key */
        GPIO_NUM_18,  /* optional shell KEY on older Aura builds */
        GPIO_NUM_1,   /* external shell button candidates, read-only */
        GPIO_NUM_2,
        GPIO_NUM_3,
        GPIO_NUM_4,
        GPIO_NUM_7,
        GPIO_NUM_19,
        GPIO_NUM_20,
        GPIO_NUM_35,
        GPIO_NUM_36,
        GPIO_NUM_37,
        GPIO_NUM_42,
        GPIO_NUM_43,
        GPIO_NUM_44,
        GPIO_NUM_47,
        GPIO_NUM_48,
    };
    static bool initialized = false;
    static int last_levels[sizeof(pins) / sizeof(pins[0])];
    int64_t now_ms = esp_timer_get_time() / 1000;

    if (!initialized) {
        for (size_t i = 0; i < sizeof(pins) / sizeof(pins[0]); i++) {
            last_levels[i] = gpio_get_level(pins[i]);
            ESP_LOGI(TAG, "DIAG gpio initial pin=%d level=%d", (int)pins[i], last_levels[i]);
        }
        initialized = true;
        return;
    }

    for (size_t i = 0; i < sizeof(pins) / sizeof(pins[0]); i++) {
        int level = gpio_get_level(pins[i]);
        if (level != last_levels[i]) {
            ESP_LOGW(
                TAG,
                "DIAG gpio change pin=%d %d->%d at=%lldms",
                (int)pins[i],
                last_levels[i],
                level,
                (long long)now_ms
            );
            if (ws_client_is_ready()) {
                ws_client_send_gpio_diag((int)pins[i], last_levels[i], level);
            }
            last_levels[i] = level;
        }
    }
}
#else
static void gpio_scan_diag_init(void) {}
static void gpio_scan_diag_poll(void) {}
#endif

static void log_input_diag(const char *reason, aura_fsm_state_t state, bool ww_listening)
{
    static int64_t s_last_input_diag_ms = 0;
    int64_t now_ms = esp_timer_get_time() / 1000;
    if (reason && strcmp(reason, "tick") == 0 &&
        (now_ms - s_last_input_diag_ms) < 5000) {
        return;
    }
    s_last_input_diag_ms = now_ms;
    ESP_LOGI(
        TAG,
        "DIAG input %s state=%s key=%d boot=%d ws=%d audio=%d tts=%d ww=%d menu=%d lang=%d",
        reason ? reason : "?",
        fsm_state_name(state),
        gpio_get_level(BTN_KEY_PIN),
        gpio_get_level(BTN_BOOT_PIN),
        ws_client_is_connected() ? 1 : 0,
        audio_is_playing() ? 1 : 0,
        ws_client_is_tts_active() ? 1 : 0,
        ww_listening ? 1 : 0,
        s_menu_open ? 1 : 0,
        s_language_select_open ? 1 : 0
    );
}

static void button_probe_poll(void)
{
    static bool initialized = false;
    static const gpio_num_t pins[] = {
        BTN_BOOT_PIN,
        BTN_KEY_PIN,
    };
    static int last_levels[sizeof(pins) / sizeof(pins[0])];

    if (!initialized) {
        for (size_t i = 0; i < sizeof(pins) / sizeof(pins[0]); i++) {
            last_levels[i] = gpio_get_level(pins[i]);
        }
        initialized = true;
        return;
    }

    for (size_t i = 0; i < sizeof(pins) / sizeof(pins[0]); i++) {
        int level = gpio_get_level(pins[i]);
        if (level != last_levels[i]) {
            ESP_LOGW(TAG, "DIAG button probe pin=%d %d->%d",
                     (int)pins[i], last_levels[i], level);
            if (ws_client_is_ready()) {
                ws_client_send_gpio_diag((int)pins[i], last_levels[i], level);
            }
            last_levels[i] = level;
        }
    }
}

static void create_task_checked(
    TaskFunction_t task_func,
    const char *name,
    uint32_t stack_depth,
    UBaseType_t priority,
    BaseType_t core_id
)
{
    BaseType_t ok = xTaskCreatePinnedToCore(
        task_func,
        name,
        stack_depth,
        NULL,
        priority,
        NULL,
        core_id
    );
    if (ok != pdPASS) {
        ESP_LOGE(
            TAG,
            "Task create failed: %s stack=%u prio=%u core=%ld internal_free=%u spiram_free=%u",
            name ? name : "?",
            (unsigned)stack_depth,
            (unsigned)priority,
            (long)core_id,
            (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
            (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM)
        );
    } else {
        ESP_LOGI(
            TAG,
            "Task created: %s stack=%u prio=%u core=%ld internal_free=%u spiram_free=%u",
            name ? name : "?",
            (unsigned)stack_depth,
            (unsigned)priority,
            (long)core_id,
            (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
            (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM)
        );
    }
}

typedef struct {
    const int16_t *buffer;
    volatile size_t produced_samples;
    size_t sent_samples;
    size_t capacity_samples;
    bool server_vad_enabled;
    bool start_sent;
    volatile bool running;
    volatile bool stop_requested;
    volatile bool abort_requested;
    int64_t stop_requested_at_ms;
    int64_t begin_at_ms;
    int64_t start_sent_at_ms;
    int64_t first_packet_sent_at_ms;
    int64_t last_start_retry_log_ms;
    size_t stop_requested_produced_samples;
    size_t stop_requested_sent_samples;
    uint32_t sent_packet_count;
    uint32_t start_retry_count;
} pcm_upload_session_t;

static pcm_upload_session_t s_pcm_upload = {0};
static volatile bool s_pcm_upload_failed = false;

static void pcm_upload_abort(void);

static bool pcm_upload_take_failure(void)
{
    if (!s_pcm_upload_failed) return false;
    s_pcm_upload_failed = false;
    return true;
}

static void pcm_upload_reset(void)
{
    s_pcm_upload.buffer = NULL;
    s_pcm_upload.produced_samples = 0;
    s_pcm_upload.sent_samples = 0;
    s_pcm_upload.capacity_samples = 0;
    s_pcm_upload.server_vad_enabled = AURA_SERVER_VAD_DEFAULT;
    s_pcm_upload.start_sent = false;
    s_pcm_upload.running = false;
    s_pcm_upload.stop_requested = false;
    s_pcm_upload.abort_requested = false;
    s_pcm_upload.stop_requested_at_ms = 0;
    s_pcm_upload.begin_at_ms = 0;
    s_pcm_upload.start_sent_at_ms = 0;
    s_pcm_upload.first_packet_sent_at_ms = 0;
    s_pcm_upload.last_start_retry_log_ms = 0;
    s_pcm_upload.stop_requested_produced_samples = 0;
    s_pcm_upload.stop_requested_sent_samples = 0;
    s_pcm_upload.sent_packet_count = 0;
    s_pcm_upload.start_retry_count = 0;
}

static bool pcm_upload_begin(const int16_t *buffer, size_t capacity_samples,
                             bool server_vad_enabled)
{
    if (!buffer || capacity_samples == 0) {
        return false;
    }
    if (s_pcm_upload.running) {
        int64_t now_ms = esp_timer_get_time() / 1000;
        int64_t stale_ms = s_pcm_upload.begin_at_ms > 0 ? now_ms - s_pcm_upload.begin_at_ms : 0;
        ESP_LOGW(TAG, "PCM upload busy before new session: stale_ms=%lld start_sent=%d sent=%d produced=%d",
                 (long long)stale_ms,
                 s_pcm_upload.start_sent ? 1 : 0,
                 (int)s_pcm_upload.sent_samples,
                 (int)s_pcm_upload.produced_samples);
        ESP_LOGI(TAG, "VOICE_TIMING upload_busy_reject stale_running_since_ms=%lld stale_start_sent=%d sent=%d produced=%d",
                 (long long)stale_ms,
                 s_pcm_upload.start_sent ? 1 : 0,
                 (int)s_pcm_upload.sent_samples,
                 (int)s_pcm_upload.produced_samples);
        if (!s_pcm_upload.start_sent) {
            pcm_upload_reset();
        } else {
            pcm_upload_abort();
            for (int i = 0; i < 20 && s_pcm_upload.running; i++) {
                vTaskDelay(pdMS_TO_TICKS(10));
            }
            if (s_pcm_upload.running) {
                ESP_LOGE(TAG, "PCM upload still busy after abort wait, reject new session");
                return false;
            }
        }
    }
    s_pcm_upload.buffer = buffer;
    s_pcm_upload.produced_samples = 0;
    s_pcm_upload.sent_samples = 0;
    s_pcm_upload.capacity_samples = capacity_samples;
    s_pcm_upload.server_vad_enabled = server_vad_enabled;
    s_pcm_upload.start_sent = false;
    s_pcm_upload.stop_requested = false;
    s_pcm_upload.abort_requested = false;
    s_pcm_upload.stop_requested_at_ms = 0;
    s_pcm_upload.begin_at_ms = esp_timer_get_time() / 1000;
    s_pcm_upload.start_sent_at_ms = 0;
    s_pcm_upload.first_packet_sent_at_ms = 0;
    s_pcm_upload.last_start_retry_log_ms = 0;
    s_pcm_upload.stop_requested_produced_samples = 0;
    s_pcm_upload.stop_requested_sent_samples = 0;
    s_pcm_upload.sent_packet_count = 0;
    s_pcm_upload.start_retry_count = 0;
    s_pcm_upload_failed = false;
    s_pcm_upload.running = true;
    ESP_LOGI(TAG, "VOICE_TIMING upload_begin server_vad=%d capacity_samples=%d",
             server_vad_enabled ? 1 : 0, (int)capacity_samples);
    return true;
}

static void pcm_upload_update(size_t produced_samples)
{
    if (!s_pcm_upload.running) return;
    if (produced_samples > s_pcm_upload.capacity_samples) {
        produced_samples = s_pcm_upload.capacity_samples;
    }
    s_pcm_upload.produced_samples = produced_samples;
}

static void pcm_upload_finish(size_t produced_samples)
{
    pcm_upload_update(produced_samples);
    if (!s_pcm_upload.running) return;
    s_pcm_upload.stop_requested_at_ms = esp_timer_get_time() / 1000;
    s_pcm_upload.stop_requested_produced_samples = s_pcm_upload.produced_samples;
    s_pcm_upload.stop_requested_sent_samples = s_pcm_upload.sent_samples;
    size_t pending_samples = 0;
    if (s_pcm_upload.stop_requested_produced_samples > s_pcm_upload.stop_requested_sent_samples) {
        pending_samples = s_pcm_upload.stop_requested_produced_samples - s_pcm_upload.stop_requested_sent_samples;
    }
    ESP_LOGW(TAG, "PCM upload finish requested: produced=%d sent=%d pending=%d samples (%.2fs)",
             (int)s_pcm_upload.stop_requested_produced_samples,
             (int)s_pcm_upload.stop_requested_sent_samples,
             (int)pending_samples,
             pending_samples / 16000.0f);
    ESP_LOGI(TAG, "VOICE_TIMING upload_finish_requested since_begin_ms=%lld produced=%d sent=%d pending=%d packets=%u",
             (long long)(s_pcm_upload.stop_requested_at_ms - s_pcm_upload.begin_at_ms),
             (int)s_pcm_upload.stop_requested_produced_samples,
             (int)s_pcm_upload.stop_requested_sent_samples,
             (int)pending_samples,
             (unsigned)s_pcm_upload.sent_packet_count);
    s_pcm_upload.stop_requested = true;
}

static void pcm_upload_abort(void)
{
    if (!s_pcm_upload.running) return;
    s_pcm_upload.abort_requested = true;
    s_pcm_upload.stop_requested = true;
}

static void pcm_upload_task(void *arg)
{
    ESP_LOGI(TAG, "PCM upload task started");
    aura_opus_encoder_t *opus_enc = aura_opus_encoder_create(16000, 1, 60);
    uint8_t *opus_buf = heap_caps_malloc(OPUS_MAX_PACKET_BYTES, MALLOC_CAP_INTERNAL);
    int16_t *opus_tail = heap_caps_calloc(OPUS_FRAME_SAMPLES, sizeof(int16_t), MALLOC_CAP_INTERNAL);

    if (!opus_enc || !opus_buf || !opus_tail) {
        ESP_LOGE(TAG, "Failed to allocate Opus encoder resources");
        vTaskDelete(NULL);
        return;
    }
    aura_opus_encoder_set_dtx(opus_enc, false);
    aura_opus_encoder_set_complexity(opus_enc, 3);
    aura_opus_encoder_set_bitrate(opus_enc, 24000);
    ESP_LOGI(TAG, "Opus encoder ready (16kHz mono, 60ms frame, bitrate=24k, complexity=3)");
    ESP_LOGI(TAG, "PCM upload task stack high-water mark: %u words",
             (unsigned)uxTaskGetStackHighWaterMark(NULL));

    while (1) {
        if (!s_pcm_upload.running) {
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }

        if (s_pcm_upload.abort_requested) {
            pcm_upload_reset();
            vTaskDelay(pdMS_TO_TICKS(1));
            continue;
        }

        if (!s_pcm_upload.start_sent) {
            int64_t now_ms = esp_timer_get_time() / 1000;
            if (!ws_client_is_ready() ||
                ws_client_send_start_with_server_vad(s_pcm_upload.server_vad_enabled) != ESP_OK) {
                s_pcm_upload.start_retry_count++;
                if (now_ms - s_pcm_upload.last_start_retry_log_ms >= PCM_UPLOAD_START_RETRY_LOG_MS) {
                    s_pcm_upload.last_start_retry_log_ms = now_ms;
                    ESP_LOGW(TAG, "PCM upload start pending: retries=%u waited_ms=%lld ws_ready=%d",
                             (unsigned)s_pcm_upload.start_retry_count,
                             (long long)(now_ms - s_pcm_upload.begin_at_ms),
                             ws_client_is_ready() ? 1 : 0);
                }
                if (now_ms - s_pcm_upload.begin_at_ms >= PCM_UPLOAD_START_TIMEOUT_MS) {
                    ESP_LOGE(TAG, "PCM upload start timeout %dms retries=%u",
                             PCM_UPLOAD_START_TIMEOUT_MS,
                             (unsigned)s_pcm_upload.start_retry_count);
                    s_pcm_upload_failed = true;
                    pcm_upload_reset();
                    continue;
                }
                vTaskDelay(pdMS_TO_TICKS(80));
                continue;
            }
            s_pcm_upload.start_sent = true;
            s_pcm_upload.start_sent_at_ms = esp_timer_get_time() / 1000;
            ESP_LOGI(TAG, "VOICE_TIMING upload_start_sent since_begin_ms=%lld server_vad=%d",
                     (long long)(s_pcm_upload.start_sent_at_ms - s_pcm_upload.begin_at_ms),
                     s_pcm_upload.server_vad_enabled ? 1 : 0);
        }

        while (!s_pcm_upload.abort_requested &&
               s_pcm_upload.sent_samples + OPUS_FRAME_SAMPLES <= s_pcm_upload.produced_samples) {
            if (s_pcm_upload.stop_requested && s_pcm_upload.stop_requested_at_ms > 0) {
                int64_t now_ms = esp_timer_get_time() / 1000;
                int64_t drain_ms = now_ms - s_pcm_upload.stop_requested_at_ms;
                if (drain_ms >= PCM_UPLOAD_DRAIN_TIMEOUT_MS) {
                    size_t dropped_samples = 0;
                    if (s_pcm_upload.produced_samples > s_pcm_upload.sent_samples) {
                        dropped_samples = s_pcm_upload.produced_samples - s_pcm_upload.sent_samples;
                    }
                    ESP_LOGE(TAG, "PCM upload drain timeout %dms: sent=%d produced=%d dropped=%d",
                             PCM_UPLOAD_DRAIN_TIMEOUT_MS,
                             (int)s_pcm_upload.sent_samples,
                             (int)s_pcm_upload.produced_samples,
                             (int)dropped_samples);
                    ESP_LOGI(TAG, "VOICE_TIMING upload_drain_timeout since_finish_request_ms=%lld sent=%d produced=%d dropped=%d packets=%u",
                             (long long)drain_ms,
                             (int)s_pcm_upload.sent_samples,
                             (int)s_pcm_upload.produced_samples,
                             (int)dropped_samples,
                             (unsigned)s_pcm_upload.sent_packet_count);
                    s_pcm_upload_failed = true;
                    pcm_upload_reset();
                    ws_client_send_cancel("upload_drain_timeout");
                    break;
                }
            }
            const int16_t *pcm = &s_pcm_upload.buffer[s_pcm_upload.sent_samples];
            size_t opus_len = 0;
            if (!aura_opus_encoder_encode(opus_enc, pcm, OPUS_FRAME_SAMPLES,
                                          opus_buf, OPUS_MAX_PACKET_BYTES, &opus_len)) {
                ESP_LOGW(TAG, "Opus encode failed on full frame");
                s_pcm_upload_failed = true;
                pcm_upload_reset();
                break;
            }
            if (ws_client_send_pcm(opus_buf, opus_len) != ESP_OK) {
                ESP_LOGW(TAG, "Opus upload send failed at sample=%d", (int)s_pcm_upload.sent_samples);
                s_pcm_upload_failed = true;
                pcm_upload_reset();
                break;
            }
            s_pcm_upload.sent_packet_count++;
            if (s_pcm_upload.first_packet_sent_at_ms == 0) {
                s_pcm_upload.first_packet_sent_at_ms = esp_timer_get_time() / 1000;
                ESP_LOGI(TAG, "VOICE_TIMING upload_first_packet since_begin_ms=%lld since_start_ms=%lld opus_bytes=%u",
                         (long long)(s_pcm_upload.first_packet_sent_at_ms - s_pcm_upload.begin_at_ms),
                         (long long)(s_pcm_upload.start_sent_at_ms > 0
                            ? s_pcm_upload.first_packet_sent_at_ms - s_pcm_upload.start_sent_at_ms
                            : 0),
                         (unsigned)opus_len);
            }
            s_pcm_upload.sent_samples += OPUS_FRAME_SAMPLES;
            if ((s_pcm_upload.sent_samples % (OPUS_FRAME_SAMPLES * 10)) == 0) {
                ESP_LOGI(TAG, "PCM upload stack high-water mark: %u words",
                         (unsigned)uxTaskGetStackHighWaterMark(NULL));
            }
            vTaskDelay(pdMS_TO_TICKS(1));
        }

        if (s_pcm_upload.running && s_pcm_upload.abort_requested) {
            ESP_LOGW(TAG, "PCM upload aborted: produced=%d sent=%d",
                     (int)s_pcm_upload.produced_samples,
                     (int)s_pcm_upload.sent_samples);
            pcm_upload_reset();
            vTaskDelay(pdMS_TO_TICKS(1));
            continue;
        }

        if (s_pcm_upload.running &&
            s_pcm_upload.stop_requested) {
            size_t remaining = s_pcm_upload.produced_samples - s_pcm_upload.sent_samples;
            if (remaining > 0) {
                memset(opus_tail, 0, OPUS_FRAME_SAMPLES * sizeof(int16_t));
                memcpy(opus_tail, &s_pcm_upload.buffer[s_pcm_upload.sent_samples],
                       remaining * sizeof(int16_t));
                size_t opus_len = 0;
                if (aura_opus_encoder_encode(opus_enc, opus_tail, OPUS_FRAME_SAMPLES,
                                             opus_buf, OPUS_MAX_PACKET_BYTES, &opus_len)) {
                    if (ws_client_send_pcm(opus_buf, opus_len) == ESP_OK) {
                        s_pcm_upload.sent_samples = s_pcm_upload.produced_samples;
                        s_pcm_upload.sent_packet_count++;
                        if (s_pcm_upload.first_packet_sent_at_ms == 0) {
                            s_pcm_upload.first_packet_sent_at_ms = esp_timer_get_time() / 1000;
                            ESP_LOGI(TAG, "VOICE_TIMING upload_first_packet since_begin_ms=%lld since_start_ms=%lld opus_bytes=%u final=1",
                                     (long long)(s_pcm_upload.first_packet_sent_at_ms - s_pcm_upload.begin_at_ms),
                                     (long long)(s_pcm_upload.start_sent_at_ms > 0
                                        ? s_pcm_upload.first_packet_sent_at_ms - s_pcm_upload.start_sent_at_ms
                                        : 0),
                                     (unsigned)opus_len);
                        }
                    } else {
                        ESP_LOGW(TAG, "Final Opus upload send failed");
                        s_pcm_upload_failed = true;
                        pcm_upload_reset();
                        vTaskDelay(pdMS_TO_TICKS(1));
                        continue;
                    }
                } else {
                    ESP_LOGW(TAG, "Final Opus encode failed");
                    s_pcm_upload_failed = true;
                    pcm_upload_reset();
                    vTaskDelay(pdMS_TO_TICKS(1));
                    continue;
                }
            }

            if (s_pcm_upload.start_sent) {
                esp_err_t stop_err = ws_client_send_stop();
                if (stop_err != ESP_OK) {
                    ESP_LOGE(TAG, "PCM upload stop send failed");
                    ESP_LOGI(TAG, "VOICE_TIMING upload_stop_send_failed since_begin_ms=%lld packets=%u sent_samples=%d",
                             (long long)((esp_timer_get_time() / 1000) - s_pcm_upload.begin_at_ms),
                             (unsigned)s_pcm_upload.sent_packet_count,
                             (int)s_pcm_upload.sent_samples);
                    s_pcm_upload_failed = true;
                    pcm_upload_reset();
                    continue;
                }
            }
            int64_t drain_ms = 0;
            if (s_pcm_upload.stop_requested_at_ms > 0) {
                drain_ms = (esp_timer_get_time() / 1000) - s_pcm_upload.stop_requested_at_ms;
            }
            ESP_LOGW(TAG, "Opus upload finished: sent=%d samples drain_ms=%lld pending_at_stop=%d",
                     (int)s_pcm_upload.sent_samples,
                     (long long)drain_ms,
                     (int)(s_pcm_upload.stop_requested_produced_samples > s_pcm_upload.stop_requested_sent_samples
                        ? (s_pcm_upload.stop_requested_produced_samples - s_pcm_upload.stop_requested_sent_samples)
                        : 0));
            ESP_LOGI(TAG, "VOICE_TIMING upload_stop_sent since_begin_ms=%lld since_finish_request_ms=%lld packets=%u sent_samples=%d",
                     (long long)((esp_timer_get_time() / 1000) - s_pcm_upload.begin_at_ms),
                     (long long)drain_ms,
                     (unsigned)s_pcm_upload.sent_packet_count,
                     (int)s_pcm_upload.sent_samples);
            pcm_upload_reset();
            vTaskDelay(pdMS_TO_TICKS(1));
            continue;
        }

        vTaskDelay(pdMS_TO_TICKS(1));
    }
}

static void ui_set_dialogue(const char *text, int ttl_ticks)
{
    aura_ui_set_dialogue(text, ttl_ticks);
}

static void ui_set_agent_panel(bool visible, int progress,
                               const char *title, const char *status)
{
    aura_ui_set_agent_panel(visible, progress, title, status);
}

static void get_menu_labels(const char **header, const char *opts[], int *count)
{
    if (header) *header = tr_text(&T_MENU_TITLE);
    if (!opts || !count) return;
    opts[MAIN_MENU_SHOP] = tr_text(&T_MENU_SHOP);
    opts[MAIN_MENU_WARDROBE] = tr_text(&T_MENU_WARDROBE);
    opts[MAIN_MENU_DESSERT] = tr_text(&T_MENU_DESSERT);
    opts[MAIN_MENU_VOLUME] = tr_text(&T_MENU_VOLUME);
    opts[MAIN_MENU_LANGUAGE] = tr_text(&T_MENU_LANGUAGE);
    opts[MAIN_MENU_WIFI] = tr_text(&T_MENU_WIFI);
    opts[MAIN_MENU_UPDATE] = tr_text(&T_MENU_UPDATE);
    *count = MAIN_MENU_COUNT;
}

static void get_volume_menu_labels(const char **header, const char *opts[], int *count)
{
    if (header) *header = tr_text(&T_VOLUME_LABEL);
    if (!opts || !count) return;
    opts[VOLUME_MENU_DOWN] = tr_text(&T_VOLUME_DOWN);
    opts[VOLUME_MENU_UP] = tr_text(&T_VOLUME_UP);
    opts[VOLUME_MENU_BACK] = tr_text(&T_MENU_BACK);
    *count = VOLUME_MENU_COUNT;
}

static void get_wifi_menu_labels(const char **header, const char *opts[], int *count)
{
    if (header) *header = tr_text(&T_MENU_WIFI);
    if (!opts || !count) return;
    for (int slot = 0; slot < 2; ++slot) {
        char ssid[33] = {0};
        bool saved = wifi_manager_get_saved_ssid(slot, ssid, sizeof(ssid));
        if (saved) {
            /* The row identifies the slot; show the saved SSID itself. */
            snprintf(s_wifi_slot_menu_labels[slot], sizeof(s_wifi_slot_menu_labels[slot]),
                     "%s", ssid);
        } else if (s_ui_language == AURA_LANG_EN) {
            snprintf(s_wifi_slot_menu_labels[slot], sizeof(s_wifi_slot_menu_labels[slot]),
                     "Wi-Fi %d: Empty", slot + 1);
        } else if (s_ui_language == AURA_LANG_JA) {
            snprintf(s_wifi_slot_menu_labels[slot], sizeof(s_wifi_slot_menu_labels[slot]),
                     "Wi-Fi %d: 空き", slot + 1);
        } else {
            snprintf(s_wifi_slot_menu_labels[slot], sizeof(s_wifi_slot_menu_labels[slot]),
                     "Wi-Fi %d：未保存", slot + 1);
        }
        opts[slot == 0 ? WIFI_MENU_SLOT_1 : WIFI_MENU_SLOT_2] = s_wifi_slot_menu_labels[slot];
    }
    opts[WIFI_MENU_RECONNECT] = tr_text(&T_WIFI_ACTION_RECONNECT);
    opts[WIFI_MENU_PROVISION] = tr_text(&T_WIFI_ACTION_SETUP);
    opts[WIFI_MENU_BACK] = tr_text(&T_MENU_BACK);
    *count = WIFI_MENU_COUNT;
}

static void show_wifi_provisioning_dialogue(void)
{
    char provision_text[112];
    snprintf(provision_text, sizeof(provision_text), "%s %s %s",
             tr_text(&T_WIFI_SETUP),
             wifi_manager_get_provisioning_ssid(),
             wifi_manager_get_provisioning_url());
    ui_set_dialogue(provision_text, 600);
}

static void wifi_menu_reconnect(void)
{
    if (!s_wifi_manager_ready) {
        ui_set_dialogue(tr_text(&T_WIFI_STARTING), 20);
        return;
    }
    if (wifi_manager_is_provisioning()) {
        wifi_manager_stop_provisioning();
    }
    esp_err_t err = wifi_manager_connect();
    ui_set_dialogue(err == ESP_OK ? tr_text(&T_WIFI_RECONNECT) : tr_text(&T_WIFI_OFFLINE), 30);
}

static void wifi_menu_connect_saved_slot(int slot)
{
    if (!s_wifi_manager_ready) {
        ui_set_dialogue(tr_text(&T_WIFI_STARTING), 20);
        return;
    }
    esp_err_t err = wifi_manager_connect_saved_network(slot);
    ui_set_dialogue(err == ESP_OK ? tr_text(&T_WIFI_RECONNECT) : tr_text(&T_WIFI_OFFLINE), 30);
}

static void wifi_menu_start_provisioning(void)
{
    if (!s_wifi_manager_ready) {
        ui_set_dialogue(tr_text(&T_WIFI_STARTING), 20);
        return;
    }
    esp_err_t err = wifi_manager_start_provisioning();
    if (err == ESP_OK) {
        show_wifi_provisioning_dialogue();
    } else {
        ESP_LOGE(TAG, "Wi-Fi provisioning failed: 0x%x", err);
        ui_set_dialogue(tr_text(&T_WIFI_SETUP_ERR), 30);
    }
}

static void ota_status_changed(aura_ota_status_t status, int progress, void *ctx)
{
    (void)ctx;
    const i18n_text_t *text = &T_OTA_FAILED;
    bool show_progress = false;
    int ttl = 600;
    switch (status) {
        case AURA_OTA_CHECKING: text = &T_OTA_CHECKING; break;
        case AURA_OTA_DOWNLOADING_RESOURCES:
            text = &T_OTA_RESOURCES;
            show_progress = true;
            break;
        case AURA_OTA_DOWNLOADING_APP:
            text = &T_OTA_APP;
            show_progress = true;
            break;
        case AURA_OTA_VERIFYING: text = &T_OTA_VERIFYING; break;
        case AURA_OTA_UP_TO_DATE:
            text = &T_OTA_CURRENT;
            ttl = 35;
            ws_client_resume_after_ota();
            break;
        case AURA_OTA_RESTARTING: text = &T_OTA_RESTARTING; break;
        case AURA_OTA_FAILED:
        default:
            text = &T_OTA_FAILED;
            ttl = 50;
            ws_client_resume_after_ota();
            break;
    }
    char message[96];
    if (show_progress) {
        snprintf(message, sizeof(message), "%s %d%%", tr_text(text), progress);
        ui_set_dialogue(message, ttl);
    } else {
        ui_set_dialogue(tr_text(text), ttl);
    }
    aura_ui_mark_dirty();
}

static void start_ota_update_from_menu(void)
{
    s_menu_open = false;
    s_volume_menu_open = false;
    s_wifi_menu_open = false;
    if (!wifi_manager_is_connected()) {
        ui_set_dialogue(tr_text(&T_WIFI_OFFLINE), 40);
        return;
    }
    if (ota_update_is_running()) {
        ui_set_dialogue(tr_text(&T_OTA_BUSY), 40);
        return;
    }
    ws_client_prepare_for_ota();
    /* esp_websocket_client_destroy() releases its task asynchronously. */
    vTaskDelay(pdMS_TO_TICKS(1000));
    /* The network task already owns an internal-RAM stack. Asking it to run
     * OTA avoids a second contiguous stack allocation at the point where TLS
     * also needs internal memory. */
    s_ota_manual_requested = true;
    ota_status_changed(AURA_OTA_CHECKING, 0, NULL);
}

static void purchase_selected_dessert(void)
{
    if (ws_client_dessert_purchase_pending()) return;
    const dessert_item_t *dessert = &s_dessert_catalog[s_dessert_sel];
    if (g_state.coins < dessert->price) {
        ui_set_dialogue(tr_text(&T_NOT_ENOUGH), 20);
        return;
    }
    if (!ws_client_is_ready()) {
        ui_set_dialogue(tr_text(&T_DESSERT_OFFLINE), 30);
        return;
    }
    ui_set_dialogue(tr_text(&T_DESSERT_PENDING), 18);
    aura_ui_mark_dirty();
    if (ws_client_send_dessert_purchase(dessert->id, g_state.coins) != ESP_OK) {
        ui_set_dialogue(tr_text(&T_VOICE_OFFLINE), 30);
        aura_ui_mark_dirty();
    }
}

static int clamp_companion_stat(int value)
{
    if (value < 0) return 0;
    if (value > AURA_COMPANION_STAT_MAX) return AURA_COMPANION_STAT_MAX;
    return value;
}

static int clamp_companion_beans(int value)
{
    if (value < 0) return 0;
    if (value > AURA_BEANS_MAX) return AURA_BEANS_MAX;
    return value;
}

static int clamp_affinity_level(int value)
{
    if (value < 1) return 1;
    if (value > 5) return 5;
    return value;
}

static bool voice_button_any_pressed(void)
{
    return gpio_get_level(BTN_KEY_PIN) == 0;
}

static bool voice_button_wait_release_with_timeout(int timeout_ms)
{
    int64_t start_ms = esp_timer_get_time() / 1000;
    while (voice_button_any_pressed()) {
        task_wdt_reset_current();
        if ((esp_timer_get_time() / 1000) - start_ms >= timeout_ms) {
            ESP_LOGW(TAG, "Voice button release wait timed out after %d ms", timeout_ms);
            return false;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    vTaskDelay(pdMS_TO_TICKS(30));
    return true;
}

static bool button_reached_cancel_hold(void)
{
    int64_t start_ms = esp_timer_get_time() / 1000;
    while (voice_button_any_pressed()) {
        if ((esp_timer_get_time() / 1000) - start_ms >= CANCEL_HOLD_MS) {
            return true;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    return false;
}

static void button_wait_release(void)
{
    (void)voice_button_wait_release_with_timeout(1500);
}

static void audio_apply_output_volume(int volume)
{
    if (volume < 0) volume = 0;
    if (volume > 100) volume = 100;
    s_output_volume = volume;
    es8311_set_volume(s_output_volume);

    char text[32];
    snprintf(text, sizeof(text), "%s %d%%", tr_text(&T_VOLUME_LABEL), s_output_volume);
    ui_set_dialogue(text, 12);
}

static void start_voice_session_from_key(bool *ww_listening)
{
    ESP_LOGI(TAG, "KEY → start voice, ws=%d ready=%d",
             ws_client_is_connected(), ws_client_is_ready());
    if (!ws_client_is_ready()) {
        char diag[96];
        snprintf(
            diag,
            sizeof(diag),
            tr_text(&T_VOICE_NOT_READY),
            ws_client_is_connected() ? 1 : 0,
            ws_client_is_ready() ? 1 : 0
        );
        ESP_LOGW(TAG, "%s", diag);
        ui_set_dialogue(wifi_manager_is_connected() ? diag : tr_text(&T_VOICE_OFFLINE), 30);
        return;
    }

    if (ww_listening && *ww_listening) {
        wake_word_stop();
        *ww_listening = false;
    }
    s_ignore_listening_release = false;
    /* A fresh user gesture arms continuation; the server still must opt in
     * per reply before the next turn becomes hands-free. */
    continuous_session_begin();
    fsm_handle_event(AURA_EVT_WAKE_BUTTON);
}

static void apply_diagonal_blinds(uint8_t *gb, int tick)
{
    int phase = tick <= 6 ? tick : (13 - tick);
    if (phase < 0) phase = 0;
    if (phase > 6) phase = 6;

    const int period = 28;
    const int max_width = period + 18;
    int width = 3 + (phase * max_width) / 6;
    int sweep = tick * 17;

    for (int y = 0; y < RLCD_HEIGHT; y++) {
        uint8_t *row = gb + y * RLCD_WIDTH;
        for (int x = 0; x < RLCD_WIDTH; x++) {
            int diag = (x + y * 2 + sweep) % period;
            if (diag < width) {
                row[x] = 0x00;
            } else if (phase >= 4 && diag == width) {
                row[x] = 0x80;
            }
        }
    }

    if (tick >= 4 && tick <= 9) {
        font_draw_utf8(gb, RLCD_WIDTH, 160, 138, tr_text(&T_CHANGING), 0xFF);
    }
}

bool aura_companion_state_cache_load(void)
{
    nvs_handle_t nvs = 0;
    uint8_t valid = 0;
    int32_t mood = 0;
    int32_t energy = 0;
    int32_t satiety = 80;
    int32_t affinity = 0;
    int32_t affinity_level = 0;
    int32_t beans = 0;
    int32_t current_outfit = 0;
    bool outfit_migrated = false;

    if (nvs_open("companion", NVS_READONLY, &nvs) != ESP_OK) {
        return false;
    }

    esp_err_t err = nvs_get_u8(nvs, "valid", &valid);
    if (err != ESP_OK || valid != 1) {
        nvs_close(nvs);
        return false;
    }

    if (nvs_get_i32(nvs, "mood", &mood) != ESP_OK ||
        nvs_get_i32(nvs, "energy", &energy) != ESP_OK ||
        nvs_get_i32(nvs, "affinity", &affinity) != ESP_OK ||
        nvs_get_i32(nvs, "aff_lv", &affinity_level) != ESP_OK ||
        (nvs_get_i32(nvs, "beans", &beans) != ESP_OK &&
         nvs_get_i32(nvs, "coins", &beans) != ESP_OK)) {
        nvs_close(nvs);
        return false;
    }
    if (nvs_get_i32(nvs, "satiety", &satiety) != ESP_OK) {
        satiety = 80;
    }
    int32_t unlocked_saved = BASIC_OUTFIT_UNLOCK_MASK;
    nvs_get_i32(nvs, "outfits", &unlocked_saved);
    nvs_get_i32(nvs, "outfit", &current_outfit);
    /* v2 服装体系：索引语义整体变更，旧解锁位/穿着索引直接重置
     * （豆/好感度等保留），首存时写入 outfit_v2 标记。 */
    uint8_t outfit_v2 = 0;
    if (nvs_get_u8(nvs, "outfit_v2", &outfit_v2) != ESP_OK || outfit_v2 != 1) {
        ESP_LOGI(TAG, "Outfit v2 migration: resetting unlock mask (was 0x%lx) and outfit (was %d)",
                 (unsigned long)(uint32_t)unlocked_saved, (int)current_outfit);
        unlocked_saved = BASIC_OUTFIT_UNLOCK_MASK;
        current_outfit = 0;
        outfit_migrated = true;
    }
    int32_t pin_md = -1;
    nvs_get_i32(nvs, "outfit_pin", &pin_md);
    s_outfit_pin_md = (int)pin_md;

    nvs_close(nvs);
    g_state.mood = clamp_companion_stat((int)mood);
    g_state.energy = clamp_companion_stat((int)energy);
    g_state.satiety = clamp_companion_stat((int)satiety);
    g_state.affinity = (int)affinity;
    g_state.affinity_level = clamp_affinity_level((int)affinity_level);
    g_state.coins = clamp_companion_beans((int)beans);
    g_state.outfit_unlocked = (uint32_t)unlocked_saved;
    g_state.outfit_unlocked |= BASIC_OUTFIT_UNLOCK_MASK;
    if (current_outfit < 0 || current_outfit >= renderer_get_outfit_count() ||
        ((g_state.outfit_unlocked >> current_outfit) & 1u) == 0) {
        current_outfit = 0;
        outfit_migrated = true;
    }
    g_state.current_outfit = current_outfit;
    g_state.companion_state_ready = true;
    g_state.quota_ready = false;
    g_state.quota_provider[0] = '\0';
    g_state.quota_headline[0] = '\0';
    g_state.quota_percent = 0;
    g_state.quota_text[0] = '\0';
    g_state.quota_primary_label[0] = '\0';
    g_state.quota_primary_text[0] = '\0';
    g_state.quota_primary_percent = 0;
    g_state.quota_secondary_label[0] = '\0';
    g_state.quota_secondary_text[0] = '\0';
    g_state.quota_secondary_percent = 0;
    ESP_LOGI(TAG, "Loaded companion cache: mood=%d energy=%d satiety=%d affinity=%d lv=%d beans=%d outfit=%d outfits=0x%lx",
             g_state.mood, g_state.energy, g_state.satiety, g_state.affinity,
             g_state.affinity_level, g_state.coins, g_state.current_outfit,
             (unsigned long)g_state.outfit_unlocked);
    if (outfit_migrated) {
        aura_companion_state_cache_save();
    }
    return true;
}

void aura_companion_state_cache_save(void)
{
    if (!g_state.companion_state_ready) {
        return;
    }

    nvs_handle_t nvs = 0;
    if (nvs_open("companion", NVS_READWRITE, &nvs) != ESP_OK) {
        ESP_LOGW(TAG, "Failed opening companion cache namespace");
        return;
    }

    esp_err_t err = ESP_OK;
    g_state.mood = clamp_companion_stat(g_state.mood);
    g_state.energy = clamp_companion_stat(g_state.energy);
    g_state.satiety = clamp_companion_stat(g_state.satiety);
    g_state.affinity_level = clamp_affinity_level(g_state.affinity_level);
    g_state.coins = clamp_companion_beans(g_state.coins);

    err |= nvs_set_i32(nvs, "mood", g_state.mood);
    err |= nvs_set_i32(nvs, "energy", g_state.energy);
    err |= nvs_set_i32(nvs, "satiety", g_state.satiety);
    err |= nvs_set_i32(nvs, "affinity", g_state.affinity);
    err |= nvs_set_i32(nvs, "aff_lv", g_state.affinity_level);
    err |= nvs_set_i32(nvs, "beans", g_state.coins);
    err |= nvs_set_i32(nvs, "coins", g_state.coins);
    err |= nvs_set_i32(nvs, "outfit", g_state.current_outfit);
    err |= nvs_set_i32(nvs, "outfits", (int32_t)g_state.outfit_unlocked);
    err |= nvs_set_i32(nvs, "outfit_pin", (int32_t)s_outfit_pin_md);
    err |= nvs_set_u8(nvs, "outfit_v2", 1);
    err |= nvs_set_u8(nvs, "valid", 1);
    if (err == ESP_OK) {
        err = nvs_commit(nvs);
    }
    nvs_close(nvs);

    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Failed saving companion cache: 0x%x", err);
    }
}

/* ── I2C 总线初始化 ─────────────────────────────────── */
static esp_err_t i2c_bus_init(void)
{
    i2c_config_t conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = I2C_SDA_PIN,
        .scl_io_num = I2C_SCL_PIN,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = I2C_FREQ,
    };
    esp_err_t ret = i2c_param_config(I2C_PORT, &conf);
    if (ret != ESP_OK) return ret;
    return i2c_driver_install(I2C_PORT, conf.mode, 0, 0, 0);
}

/* ── SPEAKING 超时回调 ─────────────────────────────── */
static void speak_timeout_cb(void *arg)
{
    ESP_LOGI(TAG, "SPEAKING timeout → IDLE");
    fsm_handle_event(AURA_EVT_TTS_DONE);
}

/* ── FSM 转换回调 ──────────────────────────────────── */
static void on_fsm_transition(aura_fsm_state_t old_state,
                               aura_fsm_state_t new_state)
{
    ESP_LOGI(TAG, "FSM: %s → %s",
             fsm_state_name(old_state), fsm_state_name(new_state));

    switch (new_state) {
    case AURA_STATE_LISTENING:
        s_menu_open = false;
        music_player_pause_for_interaction();
        /* 停止 SPEAKING 定时器 (打断场景) */
        if (s_speak_timer)
            esp_timer_stop(s_speak_timer);

        aura_ui_enter_listening(2 + (esp_random() % 2), 8);  /* 倾听姿势随机 2/3 */
        aura_ui_set_continuous_dialogue(false);
        ESP_LOGI(TAG, "LISTENING UI armed (old=%s)", fsm_state_name(old_state));
        break;

    case AURA_STATE_PROCESSING:
        aura_ui_set_continuous_dialogue(false);
        g_state.ui_mode = AURA_UI_PROCESSING;
        g_state.ui_anim_tick = 0;
        g_state.current_pose = 4;  /* thinking */
        g_state.mic_level = 0;
        ui_set_agent_panel(true, 10, "AGENT", tr_text(&T_AGENT_SENDING));
        ui_set_dialogue("", 0);
        aura_ui_mark_dirty();
        break;

    case AURA_STATE_SPEAKING:
        /* 仅保留兜底超时，真正结束由 TTS 播放完成驱动 */
        if (s_speak_timer)
            esp_timer_start_once(s_speak_timer, SPEAKING_SAFETY_TIMEOUT_MS * 1000);
        g_state.ui_mode = AURA_UI_SPEAKING;
        g_state.ui_anim_tick = 0;
        g_state.mic_level = 0;
        g_state.current_pose = 5 + (esp_random() % 2);  /* 说话姿势随机 5/6 */
        aura_ui_set_continuous_dialogue(false);
        ui_set_agent_panel(true, g_state.agent_progress, "AGENT", tr_text(&T_AGENT_REPLYING));
        aura_ui_mark_dirty();
        break;

    case AURA_STATE_IDLE:
        /* 停止定时器 (中止场景) */
        if (s_speak_timer)
            esp_timer_stop(s_speak_timer);
        s_ignore_listening_release = false;
        /* 从播报态回到 IDLE 时，给唤醒词一个短冷却，避免扬声器串音自触发 */
        if (old_state == AURA_STATE_SPEAKING) {
            s_wake_resume_after_ms = esp_timer_get_time() / 1000 + 1500;
        }
        g_state.ui_mode = AURA_UI_IDLE;
        g_state.ui_anim_tick = 0;
        g_state.mic_level = 0;
        g_state.current_pose = esp_random() % 2;  /* 待机姿势随机 0/1 */
        aura_ui_set_continuous_dialogue(false);
        ui_set_agent_panel(false, 0, "", "");
        music_player_resume_after_interaction();
        aura_ui_mark_dirty();
        break;
    }
}

/* ── 显示任务 ──────────────────────────────────────── */
static void display_task(void *arg)
{
    ESP_LOGI(TAG, "Display task started");

    // 初始化 RLCD
    rlcd_init();
    xEventGroupSetBits(g_event_group, EVT_DISPLAY_READY);

    // 初始化渲染器
    renderer_init();

    // 初始化 Minigame 模块
    mg_render_init();
    mg_init();

    // ── 开机画面 ──
    rlcd_clear(0xFF);
    vTaskDelay(pdMS_TO_TICKS(300));

    aura_ui_mark_dirty();
    aura_state_t snapshot;
    if (aura_ui_copy_and_clear_dirty(&snapshot)) {
        renderer_draw(&snapshot);
        rlcd_flush(renderer_get_framebuffer());
    }

    ESP_LOGI(TAG, "Boot screen rendered, playing startup SFX");
    sfx_play(SFX_STARTUP);

    while (1) {
        bool hold_dialogue = audio_is_playing() || ws_client_is_tts_active();
        bool force_listening_ui = (fsm_get_state() == AURA_STATE_LISTENING);

        if (s_language_select_open) {
            aura_ui_display_tick(false, DIALOGUE_PAGE_TICKS, &snapshot);
            const language_option_t *option = language_selected_option();
            if (option) {
                renderer_draw_with_outfit_override(&snapshot,
                                                   option->outfit_idx,
                                                   option->outfit_path);
            } else {
                renderer_draw(&snapshot);
            }
            language_select_draw(renderer_get_graybuf(), (int)s_language_sel);
            renderer_apply_threshold();
            rlcd_flush(renderer_get_framebuffer());
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        if (force_listening_ui) {
            s_menu_open = false;
            s_volume_menu_open = false;
            s_wifi_menu_open = false;
            /*
             * 续听由 audio/ws 任务触发，录音由 input 任务消费。这里直接收敛
             * 真实 UI 状态，而不是只改临时 snapshot；否则旧字幕可能在下一帧
             * 又被 g_state 带回来，导致"实际在录音但胶囊不显示"。
             */
            aura_ui_ensure_listening(7, 6);
            if (aura_ui_display_tick(hold_dialogue, DIALOGUE_PAGE_TICKS, &snapshot)) {
                renderer_draw(&snapshot);
                rlcd_flush(renderer_get_framebuffer());
            }
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        if (mg_is_active()) {
            /* Minigame owns the entire screen: render, threshold, flush */
            mg_render_frame();
        } else if (s_curtain_tick > 0) {
            /* ── 换衣：斜向百叶窗擦除，适合 1-bit RLCD 的高速几何转场 ── */
            aura_ui_display_tick(false, DIALOGUE_PAGE_TICKS, &snapshot);
            renderer_draw_scene(&snapshot);
            uint8_t *gb = renderer_get_graybuf();
            int tick = s_curtain_tick;
            apply_diagonal_blinds(gb, tick);
            renderer_apply_threshold();
            rlcd_flush(renderer_get_framebuffer());
            s_curtain_tick++;
            if (s_curtain_tick == 7 && s_curtain_target >= 0) {
                /* 换装：遮罩最密时切换 */
                snapshot.current_outfit = s_curtain_target;
                g_state.current_outfit  = s_curtain_target;
            }
            if (s_curtain_tick > 12) {
                s_curtain_tick   = 0;
                s_curtain_target = -1;
                /*
                 * Do not leave the last blinds frame on-screen waiting for
                 * the next dirty UI update. The outfit transition owns the
                 * display, so finish by immediately flushing one clean scene.
                 */
                aura_ui_mark_dirty();
                aura_ui_display_tick(false, DIALOGUE_PAGE_TICKS, &snapshot);
                renderer_draw_scene(&snapshot);
                renderer_apply_threshold();
                rlcd_flush(renderer_get_framebuffer());
            }
            vTaskDelay(pdMS_TO_TICKS(24));
            continue;
        } else if (s_dessert_open) {
            aura_ui_display_tick(false, DIALOGUE_PAGE_TICKS, &snapshot);
            int lang = tr_lang_index();
            const dessert_item_t *dessert = &s_dessert_catalog[s_dessert_sel];
            mg_draw_dessert_panel(renderer_get_graybuf(), s_dessert_sel,
                                  DESSERT_ITEM_COUNT, snapshot.coins,
                                  dessert->name[lang], dessert->price,
                                  dessert->effect[lang],
                                  tr_text(&T_DESSERT_TITLE),
                                  tr_text(&T_SHOP_CURRENCY),
                                  tr_text(&T_DESSERT_PRICE),
                                  tr_text(&T_DESSERT_EFFECT),
                                  tr_text(&T_DESSERT_BALANCE),
                                  tr_text(&T_DESSERT_BUY),
                                  tr_text(&T_NOT_ENOUGH),
                                  tr_text(&T_DESSERT_PENDING),
                                  ws_client_dessert_purchase_pending(),
                                  tr_text(&T_SHOP_FOOTER));
            renderer_apply_threshold();
            rlcd_flush(renderer_get_framebuffer());
        } else if (s_shop_open) {
            /* ── 服装店：全屏双栏，左侧人物右侧卡片 ── */
            aura_ui_display_tick(false, DIALOGUE_PAGE_TICKS, &snapshot);
            int shop_outfit = s_shop_catalog[s_shop_sel].outfit_idx;
            bool owned = (g_state.outfit_unlocked >> shop_outfit) & 1;
            int lang = tr_lang_index();
            /* mg_draw_shop_panel 会先清白屏再画卡片；人物之后叠上去 */
            mg_draw_shop_panel(renderer_get_graybuf(), s_shop_sel,
                               snapshot.coins, owned,
                               s_shop_catalog[s_shop_sel].name[lang],
                               s_shop_catalog[s_shop_sel].price,
                               s_shop_catalog[s_shop_sel].tag[lang],
                               tr_text(&T_SHOP_TITLE),
                               tr_text(&T_SHOP_CURRENCY),
                               tr_text(&T_SHOP_OWNED),
                               tr_text(&T_SHOP_NEW),
                               tr_text(&T_SHOP_PRICE),
                               tr_text(&T_SHOP_STYLE),
                               tr_text(&T_SHOP_STATUS),
                               tr_text(&T_SHOP_OWNED),
                               tr_text(&T_SHOP_LOCKED),
                               tr_text(&T_SHOP_BALANCE),
                               tr_text(&T_SHOP_AFTER),
                               tr_text(&T_SHOP_SHORT),
                               tr_text(&T_SHOP_NOT_ENOUGH),
                               tr_text(&T_SHOP_WEAR),
                               tr_text(&T_SHOP_BUY),
                               tr_text(&T_SHOP_FOOTER));
            /* 商店用原始比例裁切预览：显示更大的 2/3 人物，避免缩小后线条碎裂。 */
            renderer_draw_character_preview_crop(shop_outfit, 12, 54, 180, 216, 100, 8,
                                                 preview_pose_for(shop_outfit));
            renderer_apply_threshold();
            rlcd_flush(renderer_get_framebuffer());
        } else if (s_wardrobe_open) {
            /* ── 衣柜：全屏双栏，左侧人物右侧列表 ── */
            aura_ui_display_tick(false, DIALOGUE_PAGE_TICKS, &snapshot);
            s_wardrobe_sel = normalize_unlocked_outfit(s_wardrobe_sel);
            mg_draw_wardrobe_panel(renderer_get_graybuf(), s_wardrobe_sel,
                                   unlocked_outfit_page(s_wardrobe_sel),
                                   unlocked_outfit_count(),
                                   renderer_get_outfit_name_localized(s_wardrobe_sel, tr_lang_index()),
                                   tr_text(&T_WARDROBE_TITLE),
                                   tr_text(&T_SHOP_OWNED),
                                   tr_text(&T_WARDROBE_ID),
                                   tr_text(&T_SHOP_STATUS),
                                   tr_text(&T_WARDROBE_WEARABLE),
                                   tr_text(&T_WARDROBE_SOURCE),
                                   tr_text(&T_WARDROBE_HOME),
                                   tr_text(&T_SHOP_WEAR),
                                   tr_text(&T_WARDROBE_FOOTER));
            renderer_draw_character_preview_crop(s_wardrobe_sel, 12, 54, 180, 216, 100, 8,
                                                 preview_pose_for(s_wardrobe_sel));
            renderer_apply_threshold();
            rlcd_flush(renderer_get_framebuffer());
        } else if (s_menu_open) {
            /* Normal UI with menu popup overlay */
            aura_ui_display_tick(hold_dialogue, DIALOGUE_PAGE_TICKS, &snapshot);
            renderer_draw_scene(&snapshot);
            const char *header = NULL;
            const char *menu_opts[MAIN_MENU_COUNT] = {0};
            int menu_count = 0;
            int draw_sel = (int)s_menu_sel;
            if (s_volume_menu_open) {
                get_volume_menu_labels(&header, menu_opts, &menu_count);
                draw_sel = (int)s_volume_menu_sel;
            } else if (s_wifi_menu_open) {
                get_wifi_menu_labels(&header, menu_opts, &menu_count);
                draw_sel = (int)s_wifi_menu_sel;
            } else {
                get_menu_labels(&header, menu_opts, &menu_count);
            }
            mg_draw_main_menu(renderer_get_graybuf(), draw_sel,
                              s_volume_menu_open || s_wifi_menu_open,
                              header, menu_opts, menu_count);
            renderer_apply_threshold();
            rlcd_flush(renderer_get_framebuffer());
        } else if (aura_ui_display_tick(hold_dialogue, DIALOGUE_PAGE_TICKS, &snapshot)) {
            renderer_draw(&snapshot);
            rlcd_flush(renderer_get_framebuffer());
        }

        vTaskDelay(pdMS_TO_TICKS(100));  // ~10fps 足够
    }
}

/* ── 音频任务 ──────────────────────────────────────── */
static void audio_task(void *arg)
{
    ESP_LOGI(TAG, "Audio task started");
    audio_pipeline_init();
    es8311_set_volume(s_output_volume);
    sfx_init();  // 从 SD 卡加载音效
    music_player_init();
    s_audio_ready = true;

    while (1) {
        // 音频管线循环：检查是否有待播放的 TTS 数据
        audio_pipeline_loop();
        ws_client_on_audio_loop();
        music_player_loop();
        if (audio_is_playing()) {
            vTaskDelay(pdMS_TO_TICKS(1));
        } else {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
    }
}

/* ── 网络任务 ──────────────────────────────────────── */
#define WS_REDISCOVER_AFTER_MS 30000  /* 连不上网关多久后重新 mDNS 发现 */

static void network_task(void *arg)
{
    ESP_LOGI(TAG, "Network task started");
    bool wdt_enabled = task_wdt_add_current("network");
    bool ws_ready = false;
    char ws_uri[AURA_WS_URI_MAX_LEN] = WS_URI_DEFAULT;
    char ws_last_saved_uri[AURA_WS_URI_MAX_LEN] = {0};
    int64_t ws_not_ready_since_ms = 0;
    int64_t ota_health_ready_since_ms = 0;
    bool ota_health_confirmed = false;
    bool ota_resources_after_confirm = ota_update_running_app_pending_verify();

    while (s_language_select_open) {
        task_wdt_reset_if(wdt_enabled);
        vTaskDelay(pdMS_TO_TICKS(100));
    }

    ESP_ERROR_CHECK(wifi_manager_init());
    s_wifi_manager_ready = true;
    esp_err_t wifi_ret = wifi_manager_connect();
    if (wifi_ret != ESP_OK) {
        ESP_LOGW(TAG, "Wi-Fi startup skipped/failed: 0x%x", wifi_ret);
        ui_set_dialogue(tr_text(&T_WIFI_OFFLINE), 40);
    }

    while (1) {
        task_wdt_reset_if(wdt_enabled);

        if (wifi_manager_is_provisioning()) {
            vTaskDelay(pdMS_TO_TICKS(200));
            continue;
        }

        if (!wifi_manager_is_connected()) {
            if (!ota_health_confirmed) ota_health_ready_since_ms = 0;
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }

        if (s_ota_manual_requested) {
            s_ota_manual_requested = false;
            bool restore_wdt = wdt_enabled;
            if (restore_wdt) {
                esp_task_wdt_delete(NULL);
                wdt_enabled = false;
            }
            esp_err_t ota_err = ota_update_run_blocking(ota_status_changed, NULL, false);
            if (restore_wdt) wdt_enabled = task_wdt_add_current("network");
            if (ota_err != ESP_OK) {
                ESP_LOGE(TAG, "Manual OTA failed on network task: %s",
                         esp_err_to_name(ota_err));
            }
            continue;
        }

        EventBits_t ready_bits = xEventGroupGetBits(g_event_group);
        if (!ota_health_confirmed && s_audio_ready && (ready_bits & EVT_DISPLAY_READY) != 0) {
            int64_t now_ms = esp_timer_get_time() / 1000;
            if (ota_health_ready_since_ms == 0) {
                ota_health_ready_since_ms = now_ms;
            } else if (now_ms - ota_health_ready_since_ms >= 10000) {
                esp_err_t confirm_err = ota_update_confirm_running_app();
                if (confirm_err != ESP_OK) {
                    ESP_LOGE(TAG, "OTA boot confirmation failed: %s", esp_err_to_name(confirm_err));
                    ota_health_ready_since_ms = now_ms;
                } else {
                    ota_health_confirmed = true;
                    if (ota_resources_after_confirm) {
                        ota_resources_after_confirm = false;
                        ESP_LOGI(TAG, "Starting post-upgrade resource continuation");
                        ws_client_prepare_for_ota();
                        vTaskDelay(pdMS_TO_TICKS(1000));
                        bool restore_wdt = wdt_enabled;
                        if (restore_wdt) {
                            esp_task_wdt_delete(NULL);
                            wdt_enabled = false;
                        }
                        esp_err_t resource_err = ota_update_run_blocking(
                            ota_status_changed, NULL, true);
                        if (restore_wdt) wdt_enabled = task_wdt_add_current("network");
                        if (resource_err != ESP_OK) {
                            ESP_LOGE(TAG, "Could not start resource continuation: %s",
                                     esp_err_to_name(resource_err));
                            ws_client_resume_after_ota();
                        }
                    }
                }
            }
        }

        if (ota_update_is_running()) {
            vTaskDelay(pdMS_TO_TICKS(200));
            continue;
        }

        if (!ws_ready) {
            ESP_LOGI(TAG, "WiFi connected, starting WebSocket");
            if (net_discovery_resolve_ws_uri(ws_uri, sizeof(ws_uri)) != ESP_OK) {
                strncpy(ws_uri, WS_URI_DEFAULT, sizeof(ws_uri) - 1);
                ws_uri[sizeof(ws_uri) - 1] = '\0';
            }
            ESP_LOGI(TAG, "Using Aura Lily WebSocket URI: %s", ws_uri);
            if (ws_client_init(ws_uri) != ESP_OK) {
                ESP_LOGE(TAG, "WebSocket init failed");
                sfx_play(SFX_ERROR);
                vTaskDelay(pdMS_TO_TICKS(2000));
                continue;
            }
            ws_ready = true;
            if (ws_client_connect() != ESP_OK) {
                ESP_LOGE(TAG, "WebSocket connect failed");
                sfx_play(SFX_ERROR);
            }
        }

        ws_client_loop();

        /* 连接成功后缓存“最后可用地址”；长时间连不上则重新 mDNS 发现（换网络场景）。 */
        if (ws_client_is_ready()) {
            ws_not_ready_since_ms = 0;
            if (strcmp(ws_last_saved_uri, ws_uri) != 0) {
                if (net_discovery_save_last_good(ws_uri) == ESP_OK) {
                    strncpy(ws_last_saved_uri, ws_uri, sizeof(ws_last_saved_uri) - 1);
                    ws_last_saved_uri[sizeof(ws_last_saved_uri) - 1] = '\0';
                }
            }
        } else {
            int64_t now_ms = esp_timer_get_time() / 1000;
            if (ws_not_ready_since_ms == 0) {
                ws_not_ready_since_ms = now_ms;
            } else if (now_ms - ws_not_ready_since_ms > WS_REDISCOVER_AFTER_MS) {
                /* 跑完整解析链（mDNS→配网地址→缓存→默认），而不是只查 mDNS：
                 * 换网络后卡在陈旧缓存 IP 时，才能落回配网页填的地址。 */
                char fresh_uri[AURA_WS_URI_MAX_LEN] = {0};
                if (net_discovery_resolve_ws_uri(fresh_uri, sizeof(fresh_uri)) == ESP_OK &&
                    fresh_uri[0] != '\0' && strcmp(fresh_uri, ws_uri) != 0) {
                    ESP_LOGW(TAG, "Rediscovered gateway URI: %s", fresh_uri);
                    if (ws_client_apply_uri(fresh_uri) == ESP_OK) {
                        strncpy(ws_uri, fresh_uri, sizeof(ws_uri) - 1);
                        ws_uri[sizeof(ws_uri) - 1] = '\0';
                    }
                }
                ws_not_ready_since_ms = now_ms;  /* 无论是否切换都重新计时 */
            }
        }

        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

/* ── 传感器任务 ────────────────────────────────────── */
static void sensor_task(void *arg)
{
    ESP_LOGI(TAG, "Sensor task started");

    // 初始化传感器
    shtc3_init();
    pcf85063_init();
    battery_init();

    int env_poll_ticks = 0;
    int battery_poll_ticks = 5;

    while (1) {
        /* Wi-Fi 图标使用真实连接状态和 RSSI 映射，不再显示假信号 */
        {
            int wifi_bars = 0;
            if (wifi_manager_is_connected() && !wifi_manager_is_provisioning()) {
                int rssi = wifi_manager_get_rssi();
                if (rssi >= -55) {
                    wifi_bars = 4;
                } else if (rssi >= -67) {
                    wifi_bars = 3;
                } else if (rssi >= -75) {
                    wifi_bars = 2;
                } else if (rssi >= -85) {
                    wifi_bars = 1;
                }
            }
            if (g_state.wifi_strength != wifi_bars) {
                g_state.wifi_strength = wifi_bars;
                aura_ui_mark_dirty();
            }
        }

        /* RLCD battery voltage is sampled slowly and filtered in battery.c. */
        if (battery_poll_ticks <= 0) {
            int percent = 0;
            bool valid = false;
            battery_read(&percent, &valid);
            if (g_state.battery_valid != valid ||
                (valid && g_state.battery_percent != percent)) {
                g_state.battery_valid = valid;
                g_state.battery_percent = percent;
                aura_ui_mark_dirty();
            }
            battery_poll_ticks = 30;
        }

        /* 温湿度每 30 秒采一次，时间每秒刷新 */
        if (env_poll_ticks <= 0) {
            float temp = 0, hum = 0;
            if (shtc3_read(&temp, &hum) == ESP_OK) {
                if (!g_state.ws_connected) {
                    int old_temp = (int)lroundf(g_state.temperature);
                    int new_temp = (int)lroundf(temp);
                    if (old_temp != new_temp || fabsf(g_state.humidity - hum) >= 0.5f) {
                        g_state.temperature = temp;
                        g_state.humidity = hum;
                        aura_ui_mark_dirty();
                    } else {
                        g_state.temperature = temp;
                        g_state.humidity = hum;
                    }
                } else {
                    if (fabsf(g_state.humidity - hum) >= 0.5f) {
                        g_state.humidity = hum;
                        aura_ui_mark_dirty();
                    } else {
                        g_state.humidity = hum;
                    }
                }
            }
            env_poll_ticks = 30;
        }

        // 读取时间：优先使用 SNTP 校正后的系统时间，否则 fallback 到 RTC
        {
            time_t now = time(NULL);
            struct tm tinfo;
            localtime_r(&now, &tinfo);
            if (tinfo.tm_year > (2024 - 1900)) {
                /* SNTP 已同步 — 使用系统时间 (UTC+8) */
                if (g_state.hour != tinfo.tm_hour ||
                    g_state.minute != tinfo.tm_min ||
                    g_state.month != (tinfo.tm_mon + 1) ||
                    g_state.day != tinfo.tm_mday) {
                    g_state.hour   = tinfo.tm_hour;
                    g_state.minute = tinfo.tm_min;
                    g_state.month  = tinfo.tm_mon + 1;
                    g_state.day    = tinfo.tm_mday;
                    aura_ui_mark_dirty();
                }

                /* 每小时把系统时间写回 RTC 一次 */
                static int last_sync_hour = -1;
                if (tinfo.tm_hour != last_sync_hour) {
                    last_sync_hour = tinfo.tm_hour;
                    pcf85063_time_t wt = {
                        .year = tinfo.tm_year + 1900,
                        .month = tinfo.tm_mon + 1,
                        .day = tinfo.tm_mday,
                        .hour = tinfo.tm_hour,
                        .minute = tinfo.tm_min,
                        .second = tinfo.tm_sec,
                    };
                    pcf85063_set_time(&wt);
                }
            } else {
                /* SNTP 未同步 — fallback RTC */
                pcf85063_time_t rtc_time;
                if (pcf85063_get_time(&rtc_time) == ESP_OK) {
                    if (g_state.hour != rtc_time.hour ||
                        g_state.minute != rtc_time.minute ||
                        g_state.month != rtc_time.month ||
                        g_state.day != rtc_time.day) {
                        g_state.hour = rtc_time.hour;
                        g_state.minute = rtc_time.minute;
                        g_state.month = rtc_time.month;
                        g_state.day = rtc_time.day;
                        aura_ui_mark_dirty();
                    }
                }
            }
        }

        env_poll_ticks--;
        battery_poll_ticks--;
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

/* ── 输入任务 ─ 语音交互管线核心 ──────────────────── */
/*
 * 交互流程:
 *   1. IDLE: 长按 KEY → LISTENING
 *   2. LISTENING:
 *      - 发送 {"type":"start"} 到后端
 *      - 启动 I2S 录音
 *      - 循环读取 I2S mic → 转换 32bit→16bit → WS 发送二进制帧
 *      - 短按 KEY (释放) → 停止录音 → 发送 {"type":"stop"} → PROCESSING
 *   3. PROCESSING: 等待后端回复 (ws_client 回调处理)
 *   4. SPEAKING: 显示回复文本，5 秒后自动 → IDLE
 *
 * I2S 硬件输出格式: 16-bit TDM 4-slot (driver auto-truncates ES7210 24-bit → 16-bit)
 * 后端需要:           16-bit mono  (int16 @ 16kHz)
 * 转换: 提取目标 SLOT + 软件增益
 */

/* I2S 录音 chunk: TDM 4 slot × 16-bit (driver auto-truncates ES7210 24-bit → 16-bit)
 * RX = TDM 16-bit 4-slot: 每帧 = 4 × int16_t = 8 bytes
 * 想要 960 个 MIC 样本 → 960 × 8 = 7680 bytes */
#define MIC_SAMPLES_PER_CHUNK   960
/* RX is TDM 16-bit 4-slot → each frame = 4 × int16_t = 8 bytes */
#define MIC_I2S_CHUNK_BYTES     (MIC_SAMPLES_PER_CHUNK * 4 * sizeof(int16_t))
/* 原始 SLOT 提取后: 960 mono samples × 2 bytes = 1920 bytes */
#define MIC_RAW_PCM_CHUNK_BYTES (MIC_SAMPLES_PER_CHUNK * sizeof(int16_t))
/* AFE 可能因为内部分块带来输出抖动，预留更大的发送缓冲 */
#define MIC_PCM_MAX_SAMPLES     2048
#define MIC_PCM_CHUNK_BYTES     (MIC_PCM_MAX_SAMPLES * sizeof(int16_t))

#define MIC_SLOT_COUNT          4
#define MIC_CAPTURE_SLOT        0      /* 固定 SLOT0 — loopback 测试证实 SLOT0 清楚 */
#define MIC_SW_GAIN             4
#define MIC_SLOT_LOCK_CHUNKS    4
#define MIC_SLOT_LOCK_RMS_MIN   96

typedef struct {
    int rms16;
    int peak16;
    int clip_percent;
} mic_slot_diag_t;

static int16_t mic_apply_gain(int16_t raw16, int *clipped)
{
    /* 16-bit TDM mode: I2S driver already gives us proper int16_t samples.
     * Just apply software gain. */
    int32_t sample = (int32_t)raw16 * MIC_SW_GAIN;
    bool did_clip = false;

    if (sample > 32767) {
        sample = 32767;
        did_clip = true;
    } else if (sample < -32768) {
        sample = -32768;
        did_clip = true;
    }

    if (clipped && did_clip) {
        (*clipped)++;
    }

    return (int16_t)sample;
}

static void mic_analyze_slots(const int16_t *i2s_buf, size_t total_i16,
                              mic_slot_diag_t stats[MIC_SLOT_COUNT], int *best_slot)
{
    int64_t sum_sq[MIC_SLOT_COUNT] = {0};
    int peak[MIC_SLOT_COUNT] = {0};
    int clipped[MIC_SLOT_COUNT] = {0};
    int frames = 0;

    memset(stats, 0, sizeof(mic_slot_diag_t) * MIC_SLOT_COUNT);

    for (size_t i = 0; i + (MIC_SLOT_COUNT - 1) < total_i16; i += MIC_SLOT_COUNT) {
        frames++;
        for (int slot = 0; slot < MIC_SLOT_COUNT; slot++) {
            int clip = 0;
            int16_t sample = mic_apply_gain(i2s_buf[i + slot], &clip);
            int magnitude = (sample == -32768) ? 32768 : (sample < 0 ? -sample : sample);

            sum_sq[slot] += (int64_t)sample * sample;
            if (magnitude > peak[slot]) {
                peak[slot] = magnitude;
            }
            clipped[slot] += clip;
        }
    }

    int strongest_slot = 0;
    for (int slot = 0; slot < MIC_SLOT_COUNT; slot++) {
        if (frames > 0) {
            stats[slot].rms16 = (int)sqrt((double)sum_sq[slot] / frames);
            stats[slot].clip_percent = clipped[slot] * 100 / frames;
        }
        stats[slot].peak16 = peak[slot];

        if (stats[slot].rms16 > stats[strongest_slot].rms16) {
            strongest_slot = slot;
        }
    }

    if (best_slot) {
        *best_slot = strongest_slot;
    }
}

static size_t mic_extract_slot_pcm(const int16_t *i2s_buf, size_t total_i16,
                                   int slot, int16_t *pcm_buf, int *clipped_samples)
{
    size_t mono_count = 0;

    if (clipped_samples) {
        *clipped_samples = 0;
    }

    for (size_t i = slot; i < total_i16; i += MIC_SLOT_COUNT) {
        pcm_buf[mono_count++] = mic_apply_gain(i2s_buf[i], clipped_samples);
    }

    return mono_count;
}

static void input_task(void *arg)
{
    ESP_LOGI(TAG, "Input task started");
    buttons_init();
    gpio_scan_diag_init();
    bool wdt_enabled = task_wdt_add_current("input");

    /*
     * I2S 读取缓冲 (TDM 32-bit 4-slot)
     * 和转换后的 16-bit mono 缓冲
     */
    int16_t *i2s_buf     = heap_caps_malloc(MIC_I2S_CHUNK_BYTES, MALLOC_CAP_SPIRAM);
    /* mono 提取/发送缓冲只做 memcpy 级访问，放 PSRAM，内部 RAM 留给 WiFi/LWIP */
    int16_t *raw_pcm_buf = heap_caps_malloc(MIC_RAW_PCM_CHUNK_BYTES, MALLOC_CAP_SPIRAM);
    int16_t *pcm_buf     = heap_caps_malloc(MIC_PCM_CHUNK_BYTES, MALLOC_CAP_SPIRAM);

    if (!i2s_buf || !raw_pcm_buf || !pcm_buf) {
        ESP_LOGE(TAG, "Failed to allocate mic buffers");
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "Streaming raw PCM (16kHz mono) to backend");

#if MIC_LOOPBACK_TEST
    /* ══════════════════════════════════════════════════════
     * MIC LOOPBACK TEST — 按住录音，松手播放
     * 每次按键循环切换不同的 SLOT / 增益配置
     * ══════════════════════════════════════════════════════ */
    {
        /* 录音缓冲: 5 秒 @ 16kHz × 2 bytes = 160KB (PSRAM) */
        #define LB_MAX_SAMPLES  (AUDIO_SAMPLE_RATE * 5)
        int16_t *lb_buf = heap_caps_malloc(LB_MAX_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM);
        if (!lb_buf) {
            ESP_LOGE(TAG, "LOOPBACK: Failed to alloc record buffer");
            vTaskDelete(NULL);
            return;
        }

        typedef struct {
            int slot;       /* -1 = mix all slots */
            int sw_gain;
            const char *name;
        } lb_config_t;

        static const lb_config_t configs[] = {
            { 0,  1, "S0 G1"  },
            { 0,  4, "S0 G4"  },
            { 0,  8, "S0 G8"  },
            { 1,  1, "S1 G1"  },
            { 1,  4, "S1 G4"  },
            { 2,  1, "S2 G1"  },
            { 3,  1, "S3 G1"  },
            { -1, 4, "MIX G4" },
        };
        #define LB_NUM_CONFIGS (sizeof(configs) / sizeof(configs[0]))

        int cfg_idx = 0;
        snprintf(g_state.display_text, sizeof(g_state.display_text),
                 "MIC TEST: %s", configs[cfg_idx].name);
        g_state.text_char_index = 0;
        g_state.dialogue_page_tick = 0;
        g_state.dirty = true;
        ESP_LOGI(TAG, "=== MIC LOOPBACK TEST === Config: %s", configs[cfg_idx].name);

        while (1) {
            /* 等待按键按下 */
            if (gpio_get_level(BTN_KEY_PIN) != 0) {
                vTaskDelay(pdMS_TO_TICKS(20));
                continue;
            }
            /* 去抖 */
            vTaskDelay(pdMS_TO_TICKS(30));
            if (gpio_get_level(BTN_KEY_PIN) != 0) continue;

            const lb_config_t *cfg = &configs[cfg_idx];
            ESP_LOGI(TAG, "LOOPBACK REC START: %s (slot=%d gain=%d)",
                     cfg->name, cfg->slot, cfg->sw_gain);

            snprintf(g_state.display_text, sizeof(g_state.display_text),
                     "REC: %s", cfg->name);
            g_state.text_char_index = 0;
            g_state.dialogue_page_tick = 0;
            g_state.dirty = true;

            /* 启动 I2S */
            audio_record_start_stream();
            size_t lb_pos = 0;
            int chunk_cnt = 0;

            /* ── 录音循环 (按住期间) ── */
            while (gpio_get_level(BTN_KEY_PIN) == 0 && lb_pos < LB_MAX_SAMPLES) {
                size_t bytes_read = 0;
                esp_err_t ret = audio_record_read(
                    (uint8_t *)i2s_buf, MIC_I2S_CHUNK_BYTES,
                    &bytes_read, pdMS_TO_TICKS(100));
                if (ret != ESP_OK || bytes_read == 0) {
                    vTaskDelay(pdMS_TO_TICKS(5));
                    continue;
                }

                size_t total_i16 = bytes_read / sizeof(int16_t);
                size_t frames = total_i16 / MIC_SLOT_COUNT;

                /* 提取 mono samples */
                for (size_t f = 0; f < frames && lb_pos < LB_MAX_SAMPLES; f++) {
                    int32_t sample;
                    if (cfg->slot >= 0) {
                        /* 单 SLOT */
                        sample = (int32_t)i2s_buf[f * MIC_SLOT_COUNT + cfg->slot];
                    } else {
                        /* MIX: 4 SLOT 平均 */
                        int32_t sum = 0;
                        for (int s = 0; s < MIC_SLOT_COUNT; s++) {
                            sum += (int32_t)i2s_buf[f * MIC_SLOT_COUNT + s];
                        }
                        sample = sum / MIC_SLOT_COUNT;
                    }
                    /* 软件增益 */
                    sample *= cfg->sw_gain;
                    if (sample > 32767)  sample = 32767;
                    if (sample < -32768) sample = -32768;
                    lb_buf[lb_pos++] = (int16_t)sample;
                }

                chunk_cnt++;
                /* 每 10 chunks 打印 RMS */
                if (chunk_cnt % 10 == 0) {
                    int64_t sum_sq = 0;
                    size_t start = (lb_pos > 240) ? lb_pos - 240 : 0;
                    for (size_t j = start; j < lb_pos; j++) {
                        sum_sq += (int64_t)lb_buf[j] * lb_buf[j];
                    }
                    int rms = (int)sqrt((double)sum_sq / (lb_pos - start));
                    ESP_LOGI(TAG, "LOOPBACK REC #%d: %s pos=%d rms=%d",
                             chunk_cnt, cfg->name, (int)lb_pos, rms);
                }
            }

            /* 停止录音 */
            audio_record_stop_stream();

            float dur_s = (float)lb_pos / AUDIO_SAMPLE_RATE;
            ESP_LOGI(TAG, "LOOPBACK REC DONE: %s, %d samples (%.1fs)",
                     cfg->name, (int)lb_pos, dur_s);

            /* 显示播放状态 */
            snprintf(g_state.display_text, sizeof(g_state.display_text),
                     "PLAY: %s (%.1fs)", cfg->name, dur_s);
            g_state.text_char_index = 0;
            g_state.dialogue_page_tick = 0;
            g_state.dirty = true;

            /* 播放 + 同时发到后端 */
            if (lb_pos > 0) {
                size_t lb_bytes = lb_pos * sizeof(int16_t);

                /* 先通过 WS 发送完全相同的 PCM 到后端保存 */
                if (ws_client_is_connected()) {
                    ws_client_send_start();
                    /* 分块发送 (每块 1920 bytes = 960 samples) */
                    size_t sent = 0;
                    while (sent < lb_bytes) {
                        size_t chunk = lb_bytes - sent;
                        if (chunk > 1920) chunk = 1920;
                        ws_client_send_pcm((const uint8_t *)lb_buf + sent, chunk);
                        sent += chunk;
                    }
                    ws_client_send_stop();
                    ESP_LOGI(TAG, "LOOPBACK: sent %d bytes to backend via WS", (int)lb_bytes);
                } else {
                    ESP_LOGW(TAG, "LOOPBACK: WS not connected, skipping send");
                }

                /* 播放 */
                audio_play_pcm((const uint8_t *)lb_buf, lb_bytes);
                /* 等播放完 */
                while (audio_is_playing()) {
                    audio_pipeline_loop();
                    vTaskDelay(pdMS_TO_TICKS(10));
                }
            }

            ESP_LOGI(TAG, "LOOPBACK PLAY DONE: %s", cfg->name);

            /* 播放完后，同时发送到服务器跑 ASR (如果 WS 连接着) */
            if (lb_pos > 0 && ws_client_is_connected()) {
                snprintf(g_state.display_text, sizeof(g_state.display_text),
                         "ASR: %s ...", cfg->name);
                g_state.text_char_index = 0;
                g_state.dialogue_page_tick = 0;
                g_state.dirty = true;

                ws_client_send_start();
                /* 分 chunk 发送，每块 1920 bytes (960 samples) */
                size_t sent = 0;
                while (sent < lb_pos) {
                    size_t chunk = lb_pos - sent;
                    if (chunk > 960) chunk = 960;
                    ws_client_send_pcm((const uint8_t *)&lb_buf[sent],
                                       chunk * sizeof(int16_t));
                    sent += chunk;
                    vTaskDelay(pdMS_TO_TICKS(1));  /* 别撑爆 WS 缓冲 */
                }
                ws_client_send_stop();
                ESP_LOGI(TAG, "LOOPBACK → ASR sent %d samples via WS", (int)lb_pos);

                /* 等 2 秒让 ASR 处理 */
                vTaskDelay(pdMS_TO_TICKS(2000));
            }

            /* 切换到下一个配置 */
            cfg_idx = (cfg_idx + 1) % LB_NUM_CONFIGS;
            snprintf(g_state.display_text, sizeof(g_state.display_text),
                     "MIC TEST: %s", configs[cfg_idx].name);
            g_state.text_char_index = 0;
            g_state.dialogue_page_tick = 0;
            g_state.dirty = true;
            ESP_LOGI(TAG, "Next config: %s", configs[cfg_idx].name);

            /* 等松手 */
            while (gpio_get_level(BTN_KEY_PIN) == 0) {
                vTaskDelay(pdMS_TO_TICKS(20));
            }
            vTaskDelay(pdMS_TO_TICKS(200));  /* 防抖 */
        }
    }
#endif /* MIC_LOOPBACK_TEST */

    /* 初始化唤醒词检测 (MultiNet 命令词 "你好奥拉" / "你好欧拉") */
    bool ww_available = false;
    esp_err_t ww_ret = wake_word_init();
    if (ww_ret != ESP_OK) {
        ESP_LOGW(TAG, "Wake word init failed (0x%x), button-only mode", ww_ret);
    } else {
        ESP_LOGI(TAG, "Wake word '你好奥拉' / '你好欧拉' ready!");
        ww_available = true;
    }

    /* 唤醒词监听用的 I2S 缓冲 (TDM 4-slot) */
    int ww_feed_size = wake_word_get_feed_size();
    size_t ww_i2s_bytes = ww_feed_size * MIC_SLOT_COUNT * sizeof(int16_t);
    int16_t *ww_i2s_buf = NULL;
    int16_t *ww_mono_buf = NULL;
    bool ww_listening = false;  /* I2S keep-alive 是否已激活 */

    if (ww_available) {
        ww_i2s_buf = heap_caps_malloc(ww_i2s_bytes, MALLOC_CAP_SPIRAM);
        ww_mono_buf = heap_caps_malloc(ww_feed_size * sizeof(int16_t), MALLOC_CAP_INTERNAL);
        if (!ww_i2s_buf || !ww_mono_buf) {
            ESP_LOGE(TAG, "Failed to alloc wake word buffers");
            ww_available = false;
        } else {
            ESP_LOGI(TAG, "Wake word feed_size=%d, i2s_chunk=%d bytes",
                     ww_feed_size, (int)ww_i2s_bytes);
        }
    }

    /*
     * 唤醒词后的本地 VAD 参数
     * - 本地判停，不依赖 server_vad，避免"先唤醒再想一下"时被远端抢先结束
     * - 开口确认阈值高于静音阈值，避免只喊 "豆豆" 后把环境底噪当成有效指令
     */
    #define WW_VAD_SILENCE_THRESHOLD   220     /* 说话后的静音判停阈值 (已加 MIC_SW_GAIN) */
    #define WW_VAD_SPEECH_START_RMS    280     /* 初次开口确认阈值，防止底噪/尾音误确认 */
    #define WW_VAD_SPEECH_START_PEAK   1200    /* 初次开口还需要短时峰值，过滤稳定底噪 */
    #define WW_VAD_SILENCE_MS          1200    /* 说话后静音多久判定说完 (ms) */
    #define WW_VAD_NO_SPEECH_MS        2800    /* 唤醒后多久还没开口就放弃 (ms) */
    #define WW_VAD_MAX_RECORD_MS       30000   /* 唤醒词后最长录音时间 (ms) */
    #define WW_VAD_MIN_SPEECH_MS       300     /* 至少连续这么久才算真的开口 */

    while (1) {
        task_wdt_reset_if(wdt_enabled);
        aura_fsm_state_t state = fsm_get_state();
        gpio_scan_diag_poll();
        button_probe_poll();
        log_input_diag("tick", state, ww_listening);

        if (s_language_select_open) {
            button_event_t boot_evt = buttons_poll_boot();
            button_event_t key_evt = buttons_poll_key();
            if (boot_evt == BTN_EVENT_BOOT_SHORT) {
                language_select_move_next();
            }
            if (key_evt == BTN_EVENT_KEY_SHORT) {
                language_select_confirm();
            }
            g_state.mic_level = 0;
            vTaskDelay(pdMS_TO_TICKS(30));
            continue;
        }

        if (ota_update_is_running()) {
            if (ww_listening) {
                wake_word_stop();
                wake_word_clear_ring_buffer();
                ww_listening = false;
            }
            g_state.mic_level = 0;
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        if (wifi_manager_is_provisioning() && ww_listening) {
            if (ww_listening) {
                wake_word_stop();
                wake_word_clear_ring_buffer();
                ww_listening = false;
                ESP_LOGI(TAG, "Wake word listening paused during provisioning");
            }
            g_state.mic_level = 0;
        }

        /* ── IDLE: 按键 + 唤醒词 ── */
        if (state == AURA_STATE_IDLE) {
            int64_t now_ms = esp_timer_get_time() / 1000;

            /* ── Minigame: tick + full button routing ───────────────── */
            if (mg_is_active()) {
                mg_tick(now_ms);
                button_event_t boot_evt = buttons_poll_boot();
                button_event_t key_evt2  = buttons_poll_key();
                if (boot_evt == BTN_EVENT_BOOT_SHORT) mg_handle_input(MG_INPUT_LEFT);
                if (key_evt2 == BTN_EVENT_KEY_SHORT) mg_handle_input(MG_INPUT_CONFIRM);
                else if (key_evt2 == BTN_EVENT_KEY_LONG) mg_handle_input(MG_INPUT_BACK);
                vTaskDelay(pdMS_TO_TICKS(50));
                continue;
            }

            /* ── Main-menu popup ────────────────────────────────────── */
            if (s_menu_open) {
                button_event_t boot_evt = buttons_poll_boot();
                button_event_t mkey_evt = buttons_poll_key();
                if (s_volume_menu_open) {
                    if (boot_evt == BTN_EVENT_BOOT_SHORT) {
                        s_volume_menu_sel = (s_volume_menu_sel + 1) % VOLUME_MENU_COUNT;
                    }
                    if (mkey_evt == BTN_EVENT_KEY_SHORT) {
                        int sel = (int)s_volume_menu_sel;
                        if (sel == VOLUME_MENU_DOWN) {
                            audio_apply_output_volume(s_output_volume - 5);
                        } else if (sel == VOLUME_MENU_UP) {
                            audio_apply_output_volume(s_output_volume + 5);
                        } else {
                            s_volume_menu_open = false;
                            s_menu_sel = MAIN_MENU_VOLUME;
                        }
                    } else if (mkey_evt == BTN_EVENT_KEY_LONG) {
                        s_volume_menu_open = false;
                        s_menu_sel = MAIN_MENU_VOLUME;
                    }
                    vTaskDelay(pdMS_TO_TICKS(30));
                    continue;
                }

                if (s_wifi_menu_open) {
                    if (boot_evt == BTN_EVENT_BOOT_SHORT) {
                        s_wifi_menu_sel = (s_wifi_menu_sel + 1) % WIFI_MENU_COUNT;
                    }
                    if (mkey_evt == BTN_EVENT_KEY_SHORT) {
                        int sel = (int)s_wifi_menu_sel;
                        if (sel == WIFI_MENU_SLOT_1 || sel == WIFI_MENU_SLOT_2) {
                            s_menu_open = false;
                            s_wifi_menu_open = false;
                            wifi_menu_connect_saved_slot(sel == WIFI_MENU_SLOT_1 ? 0 : 1);
                        } else if (sel == WIFI_MENU_RECONNECT) {
                            s_menu_open = false;
                            s_wifi_menu_open = false;
                            wifi_menu_reconnect();
                        } else if (sel == WIFI_MENU_PROVISION) {
                            s_menu_open = false;
                            s_wifi_menu_open = false;
                            wifi_menu_start_provisioning();
                        } else {
                            s_wifi_menu_open = false;
                            s_menu_sel = MAIN_MENU_WIFI;
                        }
                    } else if (mkey_evt == BTN_EVENT_KEY_LONG) {
                        s_wifi_menu_open = false;
                        s_menu_sel = MAIN_MENU_WIFI;
                    }
                    vTaskDelay(pdMS_TO_TICKS(30));
                    continue;
                }

                if (boot_evt == BTN_EVENT_BOOT_SHORT) {
                    s_menu_sel = (s_menu_sel + 1) % MAIN_MENU_COUNT;
                }
                if (mkey_evt == BTN_EVENT_KEY_SHORT) {
                    int sel = (int)s_menu_sel;
                    if (sel == MAIN_MENU_SHOP) {
                        s_menu_open = false;
                        s_shop_open = true;
                        aura_ui_reset_idle_surface();
                        s_shop_sel  = 0;
                        s_shop_saved_outfit = g_state.current_outfit;
                    } else if (sel == MAIN_MENU_WARDROBE) {
                        s_menu_open = false;
                        s_wardrobe_open  = true;
                        aura_ui_reset_idle_surface();
                        s_wardrobe_sel   = normalize_unlocked_outfit(g_state.current_outfit);
                    } else if (sel == MAIN_MENU_DESSERT) {
                        s_menu_open = false;
                        s_dessert_open = true;
                        aura_ui_reset_idle_surface();
                        s_dessert_sel = 0;
                    } else if (sel == MAIN_MENU_VOLUME) {
                        s_volume_menu_open = true;
                        s_volume_menu_sel = 0;
                    } else if (sel == MAIN_MENU_LANGUAGE) {
                        s_menu_open = false;
                        s_language_sel = (int)s_ui_language;
                        s_language_select_open = true;
                        aura_ui_reset_idle_surface();
                        aura_ui_mark_dirty();
                    } else if (sel == MAIN_MENU_WIFI) {
                        s_wifi_menu_open = true;
                        s_wifi_menu_sel = 0;
                    } else if (sel == MAIN_MENU_UPDATE) {
                        start_ota_update_from_menu();
                    }
                } else if (mkey_evt == BTN_EVENT_KEY_LONG) {
                    s_menu_open = false;
                    s_volume_menu_open = false;
                    s_wifi_menu_open = false;
                    /* 触发重绘：IDLE 下无脏标记不会刷屏，菜单会残留在屏上 */
                    aura_ui_mark_dirty();
                    /* 等 KEY 松开再离开菜单分支：长按事件在 1s 时触发，此刻
                     * 手指还按着，下一轮循环会被当成语音键误开录音。 */
                    (void)voice_button_wait_release_with_timeout(5000);
                }
                vTaskDelay(pdMS_TO_TICKS(30));
                continue;
            }

            /* ── 甜品店界面 ─────────────────────────────────────────── */
            if (s_dessert_open) {
                button_event_t boot_evt = buttons_poll_boot();
                button_event_t key_evt  = buttons_poll_key();
                if (boot_evt == BTN_EVENT_BOOT_SHORT) {
                    s_dessert_sel = (s_dessert_sel + 1) % DESSERT_ITEM_COUNT;
                    aura_ui_mark_dirty();
                }
                if (key_evt == BTN_EVENT_KEY_SHORT) {
                    purchase_selected_dessert();
                } else if (key_evt == BTN_EVENT_KEY_LONG) {
                    s_dessert_open = false;
                    s_menu_open = true;
                    aura_ui_reset_idle_surface();
                    s_menu_sel = MAIN_MENU_DESSERT;
                    aura_ui_mark_dirty();
                }
                vTaskDelay(pdMS_TO_TICKS(50));
                continue;
            }

            /* ── 服装店界面 ─────────────────────────────────────────── */
            if (s_shop_open) {
                button_event_t boot_evt = buttons_poll_boot();
                button_event_t key_evt  = buttons_poll_key();
                if (boot_evt == BTN_EVENT_BOOT_SHORT) {
                    /* 下一件商品 */
                    s_shop_sel = (s_shop_sel + 1) % SHOP_ITEM_COUNT;
                }
                if (key_evt == BTN_EVENT_KEY_SHORT) {
                    int outfit_idx = s_shop_catalog[s_shop_sel].outfit_idx;
                    bool already_owned = (g_state.outfit_unlocked >> outfit_idx) & 1;
                    if (already_owned) {
                        /* 已拥有：直接穿上，帘幕切换 */
                        s_shop_open = false;
                        s_curtain_target = outfit_idx;
                        s_curtain_tick   = 1;
                        g_state.current_outfit = outfit_idx;
                        s_outfit_pin_md = g_state.month * 100 + g_state.day;
                        aura_companion_state_cache_save();
                    } else {
                        int price = s_shop_catalog[s_shop_sel].price;
                        if (g_state.coins >= price) {
                            g_state.coins -= price;
                            g_state.outfit_unlocked |= (1u << outfit_idx);
                            s_shop_open = false;
                            s_curtain_target = outfit_idx;
                            s_curtain_tick   = 1;
                            g_state.current_outfit = outfit_idx;
                            s_outfit_pin_md = g_state.month * 100 + g_state.day;
                            ui_set_dialogue(tr_text(&T_BUY_OK), 25);
                            aura_companion_state_cache_save();
                            aura_ui_mark_dirty();
                        } else {
                            ui_set_dialogue(tr_text(&T_NOT_ENOUGH), 20);
                        }
                    }
                } else if (key_evt == BTN_EVENT_KEY_LONG) {
                    /* 返回上一级菜单，恢复原来的服装 */
                    s_shop_open = false;
                    g_state.current_outfit = s_shop_saved_outfit;
                    s_menu_open = true;
                    aura_ui_reset_idle_surface();
                    s_volume_menu_open = false;
                    s_wifi_menu_open = false;
                    s_menu_sel = MAIN_MENU_SHOP;
                    aura_ui_mark_dirty();
                }
                vTaskDelay(pdMS_TO_TICKS(50));
                continue;
            }

            /* ── 衣柜界面 ───────────────────────────────────────────── */
            if (s_wardrobe_open) {
                button_event_t boot_evt = buttons_poll_boot();
                button_event_t key_evt  = buttons_poll_key();
                if (boot_evt == BTN_EVENT_BOOT_SHORT) {
                    s_wardrobe_sel = next_unlocked_outfit(s_wardrobe_sel);
                }
                if (key_evt == BTN_EVENT_KEY_SHORT) {
                    s_wardrobe_open = false;
                    s_curtain_target = s_wardrobe_sel;
                    s_curtain_tick   = 1;
                    g_state.current_outfit = s_wardrobe_sel;
                    s_outfit_pin_md = g_state.month * 100 + g_state.day; /* 用户指定，今天不再自动换 */
                    aura_companion_state_cache_save();
                } else if (key_evt == BTN_EVENT_KEY_LONG) {
                    s_wardrobe_open = false;
                    s_menu_open = true;
                    aura_ui_reset_idle_surface();
                    s_volume_menu_open = false;
                    s_wifi_menu_open = false;
                    s_menu_sel = MAIN_MENU_WARDROBE;
                    aura_ui_mark_dirty();
                }
                vTaskDelay(pdMS_TO_TICKS(50));
                continue;
            }

            /* ── 自动换装：无弹窗/帘幕时按时段与天气判定 ────────────── */
            if (s_curtain_tick == 0) {
                auto_outfit_tick();
            }

            button_event_t boot_evt = buttons_poll_boot();
            if (boot_evt == BTN_EVENT_BOOT_SHORT) {
                log_input_diag("boot_short_open_menu", state, ww_listening);
                s_menu_open = true;
                s_volume_menu_open = false;
                s_wifi_menu_open = false;
                s_menu_sel  = 0;
                aura_ui_reset_idle_surface();
                vTaskDelay(pdMS_TO_TICKS(30));
                continue;
            }

            if (music_player_is_active()) {
                button_event_t key_evt = buttons_poll_key();
                if (key_evt == BTN_EVENT_KEY_SHORT) {
                    if (music_player_toggle_pause() == ESP_OK) {
                        ui_set_dialogue(music_player_is_paused() ? tr_text(&T_PAUSED) : tr_text(&T_RESUMED), 12);
                    }
                    vTaskDelay(pdMS_TO_TICKS(30));
                    continue;
                }
                if (key_evt == BTN_EVENT_KEY_LONG) {
                    music_player_request_stop();
                    ui_set_dialogue(tr_text(&T_STOPPED), 12);
                    vTaskDelay(pdMS_TO_TICKS(30));
                    continue;
                }
            } else if (voice_button_any_pressed()) {
                /*
                 * 按住说话：600ms 内不给任何反馈（不进倾听、不出录音条）；
                 * 提前松开 → 播取消音；按满 600ms → 播开始音，再进入录音。
                 * 这样用户听到"滴"声才开口，体感与实际录音起点一致。
                 */
                aura_ui_reset_idle_surface();
                int64_t press_ms = esp_timer_get_time() / 1000;
                bool hold_confirmed = false;
                while (true) {
                    task_wdt_reset_if(wdt_enabled);
                    vTaskDelay(pdMS_TO_TICKS(10));
                    if (!voice_button_any_pressed()) {
                        break;
                    }
                    if (esp_timer_get_time() / 1000 - press_ms >= PTT_MIN_HOLD_MS) {
                        hold_confirmed = true;
                        break;
                    }
                }
                if (!hold_confirmed) {
                    log_input_diag("key_tap_too_short_cancel", state, ww_listening);
                    ESP_LOGI(TAG, "PTT released early (<%dms) → cancel tone", PTT_MIN_HOLD_MS);
                    sfx_play(SFX_CANCEL);
                    vTaskDelay(pdMS_TO_TICKS(30));
                    continue;
                }
                log_input_diag("key_press_start_voice", state, ww_listening);
                if (ws_client_is_ready()) {
                    esp_err_t tone_ret = sfx_play(SFX_LISTEN_START);
                    int64_t tone_deadline_ms = esp_timer_get_time() / 1000 +
                        PTT_START_TONE_TIMEOUT_MS;
                    while (tone_ret == ESP_OK && audio_is_playing() &&
                           esp_timer_get_time() / 1000 < tone_deadline_ms) {
                        task_wdt_reset_if(wdt_enabled);
                        vTaskDelay(pdMS_TO_TICKS(10));
                    }
                    if (!voice_button_any_pressed()) {
                        ESP_LOGI(TAG, "PTT released during start tone -> cancel recording");
                        sfx_play(SFX_CANCEL);
                        vTaskDelay(pdMS_TO_TICKS(30));
                        continue;
                    }
                }
                if (ws_client_is_connected()) {
                    esp_err_t button_ret = ws_client_send_button(BTN_EVENT_KEY_SHORT);
                    ESP_LOGI(TAG, "DIAG button_press send ret=0x%x ready=%d",
                             button_ret, ws_client_is_ready() ? 1 : 0);
                }
                start_voice_session_from_key(&ww_listening);
                vTaskDelay(pdMS_TO_TICKS(30));
                continue;
            } else {
                button_event_t key_evt = buttons_poll_key();
                if (key_evt == BTN_EVENT_KEY_SHORT) {
                    /* 两次轮询之间就松开的极短按：视为误触，播取消音，不进入录音 */
                    log_input_diag("key_tap_too_short_cancel", state, ww_listening);
                    sfx_play(SFX_CANCEL);
                    vTaskDelay(pdMS_TO_TICKS(30));
                    continue;
                } else if (key_evt == BTN_EVENT_KEY_LONG) {
                    /* KEY long is reserved for cancel/back semantics; do not trigger outfits here. */
                    vTaskDelay(pdMS_TO_TICKS(30));
                    continue;
                }
            }

            /*
             * 语音播放如果还没结束，就算 FSM 已经因为 5s 超时回到 IDLE，
             * 也不能恢复唤醒词监听，否则会把自己的播报重新录进去。
             */
            if (audio_is_playing() || ws_client_is_tts_active()) {
                if (ww_listening) {
                    wake_word_stop();
                    wake_word_clear_ring_buffer();
                    ww_listening = false;
                    ESP_LOGI(TAG, "Wake word listening paused while audio is still playing");
                }
                vTaskDelay(pdMS_TO_TICKS(20));
                continue;
            }

            /* --- 启动 I2S keep-alive 用于唤醒词监听 --- */
            if (ww_available && !wifi_manager_is_provisioning() &&
                !ww_listening && now_ms >= s_wake_resume_after_ms) {
                esp_err_t ka_ret = audio_i2s_keep_alive();
                if (ka_ret == ESP_OK) {
                    ww_listening = true;
                    wake_word_clear_ring_buffer();
                    wake_word_start();
                    ESP_LOGI(TAG, "Wake word listening started");
                } else {
                    ESP_LOGW(TAG, "I2S keep-alive failed: 0x%x", ka_ret);
                }
            }

            /* --- 唤醒词检测：读 I2S → 提取 mono → feed MultiNet --- */
            if (ww_available && ww_listening) {
                size_t bytes_read = 0;
                int64_t ww_read_started_ms = esp_timer_get_time() / 1000;
                esp_err_t rd = audio_i2s_read_wake(
                    (uint8_t *)ww_i2s_buf, ww_i2s_bytes,
                    &bytes_read, pdMS_TO_TICKS(60));
                int64_t ww_read_ms = (esp_timer_get_time() / 1000) - ww_read_started_ms;
                if (ww_read_ms > 250) {
                    ESP_LOGW(TAG, "DIAG wake I2S read slow: %lld ms ret=0x%x bytes=%d",
                             (long long)ww_read_ms, rd, (int)bytes_read);
                }

                if (rd == ESP_OK && bytes_read > 0) {
                    /* TDM 4-slot → mono: 提取 SLOT0，不加增益（MultiNet 需要原始音量） */
                    size_t total_i16 = bytes_read / sizeof(int16_t);
                    size_t frames = total_i16 / MIC_SLOT_COUNT;
                    if (frames > (size_t)ww_feed_size) frames = ww_feed_size;

                    for (size_t f = 0; f < frames; f++) {
                        ww_mono_buf[f] = ww_i2s_buf[f * MIC_SLOT_COUNT + MIC_CAPTURE_SLOT];
                    }
                    for (size_t f = frames; f < (size_t)ww_feed_size; f++) {
                        ww_mono_buf[f] = 0;
                    }
                    wake_word_feed(ww_mono_buf, ww_feed_size);

                    if (wake_word_detected()) {
                        ESP_LOGI(TAG, "*** '你好奥拉' detected! Capturing command... ***");

                        if (!ws_client_is_ready()) {
                            ui_set_dialogue(wifi_manager_is_connected() ? tr_text(&T_CONNECTING) : tr_text(&T_VOICE_OFFLINE), 20);
                            continue;
                        }

                        /*
                         * === "你好奥拉，XXX" 连续拾取模式 ===
                         * 不播提示音，不切状态，直接在这里录音。
                         * I2S 已经在跑 (keep-alive 模式)，继续读就行。
                         *
                         * 1. 从 ring buffer 取唤醒词后的残留音频
                         * 2. 继续读 I2S 直到 VAD 检测到静音
                         * 3. 整段发给服务器
                         */
                        wake_word_stop();
                        ww_listening = false;

                        /* A wake word is an explicit user gesture.  Arm the
                         * bounded hands-free session; the server still has
                         * to opt in to each follow-up turn. */
                        continuous_session_begin();

                        /* 切到 LISTENING 状态 (UI 显示录音中) */
                        fsm_handle_event(AURA_EVT_WAKE_WORD);
                        ui_set_dialogue(tr_text(&T_HEARD_WAKE), 30);

                        /* 复用 batch_buf (PSRAM, 10秒容量) */
                        #define WW_BATCH_MAX_SAMPLES (AUDIO_SAMPLE_RATE * 30)
                        static int16_t *ww_batch_buf = NULL;
                        if (!ww_batch_buf) {
                            ww_batch_buf = heap_caps_malloc(
                                WW_BATCH_MAX_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM);
                        }
                        if (!ww_batch_buf) {
                            ESP_LOGE(TAG, "Failed to alloc wake word batch buffer");
                            fsm_handle_event(AURA_EVT_ABORT);
                            continue;
                        }
                        size_t ww_batch_pos = 0;
                        bool ww_send_started = false;

                        /* Step 1: 从 ring buffer 取残留音频 (豆豆说完后到检测那一刻) */
                        int trailing = wake_word_get_trailing_audio(
                            ww_batch_buf, WW_BATCH_MAX_SAMPLES);
                        if (trailing > 0) {
                            /* 对残留音频也加增益 (跟正常录音一致) */
                            for (int t = 0; t < trailing; t++) {
                                int32_t s = (int32_t)ww_batch_buf[t] * MIC_SW_GAIN;
                                if (s > 32767) s = 32767;
                                if (s < -32768) s = -32768;
                                ww_batch_buf[t] = (int16_t)s;
                            }
                            ww_batch_pos = trailing;
                            ESP_LOGI(TAG, "Ring buffer trailing: %d samples (%.0f ms)",
                                     trailing, trailing * 1000.0f / AUDIO_SAMPLE_RATE);
                        }

                        /* Step 2: 继续读 I2S 直到 VAD 静音或超时 */
                        /* I2S keep-alive 已经在跑，直接用 audio_i2s_read_wake 继续读 */
                        int64_t rec_start_ms = esp_timer_get_time() / 1000;
                        int64_t last_speech_ms = rec_start_ms;
                        int64_t speech_candidate_started_ms = -1;
                        bool had_speech = false;
                        bool cancel_recording = false;
                        int vad_chunk_cnt = 0;

                        ESP_LOGI(
                            TAG,
                            "Wake VAD started (silence=%dms no_speech=%dms min_speech=%dms max=%dms)",
                            WW_VAD_SILENCE_MS,
                            WW_VAD_NO_SPEECH_MS,
                            WW_VAD_MIN_SPEECH_MS,
                            WW_VAD_MAX_RECORD_MS
                        );

                        while (fsm_get_state() == AURA_STATE_LISTENING) {
                            task_wdt_reset_if(wdt_enabled);
                            int64_t now_ms = esp_timer_get_time() / 1000;
                            /* 超时保护 */
                            if (now_ms - rec_start_ms > WW_VAD_MAX_RECORD_MS) {
                                ESP_LOGW(TAG, "VAD: max record time reached");
                                break;
                            }

                            /* 按键可以手动停止 */
                            if (voice_button_any_pressed()) {
                                vTaskDelay(pdMS_TO_TICKS(30));
                                if (voice_button_any_pressed()) {
                                    cancel_recording = button_reached_cancel_hold();
                                    if (cancel_recording) {
                                        ESP_LOGI(TAG, "VAD: button long-press cancel");
                                        fsm_handle_event(AURA_EVT_ABORT);
                                        ui_set_dialogue(tr_text(&T_CANCELLED), 20);
                                        button_wait_release();
                                    } else {
                                        ESP_LOGI(TAG, "VAD: button stop");
                                    }
                                    break;
                                }
                            }

                            /* 读 I2S */
                            size_t vad_bytes_read = 0;
                            esp_err_t vrd = audio_i2s_read_wake(
                                (uint8_t *)ww_i2s_buf, ww_i2s_bytes,
                                &vad_bytes_read, pdMS_TO_TICKS(60));

                            if (vrd != ESP_OK || vad_bytes_read == 0) {
                                vTaskDelay(pdMS_TO_TICKS(5));
                                continue;
                            }

                            /* TDM → mono + 增益 */
                            size_t vad_total_i16 = vad_bytes_read / sizeof(int16_t);
                            size_t vad_frames = vad_total_i16 / MIC_SLOT_COUNT;
                            int64_t chunk_energy = 0;
                            int chunk_peak = 0;
                            for (size_t f = 0; f < vad_frames && ww_batch_pos < WW_BATCH_MAX_SAMPLES; f++) {
                                int32_t sample = (int32_t)ww_i2s_buf[f * MIC_SLOT_COUNT + MIC_CAPTURE_SLOT];
                                sample *= MIC_SW_GAIN;
                                if (sample > 32767) sample = 32767;
                                if (sample < -32768) sample = -32768;
                                int16_t pcm_sample = (int16_t)sample;
                                int abs_sample = pcm_sample < 0 ? -pcm_sample : pcm_sample;
                                if (abs_sample > chunk_peak) {
                                    chunk_peak = abs_sample;
                                }
                                ww_batch_buf[ww_batch_pos++] = pcm_sample;
                                chunk_energy += (int64_t)pcm_sample * pcm_sample;
                            }

                            if (ww_send_started) {
                                pcm_upload_update(ww_batch_pos);
                            }

                            /* 计算 RMS */
                            int rms = (vad_frames > 0) ?
                                (int)sqrt((double)chunk_energy / vad_frames) : 0;
                            int chunk_ms = (vad_frames > 0) ?
                                (int)((vad_frames * 1000 + AUDIO_SAMPLE_RATE - 1) / AUDIO_SAMPLE_RATE) : 0;
                            if (chunk_ms <= 0) {
                                chunk_ms = 1;
                            }
                            int64_t vad_eval_ms = esp_timer_get_time() / 1000;

                            vad_chunk_cnt++;
                            if (vad_chunk_cnt <= 5 || vad_chunk_cnt % 20 == 0) {
                                ESP_LOGI(TAG, "VAD #%d: rms=%d peak=%d pos=%d",
                                         vad_chunk_cnt, rms, chunk_peak, (int)ww_batch_pos);
                            }

                            /* 更新 UI 音量条 */
                            int mic_level = rms / 140;
                            if (mic_level > 100) mic_level = 100;
                            if (mic_level < 0) mic_level = 0;
                            g_state.mic_level = mic_level;
                            aura_ui_mark_dirty();

                            /* VAD 判定 */
                            int speech_start_threshold = had_speech ?
                                WW_VAD_SILENCE_THRESHOLD : WW_VAD_SPEECH_START_RMS;
                            bool speech_like = rms >= speech_start_threshold;
                            if (!had_speech && chunk_peak < WW_VAD_SPEECH_START_PEAK) {
                                speech_like = false;
                            }
                            if (speech_like) {
                                if (speech_candidate_started_ms < 0) {
                                    speech_candidate_started_ms = vad_eval_ms - chunk_ms;
                                    if (speech_candidate_started_ms < rec_start_ms) {
                                        speech_candidate_started_ms = rec_start_ms;
                                    }
                                }
                                if (!had_speech) {
                                    int64_t speech_ms = vad_eval_ms - speech_candidate_started_ms;
                                    if (speech_ms >= WW_VAD_MIN_SPEECH_MS) {
                                        had_speech = true;
                                        last_speech_ms = vad_eval_ms;
                                        ESP_LOGI(TAG, "VAD: speech confirmed after %lld ms",
                                                 (long long)speech_ms);
                                        ww_send_started = pcm_upload_begin(
                                            ww_batch_buf, WW_BATCH_MAX_SAMPLES, false);
                                        if (!ww_send_started) {
                                            ESP_LOGE(TAG, "Failed to start wake word upload session");
                                            cancel_recording = true;
                                            fsm_handle_event(AURA_EVT_ABORT);
                                            break;
                                        }
                                        pcm_upload_update(ww_batch_pos);
                                    }
                                } else {
                                    last_speech_ms = vad_eval_ms;
                                }
                            } else {
                                speech_candidate_started_ms = -1;
                            }

                            if (had_speech && (vad_eval_ms - last_speech_ms > WW_VAD_SILENCE_MS)) {
                                /* 检测到语音后持续静音 → 用户说完了 */
                                ESP_LOGI(TAG, "VAD: silence detected after speech (%.0f ms quiet)",
                                         (double)(vad_eval_ms - last_speech_ms));
                                break;
                            }

                            /* 最少需要一点语音才判定 "说了话" */
                            if (!had_speech && (vad_eval_ms - rec_start_ms > WW_VAD_NO_SPEECH_MS)) {
                                ESP_LOGW(TAG, "VAD: no confirmed speech in %d ms, aborting",
                                         WW_VAD_NO_SPEECH_MS);
                                break;
                            }

                            if (ww_batch_pos >= WW_BATCH_MAX_SAMPLES) {
                                ESP_LOGW(TAG, "VAD: buffer full");
                                break;
                            }
                        }

                        /* Step 3: 录音完成，发送 */
                        g_state.mic_level = 0;
                        aura_ui_mark_dirty();
                        fsm_handle_event(AURA_EVT_VOICE_STOP);

                        float ww_dur_s = (float)ww_batch_pos / AUDIO_SAMPLE_RATE;
                        ESP_LOGI(TAG, "Wake word recording done: %d samples (%.1fs), had_speech=%d",
                                 (int)ww_batch_pos, ww_dur_s, had_speech);

                        if (cancel_recording) {
                            if (ww_send_started) {
                                pcm_upload_abort();
                            }
                        } else if (ww_batch_pos > 0 && had_speech && ww_send_started) {
                            pcm_upload_finish(ww_batch_pos);
                            ESP_LOGI(TAG, "Wake word streamed: %d samples", (int)ww_batch_pos);
                        } else {
                            if (ww_send_started) {
                                pcm_upload_abort();
                            }
                            ESP_LOGW(TAG, "No valid speech after wake word, skipping");
                            fsm_handle_event(AURA_EVT_ABORT);
                        }

                        continue;
                    }
                }
            }

            /* 没有唤醒词时才 delay，有的话靠 I2S read 的 timeout 节拍 */
            if (!ww_listening) {
                vTaskDelay(pdMS_TO_TICKS(20));
            }
            continue;
        }

        /* ── SPEAKING: 仅允许按键打断，不做唤醒词检测 ── */
        if (state == AURA_STATE_SPEAKING) {
            if (ww_listening) {
                wake_word_stop();
                wake_word_clear_ring_buffer();
                ww_listening = false;
                ESP_LOGI(TAG, "Wake word listening paused during SPEAKING");
            }

            button_event_t boot_evt = buttons_poll_boot();
            button_event_t key_evt = buttons_poll_key();
            if (s_continuous_session_active &&
                (boot_evt == BTN_EVENT_BOOT_SHORT || boot_evt == BTN_EVENT_BOOT_LONG)) {
                ESP_LOGI(TAG, "BOOT during hands-free speaking -> exit to menu");
                ws_client_cancel_pending_reply();
                continuous_session_end();
                fsm_handle_event(AURA_EVT_ABORT);
                s_menu_open = true;
                s_volume_menu_open = false;
                s_wifi_menu_open = false;
                s_menu_sel = 0;
                aura_ui_reset_idle_surface();
                aura_ui_mark_dirty();
            } else if (s_continuous_session_active &&
                       (key_evt == BTN_EVENT_KEY_SHORT || key_evt == BTN_EVENT_KEY_LONG)) {
                ESP_LOGI(TAG, "KEY during hands-free speaking -> exit continuous chat");
                ws_client_cancel_pending_reply();
                continuous_session_end();
                fsm_handle_event(AURA_EVT_ABORT);
                button_wait_release();
            } else if (key_evt == BTN_EVENT_KEY_SHORT || key_evt == BTN_EVENT_KEY_LONG ||
                       boot_evt == BTN_EVENT_BOOT_SHORT || boot_evt == BTN_EVENT_BOOT_LONG) {
                if (ws_client_is_ready()) {
                    ESP_LOGI(TAG, "Button during SPEAKING → interrupt and listen");
                    continuous_session_begin();
                    fsm_handle_event(AURA_EVT_WAKE_BUTTON);
                }
            }

            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }

        /* ── LISTENING: 录音循环 (toggle: 再按一下停止) ─── */
        if (state == AURA_STATE_LISTENING) {
            /* A continuation is latched only after the previous reply's TTS
             * has completed and the server explicitly opted in. */
            bool continuous_recording = ws_client_take_continuous_listening();
            aura_ui_set_continuous_dialogue(continuous_recording);
            if (continuous_recording) {
                int64_t session_now_ms = esp_timer_get_time() / 1000;
                if (!s_continuous_session_active) {
                    continuous_session_begin();
                    session_now_ms = esp_timer_get_time() / 1000;
                }
                if (s_continuous_session_turns >= CONTINUOUS_SESSION_MAX_TURNS ||
                    session_now_ms - s_continuous_session_started_ms >=
                        CONTINUOUS_SESSION_MAX_MS) {
                    ESP_LOGI(TAG, "Hands-free session limit reached turns=%u elapsed_ms=%lld",
                             (unsigned)s_continuous_session_turns,
                             (long long)(session_now_ms - s_continuous_session_started_ms));
                    continuous_session_end();
                    fsm_handle_event(AURA_EVT_ABORT);
                    ui_set_dialogue(tr_text(&T_CANCELLED), 20);
                    continue;
                }
                s_continuous_session_turns++;
                ESP_LOGI(TAG, "Hands-free continuation: recording next turn");
            }
            /*
             * BATCH 模式: 先录完整段到 PSRAM，按一下停止后一次性发送
             */
            audio_record_start_stream();
            ESP_LOGI(TAG, "Recording started (batch mode, click to stop)...");

            #define BATCH_MAX_SAMPLES (AUDIO_SAMPLE_RATE * 30)
            static int16_t *batch_buf = NULL;
            if (!batch_buf) {
                batch_buf = heap_caps_malloc(BATCH_MAX_SAMPLES * sizeof(int16_t),
                                             MALLOC_CAP_SPIRAM);
                if (!batch_buf) {
                    ESP_LOGE(TAG, "Failed to alloc batch buffer!");
                    audio_record_stop_stream();
                    fsm_handle_event(AURA_EVT_ABORT);
                    continue;
                }
            }
            size_t batch_pos = 0;
            int rec_dump_cnt = 0;
            int fail_cnt = 0;
            int pcm_chunk_cnt = 0;

            int64_t record_start_ms = esp_timer_get_time() / 1000;
            const int64_t MAX_RECORD_MS = continuous_recording
                ? CONTINUOUS_VAD_MAX_RECORD_MS : 30000;

            /*
             * 兼容按住说话和短按 toggle：如果进入 LISTENING 时按键仍按着，
             * 初次松开就停止；否则保留“再次按下停止”的旧路径。
             * （600ms 长按判定已在 IDLE 分支完成，进到这里的按键会话都是已确认的。）
             */
            s_ignore_listening_release = !continuous_recording && voice_button_any_pressed();

            bool send_started = pcm_upload_begin(batch_buf, BATCH_MAX_SAMPLES, AURA_SERVER_VAD_DEFAULT);
            if (!send_started) {
                ESP_LOGE(TAG, "Failed to start button upload session");
                audio_record_stop_stream();
                fsm_handle_event(AURA_EVT_ABORT);
                continue;
            }

            bool cancel_recording = false;
            bool continuous_exit_requested = false;
            bool stopped_by_server_vad = false;
            bool continuous_had_speech = false;
            int64_t continuous_last_speech_ms = record_start_ms;
            int64_t continuous_speech_candidate_ms = -1;

            while (fsm_get_state() == AURA_STATE_LISTENING) {
                task_wdt_reset_if(wdt_enabled);
                int64_t now_ms = esp_timer_get_time() / 1000;
                if (now_ms - record_start_ms > MAX_RECORD_MS) {
                    ESP_LOGW(TAG, "Recording timeout, auto-stopping");
                    break;
                }

                bool button_pressed = voice_button_any_pressed();

                /* In hands-free mode BOOT opens the menu and the voice key
                 * exits the session. Neither action should be interpreted as
                 * press-to-send. */
                if (continuous_recording) {
                    button_event_t continuous_boot_evt = buttons_poll_boot();
                    if (continuous_boot_evt == BTN_EVENT_BOOT_SHORT ||
                        continuous_boot_evt == BTN_EVENT_BOOT_LONG) {
                        ESP_LOGI(TAG, "BOOT during hands-free recording -> exit to menu");
                        continuous_exit_requested = true;
                        cancel_recording = true;
                        continuous_session_end();
                        fsm_handle_event(AURA_EVT_ABORT);
                        s_menu_open = true;
                        s_volume_menu_open = false;
                        s_wifi_menu_open = false;
                        s_menu_sel = 0;
                        aura_ui_reset_idle_surface();
                        break;
                    }
                }

                if (!continuous_recording && s_ignore_listening_release && !button_pressed) {
                    ESP_LOGI(TAG, "Button release → stop recording");
                    s_ignore_listening_release = false;
                    break;
                }

                if (ws_client_take_server_vad_stop()) {
                    ESP_LOGI(TAG, "Backend server_vad_stop → stop local recording");
                    stopped_by_server_vad = true;
                    break;
                }

                if (send_started && pcm_upload_take_failure()) {
                    ESP_LOGW(TAG, "Upload stream failed mid-recording → abort turn");
                    fsm_handle_event(AURA_EVT_ABORT);
                    break;
                }

                if (continuous_recording && button_pressed) {
                    ESP_LOGI(TAG, "Voice key during hands-free recording -> exit");
                    cancel_recording = true;
                    continuous_exit_requested = true;
                    continuous_session_end();
                    fsm_handle_event(AURA_EVT_ABORT);
                    button_wait_release();
                    break;
                }

                /* Toggle: 松开后再次按下 → 停止录音 */
                if (!continuous_recording && !s_ignore_listening_release && button_pressed) {
                    vTaskDelay(pdMS_TO_TICKS(30));  /* 去抖 */
                    if (voice_button_any_pressed()) {
                        cancel_recording = button_reached_cancel_hold();
                        if (cancel_recording) {
                            ESP_LOGI(TAG, "Button long-press → cancel recording");
                            fsm_handle_event(AURA_EVT_ABORT);
                            ui_set_dialogue(tr_text(&T_CANCELLED), 20);
                            button_wait_release();
                        } else {
                            ESP_LOGI(TAG, "Button click → stop recording");
                        }
                        break;
                    }
                }

                size_t bytes_read = 0;
                esp_err_t ret = audio_record_read(
                    (uint8_t *)i2s_buf, MIC_I2S_CHUNK_BYTES,
                    &bytes_read, pdMS_TO_TICKS(100));

                if (++rec_dump_cnt <= 3) {
                    ESP_LOGI(TAG, "REC TDM16 #%d: bytes=%d [%d,%d,%d,%d]",
                             rec_dump_cnt, (int)bytes_read,
                             (int)i2s_buf[0], (int)i2s_buf[1],
                             (int)i2s_buf[2], (int)i2s_buf[3]);
                }

                if (ret != ESP_OK || bytes_read == 0) {
                    if (++fail_cnt <= 5) {
                        ESP_LOGW(TAG, "I2S read fail: 0x%x, %d bytes", ret, (int)bytes_read);
                    }
                    vTaskDelay(pdMS_TO_TICKS(10));
                    continue;
                }

                /* TDM 4-slot → mono: 提取 SLOT0 + 软件增益 */
                size_t total_i16 = bytes_read / sizeof(int16_t);
                size_t frames = total_i16 / MIC_SLOT_COUNT;
                for (size_t f = 0; f < frames && batch_pos < BATCH_MAX_SAMPLES; f++) {
                    int32_t sample = (int32_t)i2s_buf[f * MIC_SLOT_COUNT + MIC_CAPTURE_SLOT];
                    sample *= MIC_SW_GAIN;
                    if (sample > 32767) sample = 32767;
                    if (sample < -32768) sample = -32768;
                    int16_t pcm_sample = (int16_t)sample;
                    batch_buf[batch_pos++] = pcm_sample;
                }

                if (send_started) {
                    pcm_upload_update(batch_pos);
                }

                int64_t sum_sq = 0;
                int chunk_peak = 0;
                size_t start = (batch_pos > 240) ? batch_pos - 240 : 0;
                for (size_t j = start; j < batch_pos; j++) {
                    int sample_abs = batch_buf[j] < 0 ? -(int)batch_buf[j] : (int)batch_buf[j];
                    if (sample_abs > chunk_peak) chunk_peak = sample_abs;
                    sum_sq += (int64_t)batch_buf[j] * batch_buf[j];
                }
                int rms = (batch_pos > start) ?
                    (int)sqrt((double)sum_sq / (batch_pos - start)) : 0;
                int mic_level = rms / 140;
                if (mic_level > 100) mic_level = 100;
                if (mic_level < 0) mic_level = 0;
                if (g_state.mic_level != mic_level) {
                    g_state.mic_level = mic_level;
                    aura_ui_mark_dirty();
                }

                if (continuous_recording) {
                    int64_t vad_now_ms = esp_timer_get_time() / 1000;
                    int threshold = continuous_had_speech
                        ? CONTINUOUS_VAD_SILENCE_THRESHOLD
                        : CONTINUOUS_VAD_SPEECH_START_RMS;
                    bool speech_like = rms >= threshold;
                    if (!continuous_had_speech &&
                        chunk_peak < CONTINUOUS_VAD_SPEECH_START_PEAK) {
                        speech_like = false;
                    }

                    if (speech_like) {
                        if (!continuous_had_speech) {
                            if (continuous_speech_candidate_ms < 0) {
                                continuous_speech_candidate_ms = vad_now_ms;
                            }
                            if (vad_now_ms - continuous_speech_candidate_ms >=
                                CONTINUOUS_VAD_MIN_SPEECH_MS) {
                                continuous_had_speech = true;
                                continuous_last_speech_ms = vad_now_ms;
                                ESP_LOGI(TAG, "Hands-free VAD speech confirmed after %lld ms",
                                         (long long)(vad_now_ms - record_start_ms));
                            }
                        } else {
                            continuous_last_speech_ms = vad_now_ms;
                        }
                    } else if (!continuous_had_speech) {
                        continuous_speech_candidate_ms = -1;
                    }

                    if (!continuous_had_speech &&
                        vad_now_ms - record_start_ms >= CONTINUOUS_VAD_NO_SPEECH_MS) {
                        ESP_LOGI(TAG, "Hands-free VAD no speech timeout (%d ms) -> exit",
                                 CONTINUOUS_VAD_NO_SPEECH_MS);
                        cancel_recording = true;
                        continuous_exit_requested = true;
                        continuous_session_end();
                        fsm_handle_event(AURA_EVT_ABORT);
                        break;
                    }
                    if (continuous_had_speech &&
                        vad_now_ms - continuous_last_speech_ms >= CONTINUOUS_VAD_SILENCE_MS) {
                        ESP_LOGI(TAG, "Hands-free VAD silence (%d ms) -> submit turn",
                                 CONTINUOUS_VAD_SILENCE_MS);
                        break;
                    }
                }

                pcm_chunk_cnt++;
                if (pcm_chunk_cnt <= 3 || pcm_chunk_cnt % 10 == 0) {
                    ESP_LOGI(TAG, "REC batch #%d: pos=%d rms=%d",
                             pcm_chunk_cnt, (int)batch_pos, rms);
                }

                if (batch_pos >= BATCH_MAX_SAMPLES) {
                    ESP_LOGW(TAG, "Batch buffer full");
                    break;
                }
            }

            /* ── 立刻停止录音 & 切到 PROCESSING 状态 ── */
            audio_record_stop_stream();
            g_state.mic_level = 0;
            if (!stopped_by_server_vad) {
                stopped_by_server_vad = ws_client_take_server_vad_stop();
            }
            bool finalized_by_backend = stopped_by_server_vad ||
                (fsm_get_state() != AURA_STATE_LISTENING);
            if (!finalized_by_backend) {
                fsm_handle_event(AURA_EVT_VOICE_STOP);  /* 先切状态！用户看到"思考中" */
            }

            float dur_s = (float)batch_pos / AUDIO_SAMPLE_RATE;
            ESP_LOGI(TAG, "Recording done: %d samples (%.1fs), finalizing stream...",
                     (int)batch_pos, dur_s);

            bool recording_too_short =
                batch_pos < (size_t)(AUDIO_SAMPLE_RATE * PTT_MIN_RECORD_MS / 1000);
            if (!cancel_recording && !finalized_by_backend && recording_too_short) {
                ESP_LOGI(TAG, "Recording too short (<%dms) -> cancel locally", PTT_MIN_RECORD_MS);
                if (send_started) {
                    pcm_upload_abort();
                }
                ws_client_cancel_pending_reply();
                fsm_handle_event(AURA_EVT_ABORT);
                ws_client_send_cancel("recording_too_short");
                sfx_play(SFX_CANCEL);
                vTaskDelay(pdMS_TO_TICKS(30));
                continue;
            }

            /* 录音期间已持续上传，这里只需要结束流 */
            if (cancel_recording) {
                if (send_started) {
                    pcm_upload_abort();
                }
                if (continuous_exit_requested) {
                    ws_client_send_cancel("continuous_exit");
                }
            } else if (finalized_by_backend) {
                if (send_started) {
                    pcm_upload_abort();
                }
                ESP_LOGI(TAG, "Recording already finalized by backend (server_vad=%d, state=%s)",
                         stopped_by_server_vad ? 1 : 0,
                         fsm_state_name(fsm_get_state()));
            } else if (batch_pos > 0 && send_started) {
                pcm_upload_finish(batch_pos);
                ESP_LOGI(TAG, "Button recording streamed: %d samples (%d bytes)", (int)batch_pos, (int)(batch_pos * 2));
            } else if (batch_pos == 0) {
                if (send_started) {
                    pcm_upload_abort();
                }
                ESP_LOGW(TAG, "No audio, skipping");
                fsm_handle_event(AURA_EVT_ABORT);
            }

            ESP_LOGI(TAG, "Waiting for AI response...");
            continue;
        }

        /* ── PROCESSING: 等待后端回复 (带超时+按键取消) ────── */
        if (state == AURA_STATE_PROCESSING) {
            /* 最多等 30 秒，期间按键可取消 */
            static int processing_ticks = 0;
            if (pcm_upload_take_failure()) {
                ESP_LOGW(TAG, "Upload failure detected in PROCESSING → abort");
                ws_client_cancel_pending_reply();
                fsm_handle_event(AURA_EVT_ABORT);
                processing_ticks = 0;
                continue;
            }
            processing_ticks++;

            /* 按键取消 */
            button_event_t boot_cancel_evt = buttons_poll_boot();
            button_event_t cancel_evt = buttons_poll_key();
            if (cancel_evt == BTN_EVENT_KEY_SHORT || boot_cancel_evt == BTN_EVENT_BOOT_SHORT) {
                ESP_LOGW(TAG, "Button pressed during PROCESSING — cancelling!");
                ws_client_cancel_pending_reply();
                fsm_handle_event(AURA_EVT_ABORT);
                ui_set_dialogue(tr_text(&T_CANCELLED), 20);
                processing_ticks = 0;
                /* 吃掉按键，等松手后再允许下次唤醒 */
                vTaskDelay(pdMS_TO_TICKS(500));
                /* 清空按键队列 */
                while (buttons_poll_key() != BTN_EVENT_NONE ||
                       buttons_poll_boot() != BTN_EVENT_NONE) {
                    vTaskDelay(1);
                }
                continue;
            }

            /* 超时 30 秒 (300 × 100ms)，避免坏 turn 卡住整机交互 */
            if (processing_ticks > PROCESSING_TIMEOUT_TICKS) {
                ESP_LOGW(TAG, "PROCESSING timeout (30s) — returning to IDLE");
                ws_client_cancel_pending_reply();
                fsm_handle_event(AURA_EVT_ABORT);
                processing_ticks = 0;
                continue;
            }

            vTaskDelay(pdMS_TO_TICKS(100));

            /* 如果 FSM 已经不是 PROCESSING 了 (被 ws 回调改变)，重置计数 */
            if (fsm_get_state() != AURA_STATE_PROCESSING) {
                processing_ticks = 0;
            }
            continue;
        }

        /* fallback */
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

/* ── App Main ──────────────────────────────────────── */
void app_main(void)
{
    /* Prevent the PA enable pin from floating during the comparatively long boot path. */
    ESP_ERROR_CHECK(es8311_prepare_pa_muted());
    ESP_LOGI(TAG, "=== Aura 豆豆 Starting ===");

    // PSRAM 检查
    if (esp_psram_is_initialized()) {
        ESP_LOGI(TAG, "PSRAM: %zu bytes available", esp_psram_get_size());
    } else {
        ESP_LOGW(TAG, "PSRAM not available!");
    }

    // NVS 初始化
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    // I2C 总线
    ESP_ERROR_CHECK(i2c_bus_init());
    ESP_LOGI(TAG, "I2C bus initialized");

    // SPIFFS 资源文件系统（内嵌在 Flash 中）
    esp_vfs_spiffs_conf_t spiffs_conf = {
        .base_path = SPIFFS_MOUNT_POINT,
        .partition_label = "assets",
        .max_files = 10,
        .format_if_mount_failed = false,
    };
    ret = esp_vfs_spiffs_register(&spiffs_conf);
    if (ret == ESP_OK) {
        size_t total = 0, used = 0;
        esp_spiffs_info("assets", &total, &used);
        ESP_LOGI(TAG, "SPIFFS mounted: %zu/%zu bytes used", used, total);
        ota_update_recover_resources();
    } else {
        ESP_LOGW(TAG, "SPIFFS mount failed (0x%x), will try SD card", ret);
    }

    // 加载中文字体
    esp_err_t font_ret = font_cn16_init();
    if (font_ret != ESP_OK) {
        ESP_LOGE(TAG, "!!!!! CN16 FONT LOAD FAILED: 0x%x !!!!!", font_ret);
    } else {
        ESP_LOGI(TAG, "CN16 font loaded OK");
    }

    // SD 卡（可选，作为备用存储）
    sd_card_init();

    // 事件组
    g_event_group = xEventGroupCreate();

    // 初始化状态机
    fsm_init(on_fsm_transition);

    // SPEAKING 超时定时器
    const esp_timer_create_args_t timer_args = {
        .callback = speak_timeout_cb,
        .name = "speak_timeout",
    };
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &s_speak_timer));

    // 初始化全局状态
    memset(&g_state, 0, sizeof(g_state));
    ui_language_load();
    ws_client_set_language(ui_language_code());
    g_state.current_pose = esp_random() % 2;  // 待机姿势随机 0/1
    g_state.wifi_strength = 0;
    g_state.temperature = 23.5f;
    g_state.hour = 14;
    g_state.minute = 30;
    g_state.month = 4;
    g_state.day = 10;
    g_state.weather_icon = 0;
    if (s_language_select_open) {
        ui_set_dialogue("", 0);
    } else {
        ui_set_dialogue(tr_text(&T_BOOT_HELLO), 80);
    }
    g_state.ui_mode = AURA_UI_IDLE;
    ui_set_agent_panel(false, 0, "AGENT", "");
    if (!aura_companion_state_cache_load()) {
        g_state.companion_state_ready = false;
        g_state.mood = 0;
        g_state.energy = 0;
        g_state.satiety = 0;
        g_state.affinity = 0;
        g_state.affinity_level = 0;
        g_state.coins = 0;
        g_state.current_outfit = 0;
        g_state.outfit_unlocked = BASIC_OUTFIT_UNLOCK_MASK;
        g_state.quota_ready = false;
        g_state.quota_provider[0] = '\0';
        g_state.quota_headline[0] = '\0';
        g_state.quota_percent = 0;
        g_state.quota_text[0] = '\0';
        g_state.quota_primary_label[0] = '\0';
        g_state.quota_primary_text[0] = '\0';
        g_state.quota_primary_percent = 0;
        g_state.quota_secondary_label[0] = '\0';
        g_state.quota_secondary_text[0] = '\0';
        g_state.quota_secondary_percent = 0;
        ESP_LOGI(TAG, "No companion cache yet; waiting for server status_update");
    }
    aura_ui_mark_dirty();

    // 创建任务
    create_task_checked(display_task, "display", TASK_DISPLAY_STACK, TASK_DISPLAY_PRIO, 1);
    /* 音频任务放 core 1：Wi-Fi 驱动任务(prio 23)在 core 0，连接期间会把音频任务
     * 饿死几百毫秒（实测 I2S 写被卡 414ms），造成开机音乐/首句卡顿 */
    create_task_checked(audio_task, "audio", TASK_AUDIO_STACK, TASK_AUDIO_PRIO, 1);
    create_task_checked(pcm_upload_task, "pcm_upload", TASK_UPLOAD_STACK, TASK_UPLOAD_PRIO, 0);
    create_task_checked(network_task, "network", TASK_NETWORK_STACK, TASK_NETWORK_PRIO, 0);
    create_task_checked(sensor_task, "sensor", TASK_SENSOR_STACK, TASK_SENSOR_PRIO, 0);
    create_task_checked(input_task, "input", TASK_INPUT_STACK, TASK_INPUT_PRIO, 0);

    ESP_LOGI(TAG, "All tasks created, system running");
}
