#include "wake_word.h"
#include "esp_log.h"
#include "esp_mn_iface.h"
#include "esp_mn_models.h"
#include "esp_mn_speech_commands.h"
#include "esp_process_sdkconfig.h"
#include "model_path.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include <math.h>
#include <string.h>

static const char *TAG = "wake_word";

static esp_mn_iface_t *s_multinet = NULL;
static model_iface_data_t *s_model_data = NULL;
static volatile bool s_detected = false;
static volatile bool s_running = false;

#define WAKE_WORD_COMMAND_ID 1
#define WAKE_WORD_COMMAND_TEXT "dou dou"
#define WAKE_WORD_DECOY_COMMAND_ID_BASE 100
#define WAKE_WORD_ENGINE_THRESHOLD 0.76f
#define WAKE_WORD_ACCEPT_PROB 0.76f
#define WAKE_WORD_HIGH_CONF_PROB 0.90f
#define WAKE_WORD_STRONG_PEAK_PROB 0.85f

/*
 * MultiNet is a command recognizer, not a dedicated always-on wake-word model.
 * Running it with only one command in IDLE can force-match nearby loud speech
 * to "dou dou". Keep guarded model thresholds, add negative classes, then pass
 * every "dou dou" candidate through this acoustic shape gate.
 */
#define WAKE_WORD_VOICE_RMS_THRESHOLD       18
#define WAKE_WORD_MIN_PEAK_RMS              20
#define WAKE_WORD_STRONG_PEAK_RMS           2000
#define WAKE_WORD_MIN_VOICE_RUN_MS          320
#define WAKE_WORD_HIGH_CONF_MIN_VOICE_MS    96
#define WAKE_WORD_MAX_VOICE_RUN_MS          1450
#define WAKE_WORD_MIN_PRE_QUIET_MS          160
#define WAKE_WORD_SPEECH_GAP_RESET_MS       220
#define WAKE_WORD_ACCEPT_COOLDOWN_MS        2500

static const char *const s_decoy_commands[] = {
    "da kai kong tiao",
    "guan bi kong tiao",
    "zeng da feng su",
    "jian xiao feng su",
    "kai shi bo fang",
    "zan ting bo fang",
    "da kai dian deng",
    "guan bi dian deng",
    "ni hao xiao zhi",
    "xiao ai tong xue",
    "xiao du xiao du",
    "xiao yi xiao yi",
    "tian mao jing ling",
    "hai le xin",
};

/* ── 环形缓冲区: 存最近 ~2 秒的 PCM (16kHz mono) ── */
#define RING_BUF_SECONDS    2
#define RING_BUF_SAMPLES    (16000 * RING_BUF_SECONDS)  /* 32000 samples = 64KB */
static int16_t *s_ring_buf = NULL;
static int s_ring_write_pos = 0;     /* 下一个写入位置 */
static int s_ring_total_written = 0; /* 总共写入的样本数 (用于判断 buffer 是否已满) */
static int s_ring_detect_pos = 0;    /* 检测到唤醒词时的写入位置 */
static int64_t s_last_accept_ms = 0;
static int s_voice_run_ms = 0;
static int s_voice_gap_ms = 0;
static int s_quiet_run_ms = 0;
static int s_pre_voice_quiet_ms = 0;
static int s_voice_peak_rms = 0;

static int wake_word_pcm_rms(const int16_t *pcm, int samples)
{
    if (!pcm || samples <= 0) {
        return 0;
    }
    int64_t energy = 0;
    for (int i = 0; i < samples; i++) {
        energy += (int64_t)pcm[i] * pcm[i];
    }
    return (int)sqrt((double)energy / samples);
}

static void wake_word_reset_gate_state(void)
{
    s_voice_run_ms = 0;
    s_voice_gap_ms = 0;
    s_quiet_run_ms = 0;
    s_pre_voice_quiet_ms = 0;
    s_voice_peak_rms = 0;
}

