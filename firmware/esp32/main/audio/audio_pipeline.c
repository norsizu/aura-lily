/**
 * 音频管线实现 — TX/RX 都是 TDM Philips 16-bit 4-slot
 * ES8311(DAC): 读 SLOT0/SLOT1 作为 L/R
 * ES7210(ADC): MIC1~MIC4 输出到 SLOT0~SLOT3
 * 16-bit 模式下 I2S driver 自动将 ES7210 24-bit ADC 截断为高 16 位。
 */
#include "audio_pipeline.h"
#include "aura_config.h"
#include "es8311.h"
#include "es7210.h"
#include "driver/i2s_tdm.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include <stdlib.h>
#include <string.h>

static const char *TAG = "audio";

static i2s_chan_handle_t s_tx_chan = NULL;
static i2s_chan_handle_t s_rx_chan = NULL;

// 播放队列
typedef struct play_chunk {
    const uint8_t *data;
    size_t len;
    size_t pos;
    bool owned;
    audio_playback_source_t source;
    struct play_chunk *next;
} play_chunk_t;

static play_chunk_t *s_play_head = NULL;
static play_chunk_t *s_play_tail = NULL;
static SemaphoreHandle_t s_play_mutex = NULL;
static size_t s_play_queued_bytes = 0;
static size_t s_play_music_queued_bytes = 0;
static uint32_t s_debug_tts_turn_id = 0;
static int64_t s_debug_tts_turn_started_at_ms = 0;
static int64_t s_debug_tts_pcm_queued_at_ms = 0;
static bool s_debug_tts_first_write_logged = false;

// 录音缓冲
#define RECORD_BUF_SIZE (AUDIO_SAMPLE_RATE * 2 * 10)
static uint8_t *s_record_buf = NULL;
static size_t s_record_pos = 0;
static bool s_recording = false;
static bool s_streaming = false;

// 播放状态
static bool s_tx_enabled = false;
static bool s_keep_alive = false;
static bool s_rx_enabled = false;
static bool s_output_muted = true;

#define PLAYBACK_TAIL_FADE_SAMPLES 320
#define PLAYBACK_TAIL_SILENCE_SAMPLES 640

static void apply_pcm_tail_fade(uint8_t *data, size_t len)
{
    if (!data || len < 2) {
        return;
    }

    size_t samples = len / sizeof(int16_t);
    size_t fade_samples = samples < PLAYBACK_TAIL_FADE_SAMPLES
        ? samples : PLAYBACK_TAIL_FADE_SAMPLES;
    if (fade_samples == 0) {
        return;
    }

    int16_t *pcm = (int16_t *)data;
    size_t start = samples - fade_samples;
    for (size_t i = 0; i < fade_samples; i++) {
        int32_t value = pcm[start + i];
        int32_t gain = (int32_t)(fade_samples - i - 1);
        pcm[start + i] = (int16_t)((value * gain) / (int32_t)fade_samples);
    }
}

static void free_play_chunk(play_chunk_t *chunk)
{
    if (!chunk) return;
    if (chunk->owned && chunk->data) {
        heap_caps_free((void *)chunk->data);
    }
    free(chunk);
}

static void clear_playback_queue_locked(void)
{
    while (s_play_head) {
        play_chunk_t *next = s_play_head->next;
        size_t remaining = (s_play_head->len > s_play_head->pos) ? (s_play_head->len - s_play_head->pos) : 0;
        if (remaining > 0) {
            s_play_queued_bytes = (remaining >= s_play_queued_bytes) ? 0 : (s_play_queued_bytes - remaining);
            if (s_play_head->source == AUDIO_PLAYBACK_SOURCE_MUSIC) {
                s_play_music_queued_bytes = (remaining >= s_play_music_queued_bytes)
                    ? 0 : (s_play_music_queued_bytes - remaining);
            }
        }
        free_play_chunk(s_play_head);
        s_play_head = next;
    }
    s_play_tail = NULL;
}

