/**
 * 音效播放 — 开机/报错/发送/收到回复/休眠/长按开始/取消
 */
#pragma once
#include <stdbool.h>
#include "esp_err.h"

typedef enum {
    SFX_STARTUP = 0,
    SFX_ERROR,
    SFX_SENT,
    SFX_REPLY,
    SFX_SLEEP,
    SFX_LISTEN_START,
    SFX_CANCEL,
    SFX_MAX,
} sfx_type_t;

/**
 * 初始化音效系统（从 SD 卡加载 PCM 文件）
 */
esp_err_t sfx_init(void);

/**
 * 播放指定音效（非阻塞，内部排队）
 */
esp_err_t sfx_play(sfx_type_t type);

/**
 * 播放资源文件中的 8-bit unsigned PCM（16kHz mono）。
 *
 * 用于启动设置等临时语音，不会常驻预加载到 SFX 表。
 */
esp_err_t sfx_play_file(const char *path);

/**
 * 是否有音效正在播放
 */
bool sfx_is_playing(void);
