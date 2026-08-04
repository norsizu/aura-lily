#include "music_player.h"

#include <dirent.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "audio_pipeline.h"
#include "sd_card.h"
#include "network/ws_client.h"
#include "esp_audio_simple_player.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define MUSIC_ROOT_DIR "/sdcard/music"
#define MUSIC_MAX_TRACKS 32
#define MUSIC_MAX_PATH_LEN 256
#define MUSIC_QUEUE_WATERMARK_BYTES (128 * 1024)
#define MUSIC_TTS_WAIT_TIMEOUT_MS 8000

static const char *TAG = "music_player";

static esp_asp_handle_t s_player = NULL;
static SemaphoreHandle_t s_mutex = NULL;
static char s_tracks[MUSIC_MAX_TRACKS][MUSIC_MAX_PATH_LEN];
static size_t s_track_count = 0;
static int s_current_track = -1;
static bool s_running = false;
static bool s_stop_requested = false;
static bool s_pending_play = false;
static bool s_pending_auto_next = false;
static bool s_wait_for_tts_window = false;
static bool s_wait_observed_audio = false;
static int64_t s_pending_play_requested_ms = 0;
static bool s_paused = false;
static bool s_resume_after_interaction = false;

static int choose_random_track_locked(int avoid_index)
{
    if (s_track_count == 0) {
        return -1;
    }
    if (s_track_count == 1) {
        return 0;
    }

    uint32_t rnd = esp_random();
    int index = (int)(rnd % s_track_count);
    if (index == avoid_index) {
        index = (index + 1 + (int)((rnd >> 8) % (s_track_count - 1))) % (int)s_track_count;
    }
    return index;
}

static int compare_track_names(const void *lhs, const void *rhs)
{
    return strcasecmp((const char *)lhs, (const char *)rhs);
}

static esp_err_t rescan_tracks_locked(void)
{
    DIR *dir = opendir(MUSIC_ROOT_DIR);
    s_track_count = 0;
    if (!dir) {
        ESP_LOGW(TAG, "Music directory missing: %s", MUSIC_ROOT_DIR);
        return ESP_ERR_NOT_FOUND;
    }

    struct dirent *entry = NULL;
    while ((entry = readdir(dir)) != NULL && s_track_count < MUSIC_MAX_TRACKS) {
        if (entry->d_name[0] == '.') {
            continue;
        }
        const char *ext = strrchr(entry->d_name, '.');
        if (!ext || (strcasecmp(ext, ".mp3") != 0 && strcasecmp(ext, ".wav") != 0)) {
            continue;
        }
        int written = snprintf(
            s_tracks[s_track_count],
            sizeof(s_tracks[s_track_count]),
            "%s/%s",
            MUSIC_ROOT_DIR,
            entry->d_name
        );
        if (written < 0 || written >= (int)sizeof(s_tracks[s_track_count])) {
            ESP_LOGW(TAG, "Skipping long music path: %s", entry->d_name);
            continue;
        }
        s_track_count++;
    }
    closedir(dir);

    if (s_track_count > 1) {
        qsort(s_tracks, s_track_count, sizeof(s_tracks[0]), compare_track_names);
    }
    if (s_track_count == 0) {
        ESP_LOGW(TAG, "No playable files found under %s", MUSIC_ROOT_DIR);
        return ESP_ERR_NOT_FOUND;
    }
    if (s_current_track >= (int)s_track_count) {
        s_current_track = 0;
    }
    return ESP_OK;
}