static esp_err_t enqueue_playback_locked(
    const uint8_t *data,
    size_t len,
    bool owned,
    audio_playback_source_t source
)
{
    if (!data || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    play_chunk_t *chunk = calloc(1, sizeof(play_chunk_t));
    if (!chunk) {
        if (owned) {
            heap_caps_free((void *)data);
        }
        return ESP_ERR_NO_MEM;
    }

    chunk->data = data;
    chunk->len = len;
    chunk->owned = owned;
    chunk->source = source;

    if (s_play_tail) {
        s_play_tail->next = chunk;
    } else {
        s_play_head = chunk;
    }
    s_play_tail = chunk;
    s_play_queued_bytes += len;
    if (source == AUDIO_PLAYBACK_SOURCE_MUSIC) {
        s_play_music_queued_bytes += len;
    }
    return ESP_OK;
}

static void maybe_disable_tx_locked(const char *reason)
{
    if (!s_play_head && s_tx_enabled) {
        if (!s_output_muted && es8311_mute(true) == ESP_OK) {
            s_output_muted = true;
        }
        if (s_keep_alive) {
            return;
        }
        esp_err_t ret = i2s_channel_disable(s_tx_chan);
        if (ret == ESP_OK || ret == ESP_ERR_INVALID_STATE) {
            s_tx_enabled = false;
            ESP_LOGI(TAG, "%s", reason);
        } else {
            ESP_LOGW(TAG, "TX disable failed: 0x%x", ret);
        }
    }
}

static esp_err_t ensure_tx_enabled(const char *reason)
{
    if (!s_tx_chan) return ESP_ERR_INVALID_STATE;
    if (s_tx_enabled) return ESP_OK;

    esp_err_t ret = i2s_channel_enable(s_tx_chan);
    if (ret == ESP_OK || ret == ESP_ERR_INVALID_STATE) {
        s_tx_enabled = true;
        /* Prime every TDM slot with digital silence before enabling the PA. */
        int16_t silence[64 * 4] = {0};
        size_t written = 0;
        ret = i2s_channel_write(s_tx_chan, silence, sizeof(silence), &written,
                                pdMS_TO_TICKS(50));
        if (ret != ESP_OK || written != sizeof(silence)) {
            ESP_LOGW(TAG, "TX silence prime failed: ret=0x%x written=%u/%u",
                     ret, (unsigned)written, (unsigned)sizeof(silence));
            es8311_mute(true);
            return ret != ESP_OK ? ret : ESP_FAIL;
        }
        if (reason) {
            ESP_LOGI(TAG, "%s", reason);
        }
        return ESP_OK;
    }
    ESP_LOGW(TAG, "TX enable failed: 0x%x", ret);
    return ret;
}

static esp_err_t ensure_rx_enabled(const char *reason)
{
    if (!s_rx_chan) return ESP_ERR_INVALID_STATE;
    if (s_rx_enabled) return ESP_OK;

    esp_err_t ret = i2s_channel_enable(s_rx_chan);
    if (ret == ESP_OK || ret == ESP_ERR_INVALID_STATE) {
        s_rx_enabled = true;
        if (reason) {
            ESP_LOGI(TAG, "%s", reason);
        }
        return ESP_OK;
    }
    ESP_LOGW(TAG, "RX enable failed: 0x%x", ret);
    return ret;
}

static esp_err_t ensure_rx_disabled(const char *reason)
{
    if (!s_rx_chan) return ESP_ERR_INVALID_STATE;
    if (!s_rx_enabled) return ESP_OK;

    esp_err_t ret = i2s_channel_disable(s_rx_chan);
    if (ret == ESP_OK || ret == ESP_ERR_INVALID_STATE) {
        s_rx_enabled = false;
        if (reason) {
            ESP_LOGI(TAG, "%s", reason);
        }
        return ESP_OK;
    }
    ESP_LOGW(TAG, "RX disable failed: 0x%x", ret);
    return ret;
}

esp_err_t audio_pipeline_init(void)
{
    ESP_LOGI(TAG, "Initializing audio pipeline (TX=TDM, RX=TDM, 16-bit 4-slot)");

    ESP_ERROR_CHECK(es8311_init());
    ESP_ERROR_CHECK(es7210_init());

    // I2S 通道
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = 240;
    chan_cfg.auto_clear = true;

    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &s_tx_chan, &s_rx_chan));

    /* TX: TDM Philips 16-bit 4-slot — ES8311 DAC */
    i2s_tdm_config_t tx_tdm_cfg = {
        .clk_cfg = I2S_TDM_CLK_DEFAULT_CONFIG(AUDIO_SAMPLE_RATE),
        .slot_cfg = I2S_TDM_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO,
            I2S_TDM_SLOT0 | I2S_TDM_SLOT1 | I2S_TDM_SLOT2 | I2S_TDM_SLOT3),
        .gpio_cfg = {
            .mclk = I2S_PIN_MCLK,
            .bclk = I2S_PIN_SCLK,
            .ws   = I2S_PIN_LRCK,
            .dout = I2S_PIN_DOUT,
            .din  = I2S_GPIO_UNUSED,
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };
    tx_tdm_cfg.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;

    /* RX: TDM Philips 16-bit 4-slot — ES7210 ADC (driver auto-truncates 24→16) */
    i2s_tdm_config_t rx_tdm_cfg = {
        .clk_cfg = I2S_TDM_CLK_DEFAULT_CONFIG(AUDIO_SAMPLE_RATE),
        .slot_cfg = I2S_TDM_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO,
            I2S_TDM_SLOT0 | I2S_TDM_SLOT1 | I2S_TDM_SLOT2 | I2S_TDM_SLOT3),
        .gpio_cfg = {
            .mclk = I2S_PIN_MCLK,
            .bclk = I2S_PIN_SCLK,
            .ws   = I2S_PIN_LRCK,
            .dout = I2S_GPIO_UNUSED,
            .din  = I2S_PIN_DIN,
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };
    rx_tdm_cfg.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;

    ESP_ERROR_CHECK(i2s_channel_init_tdm_mode(s_tx_chan, &tx_tdm_cfg));
    ESP_ERROR_CHECK(i2s_channel_init_tdm_mode(s_rx_chan, &rx_tdm_cfg));

    s_play_mutex = xSemaphoreCreateMutex();
    s_record_buf = heap_caps_malloc(RECORD_BUF_SIZE, MALLOC_CAP_SPIRAM);

    ESP_LOGI(TAG, "Audio pipeline ready (TDM Philips 16-bit 4-slot)");
    return ESP_OK;
}