static void wake_word_update_gate_state(int rms, int chunk_ms)
{
    if (chunk_ms <= 0) {
        chunk_ms = 1;
    }

    if (rms >= WAKE_WORD_VOICE_RMS_THRESHOLD) {
        if (s_voice_run_ms == 0) {
            s_pre_voice_quiet_ms = s_quiet_run_ms;
            s_voice_peak_rms = rms;
        } else if (rms > s_voice_peak_rms) {
            s_voice_peak_rms = rms;
        }
        s_voice_run_ms += chunk_ms + s_voice_gap_ms;
        s_voice_gap_ms = 0;
        s_quiet_run_ms = 0;
        return;
    }

    s_quiet_run_ms += chunk_ms;
    if (s_quiet_run_ms > 2000) {
        s_quiet_run_ms = 2000;
    }

    if (s_voice_run_ms > 0) {
        s_voice_gap_ms += chunk_ms;
        if (s_voice_gap_ms > WAKE_WORD_SPEECH_GAP_RESET_MS) {
            s_voice_run_ms = 0;
            s_voice_gap_ms = 0;
            s_pre_voice_quiet_ms = s_quiet_run_ms;
            s_voice_peak_rms = 0;
        }
    }
}

static bool wake_word_gate_accepts(float prob, const char **reason)
{
    int64_t now_ms = esp_timer_get_time() / 1000;

    if (s_last_accept_ms > 0 && now_ms - s_last_accept_ms < WAKE_WORD_ACCEPT_COOLDOWN_MS) {
        *reason = "cooldown";
        return false;
    }
    if (prob < WAKE_WORD_ACCEPT_PROB) {
        *reason = "low_prob";
        return false;
    }
    if (prob >= WAKE_WORD_HIGH_CONF_PROB &&
        s_voice_peak_rms >= WAKE_WORD_MIN_PEAK_RMS &&
        s_voice_run_ms >= WAKE_WORD_HIGH_CONF_MIN_VOICE_MS) {
        s_last_accept_ms = now_ms;
        *reason = "accepted_high_conf";
        return true;
    }
    if (prob >= WAKE_WORD_STRONG_PEAK_PROB &&
        s_voice_peak_rms >= WAKE_WORD_STRONG_PEAK_RMS) {
        s_last_accept_ms = now_ms;
        *reason = "accepted_strong_peak";
        return true;
    }
    if (s_voice_peak_rms < WAKE_WORD_MIN_PEAK_RMS) {
        *reason = "low_peak";
        return false;
    }
    if (s_voice_run_ms < WAKE_WORD_MIN_VOICE_RUN_MS) {
        *reason = "too_short";
        return false;
    }
    if (s_voice_run_ms > WAKE_WORD_MAX_VOICE_RUN_MS) {
        *reason = "continuous_speech";
        return false;
    }
    if (s_pre_voice_quiet_ms < WAKE_WORD_MIN_PRE_QUIET_MS) {
        *reason = "no_pre_quiet";
        return false;
    }

    s_last_accept_ms = now_ms;
    *reason = "accepted";
    return true;
}

static esp_err_t wake_word_register_commands(void)
{
    esp_err_t ret = esp_mn_commands_alloc(s_multinet, s_model_data);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to alloc MultiNet commands: 0x%x", ret);
        return ret;
    }

    ret = esp_mn_commands_add(WAKE_WORD_COMMAND_ID, WAKE_WORD_COMMAND_TEXT);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to add wake command '%s': 0x%x", WAKE_WORD_COMMAND_TEXT, ret);
        return ret;
    }

    int decoys_added = 0;
    int decoys_total = sizeof(s_decoy_commands) / sizeof(s_decoy_commands[0]);
    for (int i = 0; i < decoys_total; i++) {
        ret = esp_mn_commands_add(WAKE_WORD_DECOY_COMMAND_ID_BASE + i, (char *)s_decoy_commands[i]);
        if (ret == ESP_OK) {
            decoys_added++;
        } else {
            ESP_LOGW(TAG, "Skipping decoy command '%s': 0x%x", s_decoy_commands[i], ret);
        }
    }

    esp_mn_error_t *errors = esp_mn_commands_update();
    if (errors && errors->num > 0) {
        ESP_LOGW(TAG, "MultiNet reported %d command update errors", errors->num);
    }

    ESP_LOGI(TAG, "Wake command '%s' registered with %d/%d decoy commands",
             WAKE_WORD_COMMAND_TEXT, decoys_added, decoys_total);
    return ESP_OK;
}