static int music_out_callback(uint8_t *data, int data_size, void *ctx)
{
    (void)ctx;
    if (!data || data_size <= 0) {
        return 0;
    }

    while (!s_stop_requested && audio_get_playback_queued_bytes() >= MUSIC_QUEUE_WATERMARK_BYTES) {
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    if (s_stop_requested) {
        return 0;
    }

    esp_err_t ret = audio_queue_pcm_copy_source(
        data,
        (size_t)data_size,
        AUDIO_PLAYBACK_SOURCE_MUSIC
    );
    return (ret == ESP_OK) ? 0 : -1;
}

static int music_event_callback(esp_asp_event_pkt_t *event, void *ctx)
{
    (void)ctx;
    if (!event || !event->payload) {
        return 0;
    }

    if (event->type == ESP_ASP_EVENT_TYPE_MUSIC_INFO &&
        event->payload_size == sizeof(esp_asp_music_info_t)) {
        esp_asp_music_info_t info = {0};
        memcpy(&info, event->payload, sizeof(info));
        ESP_LOGI(
            TAG,
            "Track info rate=%d channels=%d bits=%d bitrate=%d",
            info.sample_rate,
            info.channels,
            info.bits,
            info.bitrate
        );
        return 0;
    }

    if (event->type == ESP_ASP_EVENT_TYPE_STATE &&
        event->payload_size == sizeof(esp_asp_state_t)) {
        esp_asp_state_t state = ESP_ASP_STATE_NONE;
        memcpy(&state, event->payload, sizeof(state));
        ESP_LOGI(TAG, "Simple player state: %s", esp_audio_simple_player_state_to_str(state));
        if (state == ESP_ASP_STATE_FINISHED) {
            s_running = false;
            s_paused = false;
            s_resume_after_interaction = false;
            s_pending_auto_next = !s_stop_requested;
        } else if (state == ESP_ASP_STATE_STOPPED || state == ESP_ASP_STATE_ERROR) {
            s_running = false;
            s_paused = false;
            s_resume_after_interaction = false;
            s_pending_auto_next = false;
        } else if (state == ESP_ASP_STATE_RUNNING) {
            s_running = true;
            s_paused = false;
        } else if (state == ESP_ASP_STATE_PAUSED) {
            s_running = false;
            s_paused = true;
        }
    }
    return 0;
}

static esp_err_t ensure_player_initialized(void)
{
    if (s_player) {
        return ESP_OK;
    }

    esp_asp_cfg_t cfg = {
        .out.cb = music_out_callback,
        .out.user_ctx = NULL,
        .task_prio = 5,
        .task_stack = 6 * 1024,
        /* 解码任务同样避开 core 0 的 Wi-Fi 驱动，防止连接期间供给中断 */
        .task_core = 1,
        /* 栈必须在内部 RAM：flash cache 短暂关闭时 PSRAM 栈不可访问，
         * 会造成卡顿甚至 cache_utils 断言重启 */
        .task_stack_in_ext = false,
    };

    esp_gmf_err_t err = esp_audio_simple_player_new(&cfg, &s_player);
    if (err != ESP_GMF_ERR_OK || !s_player) {
        ESP_LOGE(TAG, "Failed to create music player: %d", (int)err);
        s_player = NULL;
        return ESP_FAIL;
    }
    err = esp_audio_simple_player_set_event(s_player, music_event_callback, NULL);
    if (err != ESP_GMF_ERR_OK) {
        ESP_LOGE(TAG, "Failed to set music event callback: %d", (int)err);
        esp_audio_simple_player_destroy(s_player);
        s_player = NULL;
        return ESP_FAIL;
    }
    return ESP_OK;
}

static esp_err_t start_track_locked(int index)
{
    if (index < 0 || index >= (int)s_track_count) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!sd_card_is_mounted()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (ensure_player_initialized() != ESP_OK) {
        return ESP_FAIL;
    }

    char uri[MUSIC_MAX_PATH_LEN + 8];
    const char *track_path = s_tracks[index];
    if (strncmp(track_path, "/sdcard/", 8) == 0) {
        snprintf(uri, sizeof(uri), "file://sdcard/%s", track_path + 8);
    } else if (strncmp(track_path, "sdcard/", 7) == 0) {
        snprintf(uri, sizeof(uri), "file://%s", track_path);
    } else {
        ESP_LOGW(TAG, "Track path is outside sdcard mount, using raw path: %s", track_path);
        snprintf(uri, sizeof(uri), "file://%s", track_path);
    }
    s_stop_requested = false;
    s_pending_auto_next = false;
    esp_gmf_err_t err = esp_audio_simple_player_run(s_player, uri, NULL);
    if (err != ESP_GMF_ERR_OK) {
        ESP_LOGE(TAG, "Failed starting track %s: %d", uri, (int)err);
        return ESP_FAIL;
    }
    s_current_track = index;
    s_running = true;
    ESP_LOGI(TAG, "Playing local music: %s", s_tracks[index]);
    return ESP_OK;
}

static void stop_player_locked(void)
{
    s_pending_play = false;
    s_pending_auto_next = false;
    s_wait_for_tts_window = false;
    s_wait_observed_audio = false;
    s_pending_play_requested_ms = 0;
    s_paused = false;
    s_resume_after_interaction = false;
    s_stop_requested = true;
    if (s_player && s_running) {
        esp_audio_simple_player_stop(s_player);
    }
    s_running = false;
    audio_stop_playback();
}

esp_err_t music_player_init(void)
{
    if (!s_mutex) {
        s_mutex = xSemaphoreCreateMutex();
        if (!s_mutex) {
            return ESP_ERR_NO_MEM;
        }
    }
    if (!sd_card_is_mounted()) {
        ESP_LOGW(TAG, "SD card not mounted, local music disabled");
        return ESP_ERR_INVALID_STATE;
    }

    xSemaphoreTake(s_mutex, portMAX_DELAY);
    esp_err_t ret = rescan_tracks_locked();
    if (ret == ESP_OK) {
        ret = ensure_player_initialized();
    }
    xSemaphoreGive(s_mutex);
    return ret;
}

esp_err_t music_player_request_play(bool wait_for_tts_window)
{
    if (!s_mutex) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    esp_err_t ret = rescan_tracks_locked();
    if (ret == ESP_OK) {
        if (!s_running) {
            s_pending_play = true;
            s_wait_for_tts_window = wait_for_tts_window;
            s_wait_observed_audio = false;
            s_pending_play_requested_ms = esp_timer_get_time() / 1000;
            s_stop_requested = false;
            s_paused = false;
            s_resume_after_interaction = false;
            s_current_track = choose_random_track_locked(s_current_track);
        }
    }
    xSemaphoreGive(s_mutex);
    return ret;
}

esp_err_t music_player_request_stop(void)
{
    if (!s_mutex) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    stop_player_locked();
    xSemaphoreGive(s_mutex);
    return ESP_OK;
}

esp_err_t music_player_request_next(void)
{
    if (!s_mutex) {
        return ESP_ERR_INVALID_STATE;
    }

    xSemaphoreTake(s_mutex, portMAX_DELAY);
    esp_err_t ret = rescan_tracks_locked();
    if (ret == ESP_OK) {
        int next_index = choose_random_track_locked(s_current_track);
        if (next_index < 0) {
            ret = ESP_ERR_NOT_FOUND;
        } else {
            if (s_player && (s_running || s_paused)) {
                esp_audio_simple_player_stop(s_player);
                audio_stop_playback();
            }
            s_running = false;
            s_paused = false;
            s_resume_after_interaction = false;
            s_stop_requested = false;
            ret = start_track_locked(next_index);
        }
    }
    xSemaphoreGive(s_mutex);
    return ret;
}

esp_err_t music_player_toggle_pause(void)
{
    if (!s_mutex) {
        return ESP_ERR_INVALID_STATE;
    }

    xSemaphoreTake(s_mutex, portMAX_DELAY);
    esp_err_t ret = ESP_ERR_INVALID_STATE;
    if (s_player && s_paused) {
        esp_gmf_err_t err = esp_audio_simple_player_resume(s_player);
        if (err == ESP_GMF_ERR_OK) {
            s_running = true;
            s_paused = false;
            s_resume_after_interaction = false;
            ret = ESP_OK;
        } else {
            ESP_LOGW(TAG, "Failed to resume music: %d", (int)err);
            ret = ESP_FAIL;
        }
    } else if (s_player && s_running) {
        esp_gmf_err_t err = esp_audio_simple_player_pause(s_player);
        if (err == ESP_GMF_ERR_OK) {
            s_running = false;
            s_paused = true;
            audio_stop_playback();
            ret = ESP_OK;
        } else {
            ESP_LOGW(TAG, "Failed to pause music: %d", (int)err);
            ret = ESP_FAIL;
        }
    }
    xSemaphoreGive(s_mutex);
    return ret;
}

esp_err_t music_player_pause_for_interaction(void)
{
    if (!s_mutex) {
        return ESP_ERR_INVALID_STATE;
    }

    xSemaphoreTake(s_mutex, portMAX_DELAY);
    esp_err_t ret = ESP_OK;
    if (s_player && s_running && !s_paused) {
        esp_gmf_err_t err = esp_audio_simple_player_pause(s_player);
        if (err == ESP_GMF_ERR_OK) {
            s_running = false;
            s_paused = true;
            s_resume_after_interaction = true;
            audio_stop_playback();
        } else {
            ESP_LOGW(TAG, "Failed to pause music for interaction: %d", (int)err);
            ret = ESP_FAIL;
        }
    } else {
        s_resume_after_interaction = false;
    }
    xSemaphoreGive(s_mutex);
    return ret;
}

esp_err_t music_player_resume_after_interaction(void)
{
    if (!s_mutex) {
        return ESP_ERR_INVALID_STATE;
    }

    xSemaphoreTake(s_mutex, portMAX_DELAY);
    esp_err_t ret = ESP_OK;
    if (s_player && s_paused && s_resume_after_interaction) {
        esp_gmf_err_t err = esp_audio_simple_player_resume(s_player);
        if (err == ESP_GMF_ERR_OK) {
            s_running = true;
            s_paused = false;
            s_resume_after_interaction = false;
        } else {
            ESP_LOGW(TAG, "Failed to resume music after interaction: %d", (int)err);
            ret = ESP_FAIL;
        }
    }
    xSemaphoreGive(s_mutex);
    return ret;
}

void music_player_loop(void)
{
    if (!s_mutex) {
        return;
    }

    xSemaphoreTake(s_mutex, portMAX_DELAY);

    if (s_pending_play) {
        bool device_audio_active = audio_is_playing() || ws_client_is_tts_active();
        if (s_wait_for_tts_window) {
            if (device_audio_active) {
                s_wait_observed_audio = true;
            }

            int64_t now_ms = esp_timer_get_time() / 1000;
            bool timeout = (now_ms - s_pending_play_requested_ms) >= MUSIC_TTS_WAIT_TIMEOUT_MS;
            bool ready = (s_wait_observed_audio && !device_audio_active) || timeout;
            if (!ready) {
                xSemaphoreGive(s_mutex);
                return;
            }
        } else if (device_audio_active) {
            xSemaphoreGive(s_mutex);
            return;
        }

        int next_index = (s_current_track >= 0 && s_current_track < (int)s_track_count)
            ? s_current_track : choose_random_track_locked(-1);
        if (start_track_locked(next_index) == ESP_OK) {
            s_pending_play = false;
            s_wait_for_tts_window = false;
            s_wait_observed_audio = false;
            s_pending_play_requested_ms = 0;
        }
    } else if (s_pending_auto_next && !audio_is_playing() && !ws_client_is_tts_active()) {
        s_pending_auto_next = false;
        if (s_track_count > 0) {
            int next_index = choose_random_track_locked(s_current_track);
            start_track_locked(next_index);
        }
    }

    xSemaphoreGive(s_mutex);
}

bool music_player_is_active(void)
{
    return s_running || s_pending_play || s_paused;
}

bool music_player_is_paused(void)
{
    return s_paused;
}

const char *music_player_current_track(void)
{
    if (s_current_track < 0 || s_current_track >= (int)s_track_count) {
        return "";
    }
    return s_tracks[s_current_track];
}