void audio_pipeline_loop(void)
{
    /* 播放处理: 16-bit mono PCM → TDM 16-bit 4-slot */
    if (s_play_head) {
        if (xSemaphoreTake(s_play_mutex, 0) == pdTRUE) {
            play_chunk_t *chunk = s_play_head;
            if (!chunk) {
                xSemaphoreGive(s_play_mutex);
                goto record_phase;
            }
            if (!s_tx_enabled) {
                esp_err_t tx_ret = ensure_tx_enabled(NULL);
                if (tx_ret != ESP_OK) {
                    xSemaphoreGive(s_play_mutex);
                    goto record_phase;
                }
            }
            if (s_output_muted) {
                esp_err_t unmute_ret = es8311_mute(false);
                if (unmute_ret != ESP_OK) {
                    ESP_LOGW(TAG, "Output unmute failed: 0x%x", unmute_ret);
                    xSemaphoreGive(s_play_mutex);
                    goto record_phase;
                }
                s_output_muted = false;
            }

            #define SAMPLES_PER_CHUNK 960
            /* TDM 4-slot 16-bit: 每个源样本展开为 4 个 int16_t */
            static int16_t out_buf[SAMPLES_PER_CHUNK * 4];

            size_t src_chunk = SAMPLES_PER_CHUNK * 2;  // bytes
            if (chunk->pos + src_chunk > chunk->len) {
                src_chunk = chunk->len - chunk->pos;
            }

            size_t num_samples = src_chunk / 2;
            const int16_t *src = (const int16_t *)(chunk->data + chunk->pos);

            for (size_t i = 0; i < num_samples; i++) {
                int16_t val = src[i];
                out_buf[i * 4]     = val;   // SLOT0 (Left)
                out_buf[i * 4 + 1] = val;   // SLOT1 (Right)
                out_buf[i * 4 + 2] = 0;     // SLOT2
                out_buf[i * 4 + 3] = 0;     // SLOT3
            }

            size_t written = 0;
            size_t out_bytes = num_samples * 4 * sizeof(int16_t);
            int64_t write_started_us = esp_timer_get_time();
            esp_err_t write_ret = i2s_channel_write(s_tx_chan, out_buf, out_bytes, &written, 100);
            int64_t write_ms = (esp_timer_get_time() - write_started_us) / 1000;
            if (write_ret != ESP_OK || written != out_bytes || write_ms > 75) {
                ESP_LOGW(TAG, "I2S playback write ret=0x%x written=%u/%u elapsed_ms=%lld queued=%u",
                         write_ret,
                         (unsigned)written,
                         (unsigned)out_bytes,
                         (long long)write_ms,
                         (unsigned)s_play_queued_bytes);
            }

            /* written 是输出字节; 每个源样本 = 4 slots × 2 bytes = 8 bytes */
            size_t samples_written = written / (4 * sizeof(int16_t));
            size_t consumed_src_bytes = samples_written * 2;
            chunk->pos += consumed_src_bytes;
            if (consumed_src_bytes > 0) {
                if (chunk->source != AUDIO_PLAYBACK_SOURCE_MUSIC &&
                    !s_debug_tts_first_write_logged &&
                    s_debug_tts_turn_id != 0) {
                    int64_t now_ms = esp_timer_get_time() / 1000;
                    s_debug_tts_first_write_logged = true;
                    ESP_LOGI(TAG, "VOICE_TIMING speaker_first_write turn=%u since_turn_start_ms=%lld since_pcm_queued_ms=%lld bytes=%u",
                             (unsigned)s_debug_tts_turn_id,
                             (long long)(s_debug_tts_turn_started_at_ms > 0
                                ? now_ms - s_debug_tts_turn_started_at_ms
                                : 0),
                             (long long)(s_debug_tts_pcm_queued_at_ms > 0
                                ? now_ms - s_debug_tts_pcm_queued_at_ms
                                : 0),
                             (unsigned)consumed_src_bytes);
                }
                s_play_queued_bytes = (consumed_src_bytes >= s_play_queued_bytes)
                    ? 0 : (s_play_queued_bytes - consumed_src_bytes);
                if (chunk->source == AUDIO_PLAYBACK_SOURCE_MUSIC) {
                    s_play_music_queued_bytes = (consumed_src_bytes >= s_play_music_queued_bytes)
                        ? 0 : (s_play_music_queued_bytes - consumed_src_bytes);
                }
            }

            if (chunk->pos >= chunk->len) {
                s_play_head = chunk->next;
                if (!s_play_head) {
                    s_play_tail = NULL;
                }
                free_play_chunk(chunk);

                if (!s_play_head) {
                    int16_t silence[128] = {0};
                    size_t dummy = 0;
                    i2s_channel_write(s_tx_chan, silence, sizeof(silence), &dummy, 50);
                    maybe_disable_tx_locked("Playback finished, TX disabled");
                } else {
                    ESP_LOGD(TAG, "Playback chunk finished, continuing next chunk");
                }
            }
            xSemaphoreGive(s_play_mutex);
        }
    }

record_phase:
    /* 录音处理（仅批量模式） */
    if (s_recording && !s_streaming && s_record_buf) {
        size_t read = 0;
        size_t remain = RECORD_BUF_SIZE - s_record_pos;
        if (remain > 0) {
            size_t chunk = (remain > 1024) ? 1024 : remain;
            i2s_channel_read(s_rx_chan, s_record_buf + s_record_pos, chunk, &read, 100);
            s_record_pos += read;
        }
    }
}

