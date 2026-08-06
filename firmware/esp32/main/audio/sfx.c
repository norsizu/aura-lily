/**
 * 音效播放实现 — 从 SD 卡加载 8-bit unsigned PCM → 转 16-bit signed → I2S 播放
 */
#include "sfx.h"
#include "audio_pipeline.h"
#include "aura_config.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include <stdio.h>
#include <string.h>

static const char *TAG = "sfx";

// 音效文件路径
static const char *SFX_FILES[] = {
    ASSETS_BASE_PATH "/sounds/startup.pcm",
    ASSETS_BASE_PATH "/sounds/error.pcm",
    ASSETS_BASE_PATH "/sounds/sent.pcm",
    ASSETS_BASE_PATH "/sounds/reply.pcm",
    ASSETS_BASE_PATH "/sounds/sleep.pcm",
    ASSETS_BASE_PATH "/sounds/listen_start.pcm",
    ASSETS_BASE_PATH "/sounds/cancel.pcm",
};

// 预加载的音效数据（8-bit → 转换为 16-bit）
typedef struct {
    uint8_t *data;      // 16-bit signed PCM
    size_t len;
    bool loaded;
} sfx_data_t;

static sfx_data_t s_sfx[SFX_MAX] = {0};
static bool s_playing = false;

esp_err_t sfx_init(void)
{
    ESP_LOGI(TAG, "Loading sound effects from SD...");

    for (int i = 0; i < SFX_MAX; i++) {
        FILE *f = fopen(SFX_FILES[i], "rb");
        if (!f) {
            ESP_LOGW(TAG, "SFX not found: %s", SFX_FILES[i]);
            continue;
        }

        // 获取文件大小
        fseek(f, 0, SEEK_END);
        size_t file_size = ftell(f);
        fseek(f, 0, SEEK_SET);

        // 8-bit PCM → 16-bit PCM (大小翻倍)
        size_t out_size = file_size * 2;
        uint8_t *raw = heap_caps_malloc(file_size, MALLOC_CAP_SPIRAM);
        uint8_t *pcm16 = heap_caps_malloc(out_size, MALLOC_CAP_SPIRAM);

        if (!raw || !pcm16) {
            ESP_LOGE(TAG, "Memory alloc failed for SFX %d", i);
            if (raw) heap_caps_free(raw);
            if (pcm16) heap_caps_free(pcm16);
            fclose(f);
            continue;
        }

        fread(raw, 1, file_size, f);
        fclose(f);

        // 转换: 8-bit unsigned (0x80 = silence) → 16-bit signed
        int16_t *out = (int16_t *)pcm16;
        for (size_t j = 0; j < file_size; j++) {
            out[j] = ((int16_t)raw[j] - 128) << 8;
        }

        heap_caps_free(raw);

        s_sfx[i].data = pcm16;
        s_sfx[i].len = out_size;
        s_sfx[i].loaded = true;

        ESP_LOGI(TAG, "Loaded SFX[%d]: %s (%zu bytes → %zu bytes 16-bit)",
                 i, SFX_FILES[i], file_size, out_size);
    }

    return ESP_OK;
}

esp_err_t sfx_play(sfx_type_t type)
{
    if (type >= SFX_MAX || !s_sfx[type].loaded) {
        ESP_LOGW(TAG, "SFX %d not available", type);
        return ESP_ERR_NOT_FOUND;
    }

    // 通过 audio_pipeline 播放
    return audio_play_pcm(s_sfx[type].data, s_sfx[type].len);
}

esp_err_t sfx_play_file(const char *path)
{
    if (!path || !path[0]) {
        return ESP_ERR_INVALID_ARG;
    }

    FILE *f = fopen(path, "rb");
    if (!f) {
        ESP_LOGW(TAG, "PCM file not found: %s", path);
        return ESP_ERR_NOT_FOUND;
    }

    fseek(f, 0, SEEK_END);
    size_t file_size = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (file_size == 0) {
        fclose(f);
        return ESP_ERR_INVALID_SIZE;
    }

    uint8_t *raw = heap_caps_malloc(file_size, MALLOC_CAP_SPIRAM);
    uint8_t *pcm16 = heap_caps_malloc(file_size * 2, MALLOC_CAP_SPIRAM);
    if (!raw || !pcm16) {
        ESP_LOGE(TAG, "Memory alloc failed for PCM file: %s", path);
        if (raw) heap_caps_free(raw);
        if (pcm16) heap_caps_free(pcm16);
        fclose(f);
        return ESP_ERR_NO_MEM;
    }

    size_t rd = fread(raw, 1, file_size, f);
    fclose(f);
    if (rd != file_size) {
        ESP_LOGW(TAG, "Short PCM read: %s (%zu/%zu)", path, rd, file_size);
    }

    int16_t *out = (int16_t *)pcm16;
    for (size_t j = 0; j < rd; j++) {
        out[j] = ((int16_t)raw[j] - 128) << 8;
    }
    heap_caps_free(raw);

    /*
     * audio_play_pcm_copy() takes ownership by copying into the playback queue,
     * so this temporary conversion buffer can be released immediately.
     */
    esp_err_t ret = audio_play_pcm_copy(pcm16, rd * 2);
    heap_caps_free(pcm16);
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "Playing PCM file: %s (%zu bytes -> %zu bytes 16-bit)",
                 path, rd, rd * 2);
    }
    return ret;
}

bool sfx_is_playing(void)
{
    return audio_is_playing();
}
