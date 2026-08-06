/**
 * 音频管线 — TTS 播放 + 语音录制 + 流式录音
 */
#pragma once
#include <stdbool.h>
#include "esp_err.h"
#include <stdint.h>
#include <stddef.h>
#include "freertos/FreeRTOS.h"

typedef enum {
    AUDIO_PLAYBACK_SOURCE_DEFAULT = 0,
    AUDIO_PLAYBACK_SOURCE_MUSIC = 1,
} audio_playback_source_t;

esp_err_t audio_pipeline_init(void);
void audio_pipeline_loop(void);

// 播放 PCM 数据 (16-bit, 16kHz, mono)
esp_err_t audio_play_pcm(const uint8_t *data, size_t len);
// 复制 PCM 数据后播放，适合临时生成的 TTS 缓冲
esp_err_t audio_play_pcm_copy(const uint8_t *data, size_t len);
// 复制 PCM 数据后追加到播放队列，适合分块 TTS
esp_err_t audio_queue_pcm_copy(const uint8_t *data, size_t len);
// 指定来源复制 PCM 数据后播放/追加，供本地音乐等扩展能力区分播放类型
esp_err_t audio_play_pcm_copy_source(const uint8_t *data, size_t len, audio_playback_source_t source);
esp_err_t audio_queue_pcm_copy_source(const uint8_t *data, size_t len, audio_playback_source_t source);
esp_err_t audio_queue_pcm_copy_tail_source(const uint8_t *data, size_t len, audio_playback_source_t source);
// 立即停止当前播放并清空播放队列
void audio_stop_playback(void);

// ── 批量录音 (旧接口，保留兼容) ──────────
esp_err_t audio_record_start(void);
esp_err_t audio_record_stop(uint8_t **out_data, size_t *out_len);

// ── 流式录音 (语音交互用) ────────────────
esp_err_t audio_record_start_stream(void);
// 直接从 I2S 读取原始数据 (32-bit stereo)
esp_err_t audio_record_read(uint8_t *buf, size_t buf_size,
                             size_t *bytes_read, TickType_t timeout);
// 停止流式录音 (不返回缓冲)
esp_err_t audio_record_stop_stream(void);

// 是否正在播放
bool audio_is_playing(void);
// 是否仍有非音乐来源的播放数据（TTS/SFX 等）
bool audio_is_non_music_playing(void);
// 当前播放队列中剩余的 PCM 字节数
size_t audio_get_playback_queued_bytes(void);
// 低延迟调试：标记即将入队的 TTS turn，用于记录真实 I2S 首次写出时间
void audio_debug_mark_tts_turn(uint32_t turn_id, int64_t turn_started_at_ms, int64_t pcm_queued_at_ms);

// ── 唤醒词检测用 I2S 接口 ────────────────
// 启用 I2S TX+RX (保持时钟，用于待机监听)
esp_err_t audio_i2s_keep_alive(void);
// 读取 I2S 数据 (不设置 s_streaming 标志)
esp_err_t audio_i2s_read_wake(uint8_t *buf, size_t buf_size,
                               size_t *bytes_read, TickType_t timeout);