esp_err_t audio_play_pcm(const uint8_t *data, size_t len)
{
    xSemaphoreTake(s_play_mutex, portMAX_DELAY);
    clear_playback_queue_locked();
    esp_err_t ret = enqueue_playback_locked(data, len, false, AUDIO_PLAYBACK_SOURCE_DEFAULT);
    xSemaphoreGive(s_play_mutex);
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "Playing %zu bytes", len);
    } else {
        ESP_LOGE(TAG, "Failed to queue playback: %d", ret);
    }
    return ret;
}

esp_err_t audio_play_pcm_copy_source(const uint8_t *data, size_t len, audio_playback_source_t source)
{
    uint8_t *copy = heap_caps_malloc(len, MALLOC_CAP_SPIRAM);
    if (!copy) {
        ESP_LOGE(TAG, "Failed to alloc owned playback buffer: %zu bytes", len);
        return ESP_ERR_NO_MEM;
    }
    memcpy(copy, data, len);

    xSemaphoreTake(s_play_mutex, portMAX_DELAY);
    clear_playback_queue_locked();
    esp_err_t ret = enqueue_playback_locked(copy, len, true, source);
    xSemaphoreGive(s_play_mutex);
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "Playing copied PCM %zu bytes", len);
    } else {
        ESP_LOGE(TAG, "Failed to queue copied playback: %d", ret);
    }
    return ret;
}