esp_err_t wake_word_init(void)
{
    /* 从 model 分区加载模型 */
    srmodel_list_t *models = esp_srmodel_init("model");
    if (!models || models->num == 0) {
        ESP_LOGE(TAG, "Failed to load SR models from partition");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "Loaded %d SR models", models->num);

    /* 找中文 MultiNet 模型 */
    char *mn_name = esp_srmodel_filter(models, ESP_MN_PREFIX, "cn");
    if (!mn_name) {
        ESP_LOGW(TAG, "No CN MultiNet, trying any language");
        mn_name = esp_srmodel_filter(models, ESP_MN_PREFIX, NULL);
    }
    if (!mn_name) {
        ESP_LOGE(TAG, "No MultiNet model found");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "Using MultiNet: %s", mn_name);

    s_multinet = esp_mn_handle_from_name(mn_name);
    if (!s_multinet) {
        ESP_LOGE(TAG, "Failed to get MultiNet handle for %s", mn_name);
        return ESP_FAIL;
    }

    /* 创建模型实例, 6 秒超时 */
    s_model_data = s_multinet->create(mn_name, 6000);
    if (!s_model_data) {
        ESP_LOGE(TAG, "Failed to create MultiNet instance");
        return ESP_FAIL;
    }

    /* 使用较高阈值，配合负类和声学门，优先降低环境说话误触发。 */
    s_multinet->set_det_threshold(s_model_data, WAKE_WORD_ENGINE_THRESHOLD);

    /* 注册 "豆豆" 和一组负类命令，避免单命令强制匹配。 */
    if (wake_word_register_commands() != ESP_OK) {
        return ESP_FAIL;
    }

    int feed_size = s_multinet->get_samp_chunksize(s_model_data);
    ESP_LOGI(TAG, "Wake word '豆豆' ready, feed chunk: %d samples, threshold=%.2f/%.2f",
             feed_size, WAKE_WORD_ENGINE_THRESHOLD, WAKE_WORD_ACCEPT_PROB);

    /* 分配环形缓冲区 (PSRAM) */
    s_ring_buf = heap_caps_malloc(RING_BUF_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (!s_ring_buf) {
        ESP_LOGE(TAG, "Failed to alloc ring buffer (%d bytes)", RING_BUF_SAMPLES * 2);
        /* 不致命，唤醒词检测仍可用，只是没有残留音频 */
    } else {
        memset(s_ring_buf, 0, RING_BUF_SAMPLES * sizeof(int16_t));
        ESP_LOGI(TAG, "Ring buffer: %d samples (%d sec)", RING_BUF_SAMPLES, RING_BUF_SECONDS);
    }

    s_running = true;
    wake_word_reset_gate_state();
    return ESP_OK;
}

void wake_word_feed(const int16_t *pcm, int samples)
{
    if (!s_multinet || !s_model_data || !s_running) {
        return;
    }

    /* 存入环形缓冲区 */
    if (s_ring_buf && samples > 0) {
        for (int i = 0; i < samples; i++) {
            s_ring_buf[s_ring_write_pos] = pcm[i];
            s_ring_write_pos = (s_ring_write_pos + 1) % RING_BUF_SAMPLES;
        }
        s_ring_total_written += samples;
    }

    static int feed_count = 0;
    feed_count++;
    if (feed_count % 100 == 1) {
        ESP_LOGI(TAG, "feed #%d, %d samples, first=[%d,%d,%d]",
                 feed_count, samples, pcm[0], pcm[1], pcm[2]);
    }

    int chunk_ms = (samples * 1000 + 16000 - 1) / 16000;
    int rms = wake_word_pcm_rms(pcm, samples);
    wake_word_update_gate_state(rms, chunk_ms);

    esp_mn_state_t state = s_multinet->detect(s_model_data, (int16_t *)pcm);

    if (state == ESP_MN_STATE_DETECTED) {
        esp_mn_results_t *result = s_multinet->get_results(s_model_data);
        if (result && result->num > 0) {
            int command_id = result->command_id[0];
            int phrase_id = result->phrase_id[0];
            float prob = result->prob[0];
            const char *recognized = result->string[0] ? result->string : "";
            const char *gate_reason = "not_wake_command";
            if (command_id == WAKE_WORD_COMMAND_ID && wake_word_gate_accepts(prob, &gate_reason)) {
                ESP_LOGI(TAG, "*** WAKE WORD DETECTED! id=%d phrase=%d prob=%.3f rms=%d peak=%d voice=%dms pre_quiet=%dms text='%s' ***",
                         command_id, phrase_id, prob, rms, s_voice_peak_rms, s_voice_run_ms,
                         s_pre_voice_quiet_ms, recognized);
                /* 记录检测时刻的写入位置 */
                s_ring_detect_pos = s_ring_write_pos;
                s_detected = true;
            } else {
                ESP_LOGI(TAG, "Wake word rejected: id=%d phrase=%d prob=%.3f rms=%d peak=%d voice=%dms pre_quiet=%dms reason=%s text='%s'",
                         command_id, phrase_id, prob, rms, s_voice_peak_rms, s_voice_run_ms,
                         s_pre_voice_quiet_ms, gate_reason, recognized);
            }
        }
        s_multinet->clean(s_model_data);
        wake_word_reset_gate_state();
    } else if (state == ESP_MN_STATE_TIMEOUT) {
        s_multinet->clean(s_model_data);
        wake_word_reset_gate_state();
    }
}

bool wake_word_detected(void)
{
    if (s_detected) {
        s_detected = false;
        return true;
    }
    return false;
}

void wake_word_start(void)
{
    s_running = true;
    if (s_multinet && s_model_data) {
        s_multinet->clean(s_model_data);
    }
    /* 重置 ring buffer 状态 */
    s_ring_write_pos = 0;
    s_ring_total_written = 0;
    wake_word_reset_gate_state();
    ESP_LOGI(TAG, "Wake word detection started");
}

void wake_word_stop(void)
{
    s_running = false;
    ESP_LOGI(TAG, "Wake word detection stopped");
}

int wake_word_get_feed_size(void)
{
    if (!s_multinet || !s_model_data) {
        return 512;  /* 安全默认值 */
    }
    return s_multinet->get_samp_chunksize(s_model_data);
}

int wake_word_get_trailing_audio(int16_t *out_buf, int max_samples)
{
    if (!s_ring_buf || s_ring_total_written == 0) {
        return 0;
    }

    /*
     * 检测时刻 s_ring_detect_pos 是 "豆豆" 刚说完的位置。
     * 但 MultiNet detect() 有延迟——它在消耗完唤醒词的最后一个 chunk 后才返回检测结果，
     * 所以 s_ring_detect_pos 实际上已经是唤醒词结束后的位置。
     *
     * 我们不需要唤醒词本身的音频（服务器 ASR 不需要听到 "豆豆"），
     * 但检测到唤醒词之后、到我们调用此函数之间，可能还有几个 chunk 的新音频
     * 被 feed 了进来（因为 feed 是在检测 loop 里持续调用的）。
     *
     * 实际上：检测到后我们立刻跳出了 feed 循环（wake_word_detected() 返回 true 后 continue），
     * 所以 s_ring_detect_pos ≈ 当前 s_ring_write_pos。残留音频很少甚至没有。
     *
     * 但为了安全，还是把从 detect_pos 到 write_pos 之间的数据取出来。
     */
    int available;
    if (s_ring_write_pos >= s_ring_detect_pos) {
        available = s_ring_write_pos - s_ring_detect_pos;
    } else {
        available = RING_BUF_SAMPLES - s_ring_detect_pos + s_ring_write_pos;
    }

    if (available <= 0) {
        return 0;
    }
    if (available > max_samples) {
        /* 只取最后 max_samples */
        int skip = available - max_samples;
        s_ring_detect_pos = (s_ring_detect_pos + skip) % RING_BUF_SAMPLES;
        available = max_samples;
    }

    /* 从 ring buffer 拷贝 */
    int pos = s_ring_detect_pos;
    for (int i = 0; i < available; i++) {
        out_buf[i] = s_ring_buf[pos];
        pos = (pos + 1) % RING_BUF_SAMPLES;
    }

    ESP_LOGI(TAG, "Trailing audio: %d samples (%.1f ms)",
             available, available * 1000.0f / 16000);
    return available;
}

void wake_word_clear_ring_buffer(void)
{
    s_ring_write_pos = 0;
    s_ring_total_written = 0;
    s_ring_detect_pos = 0;
}
