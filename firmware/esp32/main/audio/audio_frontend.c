#include "audio_frontend.h"

#include "esp_afe_config.h"
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include <string.h>

static const char *TAG = "afe";

static const esp_afe_sr_iface_t *s_afe_iface = &ESP_AFE_SR_HANDLE;
static esp_afe_sr_data_t *s_afe_data = NULL;
static int16_t *s_feed_buf = NULL;
static int s_feed_chunksize = 0;
static int s_fetch_chunksize = 0;
static size_t s_feed_fill = 0;

esp_err_t audio_frontend_init(void)
{
    if (s_afe_data) {
        return ESP_OK;
    }

    afe_config_t afe_config = AFE_CONFIG_DEFAULT();
    afe_config.aec_init = false;
    afe_config.se_init = true;
    afe_config.vad_init = true;
    afe_config.wakenet_init = false;
    afe_config.voice_communication_init = false;
    afe_config.voice_communication_agc_init = false;
    afe_config.afe_mode = SR_MODE_HIGH_PERF;
    afe_config.afe_ringbuf_size = 8;
    afe_config.memory_alloc_mode = AFE_MEMORY_ALLOC_INTERNAL_PSRAM_BALANCE;
    afe_config.pcm_config.total_ch_num = 1;
    afe_config.pcm_config.mic_num = 1;
    afe_config.pcm_config.ref_num = 0;
    afe_config.pcm_config.sample_rate = 16000;

    s_afe_data = s_afe_iface->create_from_config(&afe_config);
    if (!s_afe_data) {
        ESP_LOGE(TAG, "Failed to create AFE handle");
        return ESP_FAIL;
    }

    s_feed_chunksize = s_afe_iface->get_feed_chunksize(s_afe_data);
    s_fetch_chunksize = s_afe_iface->get_fetch_chunksize(s_afe_data);
    if (s_feed_chunksize <= 0 || s_fetch_chunksize <= 0) {
        ESP_LOGE(TAG, "Invalid AFE chunk sizes: feed=%d fetch=%d",
                 s_feed_chunksize, s_fetch_chunksize);
        s_afe_iface->destroy(s_afe_data);
        s_afe_data = NULL;
        return ESP_FAIL;
    }

    s_feed_buf = heap_caps_malloc(
        s_feed_chunksize * sizeof(int16_t), MALLOC_CAP_INTERNAL);
    if (!s_feed_buf) {
        ESP_LOGE(TAG, "Failed to allocate AFE feed buffer");
        s_afe_iface->destroy(s_afe_data);
        s_afe_data = NULL;
        return ESP_ERR_NO_MEM;
    }

    s_feed_fill = 0;
    ESP_LOGI(TAG, "AFE ready: feed=%d fetch=%d", s_feed_chunksize, s_fetch_chunksize);
    return ESP_OK;
}

void audio_frontend_reset(void)
{
    if (!s_afe_data) {
        return;
    }
    s_afe_iface->reset_buffer(s_afe_data);
    s_feed_fill = 0;
}

size_t audio_frontend_process(const int16_t *input, size_t input_samples,
                              int16_t *output, size_t output_capacity,
                              audio_frontend_diag_t *diag)
{
    size_t out_count = 0;

    if (diag) {
        memset(diag, 0, sizeof(*diag));
    }

    if (!s_afe_data || !s_feed_buf || !input || !output || output_capacity == 0) {
        return 0;
    }

    for (size_t i = 0; i < input_samples; i++) {
        s_feed_buf[s_feed_fill++] = input[i];
        if (s_feed_fill < (size_t)s_feed_chunksize) {
            continue;
        }

        s_afe_iface->feed(s_afe_data, s_feed_buf);
        s_feed_fill = 0;
        if (diag) {
            diag->fed_chunks++;
        }

        afe_fetch_result_t *result = s_afe_iface->fetch(s_afe_data);
        if (!result || !result->data) {
            continue;
        }

        size_t samples = (size_t)result->data_size / sizeof(int16_t);
        if (samples > output_capacity - out_count) {
            samples = output_capacity - out_count;
        }
        if (samples > 0) {
            memcpy(output + out_count, result->data, samples * sizeof(int16_t));
            out_count += samples;
        }

        if (diag) {
            diag->fetched_chunks++;
            if (result->vad_state == AFE_VAD_SPEECH) {
                diag->vad_speech = true;
            }
        }

        if (out_count >= output_capacity) {
            break;
        }
    }

    return out_count;
}