esp_err_t audio_play_pcm_copy(const uint8_t *data, size_t len)
{
    return audio_play_pcm_copy_source(data, len, AUDIO_PLAYBACK_SOURCE_DEFAULT);
}

esp_err_t audio_queue_pcm_copy_source(const uint8_t *data, size_t len, audio_playback_source_t source)
{
    uint8_t *copy = heap_caps_malloc(len, MALLOC_CAP_SPIRAM);
    if (!copy) {
        ESP_LOGE(TAG, "Failed to alloc queued playback buffer: %zu bytes", len);
        return ESP_ERR_NO_MEM;
    }
    memcpy(copy, data, len);

    xSemaphoreTake(s_play_mutex, portMAX_DELAY);
    esp_err_t ret = enqueue_playback_locked(copy, len, true, source);
    xSemaphoreGive(s_play_mutex);
    if (ret == ESP_OK) {
        ESP_LOGD(TAG, "Queued PCM chunk %zu bytes", len);
    } else {
        ESP_LOGE(TAG, "Failed to append PCM chunk: %d", ret);
    }
    return ret;
}

esp_err_t audio_queue_pcm_copy(const uint8_t *data, size_t len)
{
    return audio_queue_pcm_copy_source(data, len, AUDIO_PLAYBACK_SOURCE_DEFAULT);
}

esp_err_t audio_queue_pcm_copy_tail_source(const uint8_t *data, size_t len, audio_playback_source_t source)
{
    if (!data || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    size_t silence_bytes = PLAYBACK_TAIL_SILENCE_SAMPLES * sizeof(int16_t);
    size_t copy_len = len + silence_bytes;
    uint8_t *copy = heap_caps_malloc(copy_len, MALLOC_CAP_SPIRAM);
    if (!copy) {
        ESP_LOGE(TAG, "Failed to alloc tail playback buffer: %zu bytes", copy_len);
        return ESP_ERR_NO_MEM;
    }

    memcpy(copy, data, len);
    apply_pcm_tail_fade(copy, len);
    memset(copy + len, 0, silence_bytes);

    xSemaphoreTake(s_play_mutex, portMAX_DELAY);
    esp_err_t ret = enqueue_playback_locked(copy, copy_len, true, source);
    xSemaphoreGive(s_play_mutex);
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "Queued tail-smoothed PCM chunk %zu + %zu bytes", len, silence_bytes);
    } else {
        ESP_LOGE(TAG, "Failed to append tail-smoothed PCM chunk: %d", ret);
    }
    return ret;
}

void audio_stop_playback(void)
{
    xSemaphoreTake(s_play_mutex, portMAX_DELAY);
    clear_playback_queue_locked();
    maybe_disable_tx_locked("Playback stopped, TX disabled");
    xSemaphoreGive(s_play_mutex);
}

esp_err_t audio_record_start(void)
{
    s_record_pos = 0;
    s_recording = true;
    s_streaming = false;
    esp_err_t ret = ensure_rx_enabled("Recording started (batch mode)");
    return ret;
}

/* ── 后台 TX 静音喂送任务 (保持 MCLK 输出) ──────── */
static TaskHandle_t s_tx_silence_task = NULL;
static bool s_tx_silence_running = false;

static void tx_silence_task(void *arg)
{
    int16_t silence[512] = {0};  // 512 × 2 = 1024 bytes per write
    size_t written;
    while (s_tx_silence_running) {
        i2s_channel_write(s_tx_chan, silence, sizeof(silence), &written, pdMS_TO_TICKS(100));
    }
    ESP_LOGI(TAG, "TX silence task exiting");
    s_tx_silence_task = NULL;
    vTaskDelete(NULL);
}

static void start_tx_silence(void)
{
    if (s_tx_silence_task != NULL) return;
    s_tx_silence_running = true;
    xTaskCreate(tx_silence_task, "tx_silence", 4096, NULL, 4, &s_tx_silence_task);
    ESP_LOGI(TAG, "TX silence task started (MCLK active)");
}

static void stop_tx_silence(void)
{
    if (s_tx_silence_task == NULL) return;
    s_tx_silence_running = false;
    for (int i = 0; i < 20 && s_tx_silence_task != NULL; i++) {
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    ESP_LOGI(TAG, "TX silence task stopped");
}

esp_err_t audio_record_start_stream(void)
{
    s_record_pos = 0;
    s_recording = true;
    s_streaming = true;

    esp_err_t tx_ret = ensure_tx_enabled("TX enabled for clock generation");
    if (tx_ret != ESP_OK) {
        s_recording = false;
        s_streaming = false;
        return tx_ret;
    }

    start_tx_silence();
    vTaskDelay(pdMS_TO_TICKS(8));

    esp_err_t rx_ret = ensure_rx_enabled(NULL);
    if (rx_ret != ESP_OK) {
        stop_tx_silence();
        s_recording = false;
        s_streaming = false;
        return rx_ret;
    }
    ESP_LOGI(TAG, "Recording started (stream mode, MCLK active)");
    return ESP_OK;
}

esp_err_t audio_record_stop(uint8_t **out_data, size_t *out_len)
{
    s_recording = false;
    ensure_rx_disabled(NULL);
    *out_data = s_record_buf;
    *out_len = s_record_pos;
    ESP_LOGI(TAG, "Recording stopped: %zu bytes", s_record_pos);
    return ESP_OK;
}

/* ── 流式录音: 直接 I2S 读取 (TDM 32-bit 4-slot) ────── */
esp_err_t audio_record_read(uint8_t *buf, size_t buf_size,
                             size_t *bytes_read, TickType_t timeout)
{
    if (!s_rx_chan) return ESP_ERR_INVALID_STATE;
    s_streaming = true;
    return i2s_channel_read(s_rx_chan, buf, buf_size, bytes_read, timeout);
}

esp_err_t audio_record_stop_stream(void)
{
    s_streaming = false;
    s_recording = false;

    stop_tx_silence();

    ensure_rx_disabled(NULL);
    xSemaphoreTake(s_play_mutex, portMAX_DELAY);
    maybe_disable_tx_locked("TX disabled (no playback)");
    xSemaphoreGive(s_play_mutex);
    ESP_LOGI(TAG, "Streaming recording stopped");
    return ESP_OK;
}

bool audio_is_playing(void)
{
    return s_play_queued_bytes > 0;
}

bool audio_is_non_music_playing(void)
{
    return s_play_queued_bytes > s_play_music_queued_bytes;
}

size_t audio_get_playback_queued_bytes(void)
{
    size_t queued = 0;
    if (!s_play_mutex) {
        return 0;
    }
    if (xSemaphoreTake(s_play_mutex, pdMS_TO_TICKS(20)) == pdTRUE) {
        queued = s_play_queued_bytes;
        xSemaphoreGive(s_play_mutex);
    }
    return queued;
}

void audio_debug_mark_tts_turn(uint32_t turn_id, int64_t turn_started_at_ms, int64_t pcm_queued_at_ms)
{
    s_debug_tts_turn_id = turn_id;
    s_debug_tts_turn_started_at_ms = turn_started_at_ms;
    s_debug_tts_pcm_queued_at_ms = pcm_queued_at_ms;
    s_debug_tts_first_write_logged = false;
}

/* ── 唤醒词检测用 I2S 接口 ────────────────── */
esp_err_t audio_i2s_keep_alive(void)
{
    if (!s_tx_chan || !s_rx_chan) return ESP_ERR_INVALID_STATE;

    s_keep_alive = true;

    esp_err_t tx_ret = ensure_tx_enabled("TX enabled for wake word clock");
    if (tx_ret != ESP_OK) return tx_ret;

    esp_err_t rx_dis = ensure_rx_disabled(NULL);
    vTaskDelay(pdMS_TO_TICKS(10));
    esp_err_t rx_en = ensure_rx_enabled(NULL);
    ESP_LOGI(TAG, "RX reset: disable=0x%x, enable=0x%x", rx_dis, rx_en);
    if (rx_en != ESP_OK) return rx_en;

    int16_t silence[512] = {0};
    size_t written = 0;
    for (int i = 0; i < 5; i++) {
        i2s_channel_write(s_tx_chan, silence, sizeof(silence), &written, pdMS_TO_TICKS(50));
    }
    ESP_LOGI(TAG, "TX primed with silence");

    es7210_init();

    ESP_LOGI(TAG, "I2S keep-alive for wake word (re-init ES7210)");
    return ESP_OK;
}

esp_err_t audio_i2s_read_wake(uint8_t *buf, size_t buf_size,
                               size_t *bytes_read, TickType_t timeout)
{
    if (!s_rx_chan) return ESP_ERR_INVALID_STATE;
    return i2s_channel_read(s_rx_chan, buf, buf_size, bytes_read, timeout);
}
