/**
 * WebSocket 客户端 — 与 Aura 后端通信
 *
 * Protocol (voice_command_server.py):
 *   → text  {"type":"start"}           开始录音
 *   → bin   raw PCM int16 16kHz mono   音频帧
 *   → text  {"type":"stop"}            停止 → ASR → LLM
 *   ← text  {"type":"message","sender":"AI","text":"..."} 回复
 *   ← text  {"type":"status","text":"..."}  状态提示
 *   ← text  {"type":"emotion","emotion":"..."} 表情
 *   ← text  plain-text fallback (non-JSON)
 */
#include "ws_client.h"
#include "aura_config.h"
#include "protocol/messages.h"
#include "display/renderer.h"
#include "core/state_machine.h"
#include "audio_pipeline.h"
#include "audio/opus_decode_bridge.h"
#include "sfx.h"
#include "music_player.h"
#include "state_helpers.h"
#include "wifi_manager.h"
#include "esp_websocket_client.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_transport_ssl.h"
#include "esp_transport_ws.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_app_desc.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "nvs.h"
#include "cJSON.h"
#include "mbedtls/base64.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "lwip/inet.h"
#include <string.h>
#include <stdlib.h>

static const char *TAG = "ws";

static esp_websocket_client_handle_t s_client = NULL;
static esp_transport_handle_t s_wss_transport = NULL;
static bool    s_connected      = false;
static bool    s_ota_paused     = false;
static int64_t s_ota_resume_after_ms = 0;
static int64_t s_last_heartbeat = 0;
static bool    s_handshake_ready = false;
static int64_t s_last_error_sfx_ms = 0;
static bool    s_waiting_send_ack = false;
static bool    s_reply_in_flight = false;
static volatile bool s_server_vad_stop_received = false;
static uint32_t s_connection_seq = 0;
static uint32_t s_turn_seq = 0;
static uint32_t s_heartbeat_seq = 0;
static char     s_device_id[32] = {0};
static char     s_boot_id[24] = {0};
static char     s_auth_token[96] = {0};
static char     s_ws_uri[160] = {0};
static char     s_ws_host[96] = {0};
static char     s_ws_path[96] = {0};
static char     s_device_public_ip[48] = {0};
static char     s_language[3] = "zh";
static bool     s_public_ip_lookup_attempted = false;
static int64_t  s_last_connect_attempt_ms = 0;
static int64_t  s_last_disconnect_ms = 0;
static int64_t  s_last_ws_rebuild_ms = 0;
#define WS_STALL_RECOVERY_MS 30000
#define WS_REBUILD_INTERVAL_MS 15000
#define WS_PUBLIC_IP_STUN_TIMEOUT_MS 1200
#define WS_PUBLIC_IP_LOOKUP_TIMEOUT_MS 2500
#define WS_EVENT_QUEUE_DEPTH 24

typedef struct {
    char *buf;
    size_t size;
    size_t len;
} ws_http_text_buffer_t;

/* ── dialogue+TTS 同步: 文字等语音到了再一起显示 ── */
static bool    s_pending_dialogue = false;   /* 有未显示的 dialogue 文字 */
static int64_t s_dialogue_recv_ms = 0;       /* 收到 dialogue 的时间 */
#define DIALOGUE_TTS_TIMEOUT_MS  90000       /* 仅做异常监控，不再在本地假超时打断 */
#define REPLY_DIALOGUE_TTL_TICKS 180         /* 播放期间冻结；异常时最多保留约 18s */
#define REPLY_DIALOGUE_DONE_TICKS 12          /* 末句播完后约 1.2s 消失 */
#define DIALOGUE_MAX_SEGMENTS 12
#define DIALOGUE_SEGMENT_TEXT_BYTES 192
#define DIALOGUE_SEGMENT_MIN_MS 1200
#define DIALOGUE_SEGMENT_MAX_MS 12000
static char    s_pending_text[512] = {0};
static char    s_pending_segments[DIALOGUE_MAX_SEGMENTS][DIALOGUE_SEGMENT_TEXT_BYTES] = {{0}};
static int     s_pending_segment_ms[DIALOGUE_MAX_SEGMENTS] = {0};
static int     s_pending_segment_count = 0;
static char    s_timed_segments[DIALOGUE_MAX_SEGMENTS][DIALOGUE_SEGMENT_TEXT_BYTES] = {{0}};
static int     s_timed_segment_ms[DIALOGUE_MAX_SEGMENTS] = {0};
static uint32_t s_timed_segment_turn_id = 0;
static char    s_active_segments[DIALOGUE_MAX_SEGMENTS][DIALOGUE_SEGMENT_TEXT_BYTES] = {{0}};
static int     s_active_segment_ms[DIALOGUE_MAX_SEGMENTS] = {0};
static int     s_active_segment_count = 0;
static int     s_active_segment_index = 0;
static int     s_active_segment_elapsed_ms = 0;
static int64_t s_active_segment_last_update_ms = 0;
static uint32_t s_pending_turn_id = 0;
static int     s_pending_pose = -1;
static int     s_pending_scene = -1;
static int     s_pending_coins = 0;
static volatile bool s_dessert_purchase_pending = false;
static bool    s_pending_continue_listening = false;
static bool    s_active_continue_listening_after_tts = false;
static int     s_active_tts_stream_id = 0;
static uint32_t s_active_tts_turn_id = 0;
static bool    s_tts_stream_started = false;
static bool    s_tts_ui_pending = false;
static bool    s_tts_final_received = false;
static int64_t s_tts_active_until_ms = 0;
static int64_t s_last_tts_chunk_ms = 0;
static int64_t s_turn_started_at_ms = 0;
static int64_t s_tts_first_frame_at_ms = 0;
static int64_t s_tts_first_pcm_queued_at_ms = 0;
static int64_t s_tts_playback_started_at_ms = 0;
static int64_t s_tts_final_frame_at_ms = 0;
static size_t  s_tts_first_frame_bytes = 0;
static int64_t s_connection_sfx_silent_until_ms = 0;
static uint8_t *s_tts_prefetch_buf = NULL;
static size_t   s_tts_prefetch_len = 0;
static size_t   s_tts_prefetch_cap = 0;
static int      s_tts_prefetch_stream_id = 0;
#define TTS_STREAM_ACTIVE_GRACE_MS 10000
#define CONNECTION_SFX_SUPPRESS_AFTER_TTS_DISCONNECT_MS 15000
#define TTS_PREFETCH_BYTES 16384
#define TTS_STREAM_QUEUE_BYTES 2048
#define TTS_DONE_SETTLE_MS 220
#define TTS_BINARY_MAGIC "ATTS"
#define TTS_BINARY_HEADER_SIZE 16
#define TTS_BINARY_FLAG_FINAL 0x01
#define TTS_BINARY_FLAG_OPUS  0x02
#define TTS_OPUS_SAMPLE_RATE 16000
#define TTS_OPUS_CHANNELS 1
#define TTS_OPUS_FRAME_MS 60
#define TTS_OPUS_MAX_SAMPLES ((TTS_OPUS_SAMPLE_RATE / 1000) * TTS_OPUS_CHANNELS * TTS_OPUS_FRAME_MS)
#define WS_SEND_TIMEOUT_MS 5000
#define WS_CONTROL_SEND_TIMEOUT_MS 800
#define WS_AUDIO_SEND_TIMEOUT_MS 1200
#define WS_AUDIO_SEND_RETRIES 2
#define WS_AUDIO_SEND_RETRY_DELAY_MS 80
#define WS_AUDIO_SEND_SLOW_WARN_MS 250
#define WS_MAX_INBOUND_MESSAGE_BYTES (1024 * 1024)
#define WS_CLIENT_TASK_STACK_BYTES 12288

/* ── reassembly buffer for large WS text frames ── */
static char   *s_reassembly_buf  = NULL;   /* PSRAM buffer           */
static int     s_reassembly_total = 0;     /* expected payload_len   */
static int     s_reassembly_pos  = 0;      /* bytes received so far  */
static uint8_t *s_binary_reassembly_buf = NULL;
static int      s_binary_reassembly_total = 0;
static int      s_binary_reassembly_pos = 0;
static aura_opus_decoder_t *s_tts_opus_decoder = NULL;
static QueueHandle_t s_ws_event_queue = NULL;

typedef struct {
    int32_t id;
    int op_code;
    int data_len;
    int payload_len;
    int payload_offset;
    int esp_tls_last_esp_err;
    uint8_t *data;
} ws_queued_event_t;

typedef struct {
    bool tls;
    char host[96];
    char path[96];
    int port;
} ws_uri_parts_t;

/* ── forward declarations ─────────────────── */
static void handle_server_message(const char *json_str, int len);
static void ws_sanitize_dialogue_text(const char *text, char *out, size_t out_size);
static void ws_show_connection_notice(const char *text);
static void ws_log_heap_snapshot(const char *reason);
static void ws_clear_pending_dialogue(void);
static void ws_clear_text_reassembly(void);
static void ws_clear_binary_reassembly(void);
static void ws_reset_tts_stream(void);
static bool ws_play_local_audio_id(const char *audio_id);
static void ws_process_queued_event(int32_t id, esp_websocket_event_data_t *ws_data);
static void ws_event_handler(void *arg, esp_event_base_t base,
                             int32_t id, void *data);
static esp_err_t ws_parse_uri_parts(const char *uri, ws_uri_parts_t *parts);
static void ws_refresh_device_public_ip_if_needed(bool force);
static esp_err_t ws_public_ip_http_event(esp_http_client_event_t *evt);
static bool ws_stun_lookup_public_ip(char *out, size_t out_size);
static bool ws_parse_stun_public_ip(const uint8_t *data, size_t len, char *out, size_t out_size);

static void ws_trim_string(char *s)
{
    if (!s) return;
    size_t len = strlen(s);
    while (len > 0 && (s[len - 1] == '\r' || s[len - 1] == '\n' || s[len - 1] == ' ' || s[len - 1] == '\t')) {
        s[--len] = '\0';
    }
}

static esp_err_t ws_parse_uri_parts(const char *uri, ws_uri_parts_t *parts)
{
    if (!uri || !parts) return ESP_ERR_INVALID_ARG;
    memset(parts, 0, sizeof(*parts));

    const char *host_start = NULL;
    int default_port = 0;
    if (strncmp(uri, "wss://", 6) == 0) {
        parts->tls = true;
        host_start = uri + 6;
        default_port = 443;
    } else if (strncmp(uri, "ws://", 5) == 0) {
        parts->tls = false;
        host_start = uri + 5;
        default_port = 80;
    } else {
        return ESP_ERR_INVALID_ARG;
    }

    if (host_start[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }

    const char *path_start = strchr(host_start, '/');
    const char *host_end = path_start ? path_start : uri + strlen(uri);
    const char *port_start = NULL;

    if (host_start[0] == '[') {
        const char *ipv6_end = memchr(host_start, ']', (size_t)(host_end - host_start));
        if (!ipv6_end) return ESP_ERR_INVALID_ARG;
        size_t host_len = (size_t)(ipv6_end - host_start - 1);
        if (host_len == 0 || host_len >= sizeof(parts->host)) return ESP_ERR_INVALID_ARG;
        memcpy(parts->host, host_start + 1, host_len);
        parts->host[host_len] = '\0';
        if ((ipv6_end + 1) < host_end) {
            if (*(ipv6_end + 1) != ':') return ESP_ERR_INVALID_ARG;
            port_start = ipv6_end + 2;
        }
    } else {
        const char *colon = memchr(host_start, ':', (size_t)(host_end - host_start));
        const char *copy_end = colon ? colon : host_end;
        size_t host_len = (size_t)(copy_end - host_start);
        if (host_len == 0 || host_len >= sizeof(parts->host)) return ESP_ERR_INVALID_ARG;
        memcpy(parts->host, host_start, host_len);
        parts->host[host_len] = '\0';
        if (colon) {
            port_start = colon + 1;
        }
    }

    parts->port = default_port;
    if (port_start && port_start < host_end) {
        char port_buf[8] = {0};
        size_t port_len = (size_t)(host_end - port_start);
        if (port_len == 0 || port_len >= sizeof(port_buf)) return ESP_ERR_INVALID_ARG;
        memcpy(port_buf, port_start, port_len);
        char *end = NULL;
        long parsed = strtol(port_buf, &end, 10);
        if (!end || *end != '\0' || parsed <= 0 || parsed > 65535) {
            return ESP_ERR_INVALID_ARG;
        }
        parts->port = (int)parsed;
    }

    const char *path = (path_start && path_start[0] != '\0') ? path_start : "/";
    size_t path_len = strlen(path);
    if (path_len == 0 || path_len >= sizeof(parts->path)) return ESP_ERR_INVALID_ARG;
    memcpy(parts->path, path, path_len + 1);
    return ESP_OK;
}

static bool ws_public_ip_text_is_valid(const char *text)
{
    if (!text || text[0] == '\0') return false;
    bool has_dot = false;
    for (const char *p = text; *p; ++p) {
        if (*p == '.') {
            has_dot = true;
            continue;
        }
        if (*p < '0' || *p > '9') return false;
    }
    return has_dot;
}

static esp_err_t ws_public_ip_http_event(esp_http_client_event_t *evt)
{
    if (!evt || evt->event_id != HTTP_EVENT_ON_DATA || !evt->user_data || !evt->data || evt->data_len <= 0) {
        return ESP_OK;
    }
    ws_http_text_buffer_t *buffer = (ws_http_text_buffer_t *)evt->user_data;
    if (!buffer->buf || buffer->size == 0 || buffer->len >= buffer->size - 1) {
        return ESP_OK;
    }
    size_t remaining = buffer->size - buffer->len - 1;
    size_t copy_len = (size_t)evt->data_len;
    if (copy_len > remaining) copy_len = remaining;
    memcpy(buffer->buf + buffer->len, evt->data, copy_len);
    buffer->len += copy_len;
    buffer->buf[buffer->len] = '\0';
    return ESP_OK;
}

static bool ws_parse_stun_public_ip(const uint8_t *data, size_t len, char *out, size_t out_size)
{
    if (!data || len < 20 || !out || out_size == 0) return false;
    if (data[0] != 0x01 || data[1] != 0x01) return false; // Binding Success Response
    if (data[4] != 0x21 || data[5] != 0x12 || data[6] != 0xA4 || data[7] != 0x42) return false;

    size_t body_len = ((size_t)data[2] << 8) | data[3];
    if (body_len + 20 > len) body_len = len - 20;
    size_t offset = 20;
    while (offset + 4 <= 20 + body_len) {
        uint16_t attr_type = ((uint16_t)data[offset] << 8) | data[offset + 1];
        uint16_t attr_len = ((uint16_t)data[offset + 2] << 8) | data[offset + 3];
        size_t value = offset + 4;
        if (value + attr_len > len) return false;

        if ((attr_type == 0x0020 || attr_type == 0x0001) && attr_len >= 8 && data[value + 1] == 0x01) {
            uint8_t ip[4] = {
                data[value + 4],
                data[value + 5],
                data[value + 6],
                data[value + 7],
            };
            if (attr_type == 0x0020) { // XOR-MAPPED-ADDRESS
                ip[0] ^= 0x21;
                ip[1] ^= 0x12;
                ip[2] ^= 0xA4;
                ip[3] ^= 0x42;
            }
            snprintf(out, out_size, "%u.%u.%u.%u", ip[0], ip[1], ip[2], ip[3]);
            return ws_public_ip_text_is_valid(out);
        }
        offset = value + ((attr_len + 3u) & ~3u);
    }
    return false;
}

static bool ws_stun_lookup_public_ip(char *out, size_t out_size)
{
    static const struct {
        const char *host;
        const char *port;
    } servers[] = {
        {"stun.miwifi.com", "3478"},
        {"stun.qq.com", "3478"},
        {"stun.cloudflare.com", "3478"},
        {"stun.l.google.com", "19302"},
    };

    if (!out || out_size == 0) return false;
    out[0] = '\0';

    for (size_t i = 0; i < sizeof(servers) / sizeof(servers[0]); ++i) {
        struct addrinfo hints = {0};
        hints.ai_family = AF_INET;
        hints.ai_socktype = SOCK_DGRAM;
        hints.ai_protocol = IPPROTO_UDP;
        struct addrinfo *res = NULL;
        int gai = getaddrinfo(servers[i].host, servers[i].port, &hints, &res);
        if (gai != 0 || !res) {
            ESP_LOGW(TAG, "Public IP STUN DNS failed host=%s err=%d", servers[i].host, gai);
            continue;
        }

        int sock = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
        if (sock < 0) {
            ESP_LOGW(TAG, "Public IP STUN socket failed host=%s", servers[i].host);
            freeaddrinfo(res);
            continue;
        }

        struct timeval timeout = {
            .tv_sec = WS_PUBLIC_IP_STUN_TIMEOUT_MS / 1000,
            .tv_usec = (WS_PUBLIC_IP_STUN_TIMEOUT_MS % 1000) * 1000,
        };
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));

        uint8_t request[20] = {0};
        request[1] = 0x01; // Binding Request
        request[4] = 0x21;
        request[5] = 0x12;
        request[6] = 0xA4;
        request[7] = 0x42;
        uint32_t r0 = esp_random();
        uint32_t r1 = esp_random();
        uint32_t r2 = esp_random();
        memcpy(request + 8, &r0, sizeof(r0));
        memcpy(request + 12, &r1, sizeof(r1));
        memcpy(request + 16, &r2, sizeof(r2));

        ssize_t sent = sendto(sock, request, sizeof(request), 0, res->ai_addr, res->ai_addrlen);
        uint8_t response[256] = {0};
        ssize_t received = -1;
        if (sent == (ssize_t)sizeof(request)) {
            received = recvfrom(sock, response, sizeof(response), 0, NULL, NULL);
        }
        close(sock);
        freeaddrinfo(res);

        if (received <= 0) {
            ESP_LOGW(TAG, "Public IP STUN failed host=%s received=%d", servers[i].host, (int)received);
            continue;
        }
        if (ws_parse_stun_public_ip(response, (size_t)received, out, out_size)) {
            ESP_LOGI(TAG, "Device public IP detected via STUN host=%s ip=%s", servers[i].host, out);
            return true;
        }
        ESP_LOGW(TAG, "Public IP STUN parse failed host=%s len=%d", servers[i].host, (int)received);
    }
    out[0] = '\0';
    return false;
}

static void ws_refresh_device_public_ip_if_needed(bool force)
{
#if !AURA_DEVICE_PUBLIC_IP_LOOKUP
    (void)force;
    s_device_public_ip[0] = '\0';
    return;
#else
    if (!force && s_public_ip_lookup_attempted) {
        return;
    }
    if (!wifi_manager_is_connected()) {
        return;
    }
    s_public_ip_lookup_attempted = true;

    if (ws_stun_lookup_public_ip(s_device_public_ip, sizeof(s_device_public_ip))) {
        return;
    }

    const char *urls[] = {
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "http://api.ipify.org",
    };
    for (size_t i = 0; i < sizeof(urls) / sizeof(urls[0]); ++i) {
        char lookup_buf[48] = {0};
        ws_http_text_buffer_t text_buffer = {
            .buf = lookup_buf,
            .size = sizeof(lookup_buf),
            .len = 0,
        };
        esp_http_client_config_t cfg = {
            .url = urls[i],
            .timeout_ms = WS_PUBLIC_IP_LOOKUP_TIMEOUT_MS,
            .crt_bundle_attach = esp_crt_bundle_attach,
            .event_handler = ws_public_ip_http_event,
            .user_data = &text_buffer,
            .buffer_size = 128,
        };
        esp_http_client_handle_t client = esp_http_client_init(&cfg);
        if (!client) {
            ESP_LOGW(TAG, "Public IP lookup init failed");
            continue;
        }
        esp_err_t err = esp_http_client_perform(client);
        int status = esp_http_client_get_status_code(client);
        esp_http_client_cleanup(client);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Public IP lookup failed url=%u err=0x%x", (unsigned)i, err);
            continue;
        }
        ws_trim_string(lookup_buf);
        if (status == 200 && ws_public_ip_text_is_valid(lookup_buf)) {
            strncpy(s_device_public_ip, lookup_buf, sizeof(s_device_public_ip) - 1);
            s_device_public_ip[sizeof(s_device_public_ip) - 1] = '\0';
            ESP_LOGI(TAG, "Device public IP detected: %s", s_device_public_ip);
            return;
        }
        ESP_LOGW(TAG, "Public IP lookup returned status=%d text_len=%u", status, (unsigned)strlen(lookup_buf));
    }
    s_device_public_ip[0] = '\0';
#endif
}

static esp_err_t ws_store_auth_token(const char *token)
{
    if (!token) return ESP_ERR_INVALID_ARG;
    nvs_handle_t nvs;
    esp_err_t err = nvs_open("device", NVS_READWRITE, &nvs);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_str(nvs, "auth_token", token);
    if (err == ESP_OK) {
        err = nvs_commit(nvs);
    }
    nvs_close(nvs);
    return err;
}

static void ws_init_device_identity(void)
{
    if (s_device_id[0] != '\0') return;

    uint8_t mac[6] = {0};
    if (esp_read_mac(mac, ESP_MAC_WIFI_STA) != ESP_OK) {
        memset(mac, 0, sizeof(mac));
    }
    snprintf(
        s_device_id,
        sizeof(s_device_id),
        "aura-%02x%02x%02x%02x%02x%02x",
        mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]
    );

    uint32_t r0 = esp_random();
    uint32_t r1 = esp_random();
    snprintf(s_boot_id, sizeof(s_boot_id), "%08x%08x", (unsigned)r0, (unsigned)r1);

    nvs_handle_t nvs;
    if (nvs_open("device", NVS_READONLY, &nvs) == ESP_OK) {
        size_t len = sizeof(s_auth_token);
        if (nvs_get_str(nvs, "auth_token", s_auth_token, &len) != ESP_OK) {
            s_auth_token[0] = '\0';
        }
        nvs_close(nvs);
    }
    if (s_auth_token[0] == '\0' && strlen(AURA_DEVICE_AUTH_TOKEN) > 0) {
        strncpy(s_auth_token, AURA_DEVICE_AUTH_TOKEN, sizeof(s_auth_token) - 1);
        s_auth_token[sizeof(s_auth_token) - 1] = '\0';
    }
    ws_trim_string(s_auth_token);
}

static const char *ws_state_name(void)
{
    switch (fsm_get_state()) {
        case AURA_STATE_IDLE: return "idle";
        case AURA_STATE_LISTENING: return "listening";
        case AURA_STATE_PROCESSING: return "processing";
        case AURA_STATE_SPEAKING: return "speaking";
        default: return "unknown";
    }
}

static esp_err_t ws_send_hello(void)
{
    ws_refresh_device_public_ip_if_needed(false);
    cJSON *root = cJSON_CreateObject();
    cJSON *device = cJSON_AddObjectToObject(root, "device");
    cJSON *session = cJSON_AddObjectToObject(root, "session");
    cJSON *audio = cJSON_AddObjectToObject(root, "audio_params");
    cJSON *caps = cJSON_AddObjectToObject(root, "capabilities");
    if (!root || !device || !session || !audio || !caps) {
        cJSON_Delete(root);
        return ESP_ERR_NO_MEM;
    }

    const esp_app_desc_t *app = esp_app_get_description();

    cJSON_AddStringToObject(root, "type", "hello");
    cJSON_AddNumberToObject(root, "version", 4);
    cJSON_AddStringToObject(root, "transport", "websocket");
    cJSON_AddStringToObject(root, "language", s_language);

    cJSON_AddStringToObject(device, "id", s_device_id);
    cJSON_AddStringToObject(device, "name", AURA_DEVICE_NAME);
    cJSON_AddStringToObject(device, "boot_id", s_boot_id);
    if (app && app->version[0] != '\0') {
        cJSON_AddStringToObject(device, "fw_version", app->version);
    }
    if (s_auth_token[0] != '\0') {
        cJSON_AddStringToObject(device, "auth_token", s_auth_token);
    }
    if (s_device_public_ip[0] != '\0') {
        cJSON_AddStringToObject(device, "public_ip", s_device_public_ip);
        cJSON_AddStringToObject(root, "device_public_ip", s_device_public_ip);
    }

    cJSON_AddNumberToObject(session, "connection_seq", (double)s_connection_seq);
    cJSON_AddStringToObject(session, "state", ws_state_name());

    cJSON_AddStringToObject(audio, "format", "opus");
    cJSON_AddNumberToObject(audio, "sample_rate", 16000);
    cJSON_AddNumberToObject(audio, "channels", 1);
    cJSON_AddNumberToObject(audio, "frame_duration", 60);

    cJSON_AddStringToObject(caps, "upstream_audio", "opus");
    cJSON_AddStringToObject(caps, "downstream_audio", "opus");
    cJSON_AddBoolToObject(caps, "binary_tts", true);
    cJSON_AddBoolToObject(caps, "server_vad", AURA_SERVER_VAD_DEFAULT);
    cJSON_AddBoolToObject(caps, "button_cancel", true);
    cJSON_AddBoolToObject(caps, "single_device", true);

    char *payload = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!payload) {
        return ESP_ERR_NO_MEM;
    }

    esp_err_t ret = ws_client_send_text(payload);
    cJSON_free(payload);
    return ret;
}

static void play_error_sfx_throttled(void)
{
    int64_t now = esp_timer_get_time() / 1000;
    if (now < s_connection_sfx_silent_until_ms) {
        ESP_LOGI(TAG, "Suppressing connection error SFX during TTS recovery window");
        return;
    }
    if (now - s_last_error_sfx_ms < 2000) {
        return;
    }
    s_last_error_sfx_ms = now;
    sfx_play(SFX_ERROR);
}

static bool ws_tts_or_audio_active(void)
{
    int64_t now_ms = esp_timer_get_time() / 1000;
    return s_pending_dialogue ||
           s_reply_in_flight ||
           s_tts_stream_started ||
           s_active_tts_stream_id > 0 ||
           now_ms < s_tts_active_until_ms ||
           audio_is_non_music_playing();
}

static void ws_show_connection_notice(const char *text)
{
    const char *msg = (text && text[0] != '\0') ? text : "连接中...";

    if (fsm_get_state() != AURA_STATE_IDLE) {
        return;
    }

    char tmp[512];
    ws_sanitize_dialogue_text(msg, tmp, sizeof(tmp));
    aura_ui_set_dialogue(tmp, 600);
    aura_ui_set_agent_visible(false);
}

static void ws_log_heap_snapshot(const char *reason)
{
    multi_heap_info_t internal_info = {0};
    multi_heap_info_t spiram_info = {0};
    heap_caps_get_info(&internal_info, MALLOC_CAP_INTERNAL);
    heap_caps_get_info(&spiram_info, MALLOC_CAP_SPIRAM);
    ESP_LOGI(
        TAG,
        "heap[%s] internal_free=%u internal_largest=%u spiram_free=%u spiram_largest=%u",
        reason ? reason : "?",
        (unsigned)internal_info.total_free_bytes,
        (unsigned)internal_info.largest_free_block,
        (unsigned)spiram_info.total_free_bytes,
        (unsigned)spiram_info.largest_free_block
    );
}

static int ws_clamp_companion_stat(int value)
{
    if (value < 0) return 0;
    if (value > AURA_COMPANION_STAT_MAX) return AURA_COMPANION_STAT_MAX;
    return value;
}

static int ws_clamp_beans(int value)
{
    if (value < 0) return 0;
    if (value > AURA_BEANS_MAX) return AURA_BEANS_MAX;
    return value;
}

static int ws_clamp_affinity_level(int value)
{
    if (value < 1) return 1;
    if (value > 5) return 5;
    return value;
}

/* Older gateway builds may only provide text such as "5h 100/100 tokens".
 * Extract the ratio so the narrow status panel still uses the compact form. */
static bool ws_quota_ratio_percent(const char *text, int *percent_out)
{
    if (!text || !percent_out) return false;
    const char *cursor = text;
    while (*cursor) {
        if ((*cursor < '0' || *cursor > '9') && *cursor != '.') {
            ++cursor;
            continue;
        }
        char *numerator_end = NULL;
        float numerator = strtof(cursor, &numerator_end);
        if (!numerator_end || numerator_end == cursor) {
            ++cursor;
            continue;
        }
        const char *slash = numerator_end;
        while (*slash && *slash != '/') ++slash;
        if (*slash == '/') {
            char *denominator_end = NULL;
            float denominator = strtof(slash + 1, &denominator_end);
            if (denominator_end && denominator_end != slash + 1 && denominator > 0.0f) {
                int percent = (int)((numerator * 100.0f / denominator) + 0.5f);
                if (percent < 0) percent = 0;
                if (percent > 100) percent = 100;
                *percent_out = percent;
                return true;
            }
        }
        cursor = numerator_end;
    }
    return false;
}

static int ws_clamp_segment_ms(int value)
{
    if (value < DIALOGUE_SEGMENT_MIN_MS) return DIALOGUE_SEGMENT_MIN_MS;
    if (value > DIALOGUE_SEGMENT_MAX_MS) return DIALOGUE_SEGMENT_MAX_MS;
    return value;
}

static bool text_has_non_ascii(const char *text)
{
    if (!text) return false;
    while (*text) {
        if ((unsigned char)*text >= 0x80) return true;
        text++;
    }
    return false;
}

static int ws_utf8_visible_char_count(const char *text)
{
    if (!text) return 0;
    int count = 0;
    const unsigned char *p = (const unsigned char *)text;
    while (*p) {
        if ((*p & 0x80) == 0) {
            p += 1;
        } else if ((*p & 0xE0) == 0xC0 && p[1]) {
            p += 2;
        } else if ((*p & 0xF0) == 0xE0 && p[1] && p[2]) {
            p += 3;
        } else if ((*p & 0xF8) == 0xF0 && p[1] && p[2] && p[3]) {
            p += 4;
        } else {
            p += 1;
        }
        count++;
    }
    return count;
}

static bool ascii_is_space(char ch)
{
    return ch == ' ' || ch == '\n' || ch == '\r' ||
           ch == '\t' || ch == '\f' || ch == '\v';
}

static void ws_append_space(char *out, size_t out_size, size_t *out_len, bool *pending_space)
{
    if (!pending_space || !*pending_space) return;
    if (*out_len > 0 && out[*out_len - 1] != ' ' && *out_len + 1 < out_size) {
        out[(*out_len)++] = ' ';
    }
    *pending_space = false;
}

static void ws_sanitize_dialogue_text(const char *text, char *out, size_t out_size)
{
    size_t src = 0;
    size_t dst = 0;
    bool pending_space = false;
    bool skip_link_target = false;
    bool line_start = true;

    if (!out || out_size == 0) return;
    out[0] = '\0';
    if (!text) return;

    while (text[src] != '\0' && dst + 1 < out_size) {
        unsigned char ch = (unsigned char)text[src];

        if (skip_link_target) {
            if (ch == ')') {
                skip_link_target = false;
            }
            src++;
            continue;
        }

        if (ch < 0x80) {
            if ((strncmp(&text[src], "http://", 7) == 0) ||
                (strncmp(&text[src], "https://", 8) == 0)) {
                while (text[src] != '\0' &&
                       !ascii_is_space(text[src]) &&
                       text[src] != ')') {
                    src++;
                }
                pending_space = true;
                continue;
            }

            if (text[src] == '!' && text[src + 1] == '[') {
                src++;
                continue;
            }
            if (text[src] == '[') {
                src++;
                continue;
            }
            if (text[src] == ']' && text[src + 1] == '(') {
                skip_link_target = true;
                src += 2;
                continue;
            }
            if (text[src] == '`') {
                if (text[src + 1] == '`' && text[src + 2] == '`') {
                    src += 3;
                } else {
                    src++;
                }
                continue;
            }
            if (text[src] == '*' || text[src] == '_' ||
                text[src] == '~' || text[src] == '#') {
                src++;
                continue;
            }
            if (text[src] == '<') {
                while (text[src] != '\0' && text[src] != '>') {
                    src++;
                }
                if (text[src] == '>') {
                    src++;
                }
                pending_space = true;
                continue;
            }
            if (text[src] == '|' || text[src] == '>') {
                src++;
                pending_space = true;
                continue;
            }
            if ((text[src] == '-' || text[src] == '+' || text[src] == '!') &&
                line_start && (text[src + 1] == ' ' || text[src + 1] == '\t')) {
                src++;
                continue;
            }
            if (ascii_is_space((char)ch)) {
                pending_space = true;
                line_start = (text[src] == '\n' || text[src] == '\r');
                src++;
                continue;
            }

            ws_append_space(out, out_size, &dst, &pending_space);
            out[dst++] = (char)ch;
            line_start = false;
            src++;
            continue;
        }

        int clen = 1;
        if ((ch & 0xF8) == 0xF0) clen = 4;
        else if ((ch & 0xF0) == 0xE0) clen = 3;
        else if ((ch & 0xE0) == 0xC0) clen = 2;
        if (dst + (size_t)clen >= out_size) {
            break;
        }
        ws_append_space(out, out_size, &dst, &pending_space);
        if (dst + (size_t)clen >= out_size) {
            break;
        }
        memcpy(&out[dst], &text[src], clen);
        dst += clen;
        line_start = false;
        src += clen;
    }

    while (dst > 0 && out[dst - 1] == ' ') {
        dst--;
    }
    out[dst] = '\0';
}

static void ws_set_agent_panel(const char *title, int progress, const char *status)
{
    /* AGENT 面板仅在语音交互(PROCESSING/SPEAKING)时显示，IDLE 不弹出 */
    aura_fsm_state_t st = fsm_get_state();
    if (st != AURA_STATE_PROCESSING && st != AURA_STATE_SPEAKING) return;
    if (progress < 0) progress = 0;
    if (progress > 100) progress = 100;
    aura_ui_set_agent_panel(true, progress, (title && title[0]) ? title : "AGENT", status);
}

static void ws_set_agent_progress(int progress, const char *status)
{
    ws_set_agent_panel("AGENT", progress, status);
}

/* 后台任务打工面板：绕过 FSM 限制。
 * 首句“我去查”播完后设备回到 IDLE，但后台任务还在跑，
 * 这段等待就是“打工挣零花钱”的时间，WORK 面板要一直亮着。 */
static void ws_set_background_work_panel(int progress, const char *status)
{
    if (progress < 0) progress = 0;
    if (progress > 100) progress = 100;
    aura_ui_set_agent_panel(true, progress, "WORK", status);
    aura_ui_mark_dirty();
}

static void ws_clear_pending_dialogue(void)
{
    s_pending_dialogue = false;
    s_dialogue_recv_ms = 0;
    s_pending_text[0] = '\0';
    s_pending_segment_count = 0;
    memset(s_pending_segments, 0, sizeof(s_pending_segments));
    memset(s_pending_segment_ms, 0, sizeof(s_pending_segment_ms));
    s_pending_turn_id = 0;
    s_pending_pose = -1;
    s_pending_scene = -1;
    s_pending_coins = 0;
    s_pending_continue_listening = false;
}

static void ws_clear_dialogue_timings(void)
{
    memset(s_timed_segments, 0, sizeof(s_timed_segments));
    memset(s_timed_segment_ms, 0, sizeof(s_timed_segment_ms));
    s_timed_segment_turn_id = 0;
}

static void ws_clear_active_dialogue_segments(void)
{
    s_active_segment_count = 0;
    s_active_segment_index = 0;
    s_active_segment_elapsed_ms = 0;
    s_active_segment_last_update_ms = 0;
    memset(s_active_segments, 0, sizeof(s_active_segments));
    memset(s_active_segment_ms, 0, sizeof(s_active_segment_ms));
}

static void ws_clear_tts_prefetch(void)
{
    if (s_tts_prefetch_buf) {
        heap_caps_free(s_tts_prefetch_buf);
        s_tts_prefetch_buf = NULL;
    }
    s_tts_prefetch_len = 0;
    s_tts_prefetch_cap = 0;
    s_tts_prefetch_stream_id = 0;
}

static void ws_clear_text_reassembly(void)
{
    if (s_reassembly_buf) {
        heap_caps_free(s_reassembly_buf);
        s_reassembly_buf = NULL;
    }
    s_reassembly_total = 0;
    s_reassembly_pos = 0;
}

static void ws_clear_binary_reassembly(void)
{
    if (s_binary_reassembly_buf) {
        heap_caps_free(s_binary_reassembly_buf);
        s_binary_reassembly_buf = NULL;
    }
    s_binary_reassembly_total = 0;
    s_binary_reassembly_pos = 0;
}

static esp_err_t ws_ensure_tts_opus_decoder(void)
{
    if (s_tts_opus_decoder) {
        return ESP_OK;
    }
    s_tts_opus_decoder = aura_opus_decoder_create(
        TTS_OPUS_SAMPLE_RATE,
        TTS_OPUS_CHANNELS,
        TTS_OPUS_FRAME_MS
    );
    if (!s_tts_opus_decoder) {
        ESP_LOGE(TAG, "Failed to create TTS Opus decoder");
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

static void ws_reset_tts_opus_decoder(void)
{
    if (s_tts_opus_decoder) {
        aura_opus_decoder_reset(s_tts_opus_decoder);
    }
}

static void ws_reset_tts_stream(void)
{
    s_active_tts_stream_id = 0;
    s_active_tts_turn_id = 0;
    s_tts_stream_started = false;
    s_tts_ui_pending = false;
    s_tts_final_received = false;
    s_tts_active_until_ms = 0;
    s_last_tts_chunk_ms = 0;
    s_tts_first_frame_at_ms = 0;
    s_tts_first_pcm_queued_at_ms = 0;
    s_tts_playback_started_at_ms = 0;
    s_tts_final_frame_at_ms = 0;
    s_tts_first_frame_bytes = 0;
    s_active_continue_listening_after_tts = false;
    ws_clear_active_dialogue_segments();
    ws_reset_tts_opus_decoder();
    ws_clear_tts_prefetch();
}

static void ws_begin_new_turn(void)
{
    s_waiting_send_ack = false;
    s_reply_in_flight = false;
    s_turn_started_at_ms = 0;
    ws_clear_pending_dialogue();
    ws_clear_dialogue_timings();
    ws_reset_tts_stream();
    ws_clear_text_reassembly();
    ws_clear_binary_reassembly();
    s_server_vad_stop_received = false;
    audio_stop_playback();
    aura_ui_clear_dialogue();
    aura_ui_set_agent_visible(false);
}

static void ws_dispose_client(void)
{
    if (s_client) {
        esp_err_t stop_err = esp_websocket_client_stop(s_client);
        if (stop_err != ESP_OK && stop_err != ESP_FAIL && stop_err != ESP_ERR_INVALID_STATE) {
            ESP_LOGW(TAG, "WebSocket stop during rebuild returned: 0x%x", stop_err);
        }
        esp_err_t destroy_err = esp_websocket_client_destroy(s_client);
        if (destroy_err != ESP_OK) {
            ESP_LOGW(TAG, "WebSocket destroy during rebuild returned: 0x%x", destroy_err);
        }
        s_client = NULL;
    }
    if (s_wss_transport) {
        esp_transport_destroy(s_wss_transport);
        s_wss_transport = NULL;
    }
}

void ws_client_prepare_for_ota(void)
{
    s_ota_paused = true;
    s_ota_resume_after_ms = 0;
    s_connected = false;
    s_handshake_ready = false;
    s_waiting_send_ack = false;
    s_reply_in_flight = false;
    aura_ui_set_ws_connected(false);
    aura_ui_set_agent_visible(false);
    ws_dispose_client();
    ws_log_heap_snapshot("ota-ready");
}

void ws_client_resume_after_ota(void)
{
    if (!s_ota_paused) return;
    s_ota_paused = false;
    s_ota_resume_after_ms = esp_timer_get_time() / 1000 + 1000;
}

static esp_err_t ws_force_rebuild(const char *reason)
{
    int64_t now_ms = esp_timer_get_time() / 1000;
    if (s_ws_uri[0] == '\0') {
        ESP_LOGE(TAG, "Cannot rebuild WebSocket client: URI is empty");
        return ESP_ERR_INVALID_STATE;
    }
    if ((now_ms - s_last_ws_rebuild_ms) < WS_REBUILD_INTERVAL_MS) {
        return ESP_OK;
    }

    ESP_LOGW(TAG, "Rebuilding WebSocket client: %s", reason ? reason : "unknown");
    s_last_ws_rebuild_ms = now_ms;
    s_connected = false;
    s_handshake_ready = false;
    s_waiting_send_ack = false;
    s_reply_in_flight = false;
    s_last_disconnect_ms = now_ms;
    ws_clear_pending_dialogue();
    ws_reset_tts_stream();
    ws_clear_text_reassembly();
    ws_clear_binary_reassembly();
    aura_ui_set_ws_connected(false);
    aura_ui_set_agent_visible(false);
    ws_show_connection_notice("连接恢复中...");

    ws_dispose_client();

    esp_err_t err = ws_client_init(s_ws_uri);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "WebSocket re-init failed: 0x%x", err);
        return err;
    }
    err = ws_client_connect();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "WebSocket reconnect start failed: 0x%x", err);
    }
    return err;
}

esp_err_t ws_client_apply_uri(const char *uri)
{
    /* mDNS 重新发现到新网关地址时切换连接（换网络场景）。 */
    if (!uri || uri[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    if (strncmp(uri, s_ws_uri, sizeof(s_ws_uri)) == 0) {
        return ESP_OK;
    }
    ESP_LOGW(TAG, "Switching WebSocket URI: %s -> %s", s_ws_uri, uri);
    strncpy(s_ws_uri, uri, sizeof(s_ws_uri) - 1);
    s_ws_uri[sizeof(s_ws_uri) - 1] = '\0';
    s_last_ws_rebuild_ms = 0;  /* 地址变了，跳过重建限频立即生效 */
    return ws_force_rebuild("uri_changed");
}

static uint32_t ws_message_turn_id(const cJSON *payload)
{
    if (!payload || !cJSON_IsObject(payload)) return 0;
    const cJSON *turn_id = cJSON_GetObjectItem(payload, "turn_id");
    if (turn_id && cJSON_IsNumber(turn_id) && turn_id->valueint > 0) {
        return (uint32_t)turn_id->valueint;
    }
    return 0;
}

static bool ws_turn_matches(uint32_t incoming_turn_id)
{
    if (incoming_turn_id == 0) return true;
    return incoming_turn_id == s_turn_seq;
}

static void ws_buffer_pending_dialogue(const char *text, int pose, int scene, int coins,
                                       uint32_t turn_id, bool continue_listening)
{
    if (text) {
        ws_sanitize_dialogue_text(text, s_pending_text, sizeof(s_pending_text));
    } else {
        s_pending_text[0] = '\0';
    }
    s_pending_turn_id = turn_id;
    s_pending_pose = pose;
    s_pending_scene = scene;
    s_pending_coins = coins;
    s_pending_continue_listening = continue_listening;
    s_pending_dialogue = true;
    s_dialogue_recv_ms = esp_timer_get_time() / 1000;
}

static void ws_load_pending_dialogue_segments(const cJSON *payload)
{
    s_pending_segment_count = 0;
    memset(s_pending_segments, 0, sizeof(s_pending_segments));
    memset(s_pending_segment_ms, 0, sizeof(s_pending_segment_ms));
    if (!payload) return;

    const cJSON *segments = cJSON_GetObjectItem(payload, "segments");
    if (!segments || !cJSON_IsArray(segments)) return;

    const cJSON *item = NULL;
    cJSON_ArrayForEach(item, segments) {
        if (s_pending_segment_count >= DIALOGUE_MAX_SEGMENTS) break;

        const char *text = NULL;
        int duration_ms = 0;
        if (cJSON_IsString(item)) {
            text = item->valuestring;
        } else if (cJSON_IsObject(item)) {
            const cJSON *text_item = cJSON_GetObjectItem(item, "text");
            const cJSON *duration_item = cJSON_GetObjectItem(item, "duration_ms");
            if (text_item && cJSON_IsString(text_item)) {
                text = text_item->valuestring;
            }
            if (duration_item && cJSON_IsNumber(duration_item)) {
                duration_ms = duration_item->valueint;
            }
        }
        if (!text || text[0] == '\0') continue;

        int idx = s_pending_segment_count;
        ws_sanitize_dialogue_text(text, s_pending_segments[idx], sizeof(s_pending_segments[idx]));
        if (s_pending_segments[idx][0] == '\0') continue;
        if (duration_ms <= 0) {
            duration_ms = 700 + ws_utf8_visible_char_count(s_pending_segments[idx]) * 260;
        }
        s_pending_segment_ms[idx] = ws_clamp_segment_ms(duration_ms);
        s_pending_segment_count++;
    }

    uint32_t turn_id = ws_message_turn_id(payload);
    if (turn_id != 0 && turn_id == s_timed_segment_turn_id) {
        for (int i = 0; i < s_pending_segment_count; i++) {
            if (s_timed_segments[i][0] != '\0') {
                strncpy(s_pending_segments[i], s_timed_segments[i], sizeof(s_pending_segments[i]) - 1);
                s_pending_segments[i][sizeof(s_pending_segments[i]) - 1] = '\0';
            }
            if (s_timed_segment_ms[i] > 0) {
                s_pending_segment_ms[i] = s_timed_segment_ms[i];
            }
        }
    }
}

static void ws_note_dialogue_segment_timing(const cJSON *payload)
{
    if (!payload) return;
    uint32_t turn_id = ws_message_turn_id(payload);
    if (!ws_turn_matches(turn_id)) return;

    const cJSON *index_item = cJSON_GetObjectItem(payload, "segment_index");
    const cJSON *text_item = cJSON_GetObjectItem(payload, "text");
    const cJSON *duration_item = cJSON_GetObjectItem(payload, "duration_ms");
    if (!index_item || !cJSON_IsNumber(index_item)) return;

    int index = index_item->valueint;
    if (index < 0 || index >= DIALOGUE_MAX_SEGMENTS) return;

    int duration_ms = 0;
    if (duration_item && cJSON_IsNumber(duration_item)) {
        duration_ms = ws_clamp_segment_ms(duration_item->valueint);
    }

    char segment_text[DIALOGUE_SEGMENT_TEXT_BYTES] = {0};
    if (text_item && cJSON_IsString(text_item)) {
        ws_sanitize_dialogue_text(text_item->valuestring, segment_text, sizeof(segment_text));
    }

    if (turn_id != 0) {
        if (s_timed_segment_turn_id != turn_id) {
            ws_clear_dialogue_timings();
            s_timed_segment_turn_id = turn_id;
        }
        if (segment_text[0] != '\0') {
            strncpy(s_timed_segments[index], segment_text, sizeof(s_timed_segments[index]) - 1);
            s_timed_segments[index][sizeof(s_timed_segments[index]) - 1] = '\0';
        }
        if (duration_ms > 0) {
            s_timed_segment_ms[index] = duration_ms;
        }
    }

    if (s_pending_dialogue && index < s_pending_segment_count) {
        if (segment_text[0] != '\0') {
            strncpy(s_pending_segments[index], segment_text, sizeof(s_pending_segments[index]) - 1);
            s_pending_segments[index][sizeof(s_pending_segments[index]) - 1] = '\0';
        }
        if (duration_ms > 0) {
            s_pending_segment_ms[index] = duration_ms;
        }
    }
    if (index < s_active_segment_count) {
        if (segment_text[0] != '\0') {
            strncpy(s_active_segments[index], segment_text, sizeof(s_active_segments[index]) - 1);
            s_active_segments[index][sizeof(s_active_segments[index]) - 1] = '\0';
        }
        if (duration_ms > 0) {
            s_active_segment_ms[index] = duration_ms;
        }
    }
}

static void ws_apply_pending_dialogue(void)
{
    if (!s_pending_dialogue) return;
    if (!ws_turn_matches(s_pending_turn_id)) {
        ESP_LOGW(TAG, "Discarding buffered dialogue from stale turn %u (current=%u)",
                 (unsigned)s_pending_turn_id, (unsigned)s_turn_seq);
        ws_clear_pending_dialogue();
        return;
    }

    if (s_pending_segment_count > 0) {
        s_active_segment_count = s_pending_segment_count;
        s_active_segment_index = 0;
        memcpy(s_active_segments, s_pending_segments, sizeof(s_active_segments));
        memcpy(s_active_segment_ms, s_pending_segment_ms, sizeof(s_active_segment_ms));
        s_active_segment_elapsed_ms = 0;
        s_active_segment_last_update_ms = esp_timer_get_time() / 1000;
        aura_ui_set_dialogue(s_active_segments[0], REPLY_DIALOGUE_TTL_TICKS);
        ESP_LOGI(TAG, "Dialogue segmented start 1/%d: %.40s",
                 s_active_segment_count, s_active_segments[0]);
    } else if (s_pending_text[0] != '\0') {
        aura_ui_set_dialogue(s_pending_text, REPLY_DIALOGUE_TTL_TICKS);
    }
    s_active_continue_listening_after_tts = s_pending_continue_listening;
    if (s_pending_pose >= 0)
        g_state.current_pose = s_pending_pose;
    if (s_pending_scene >= 0)
        g_state.current_scene = s_pending_scene;
    if (s_pending_coins > 0) {
        g_state.coins = ws_clamp_beans(g_state.coins + s_pending_coins);
        if (g_state.companion_state_ready) {
            aura_companion_state_cache_save();
        }
    }

    // 字幕一旦上屏，就收起右侧 WORK 动画面板，避免两者并存。
    aura_ui_set_agent_visible(false);
    aura_ui_mark_dirty();
    ws_clear_pending_dialogue();
}

static void ws_update_active_dialogue_segment(void)
{
    if (s_active_segment_count <= 0) return;
    if (fsm_get_state() != AURA_STATE_SPEAKING) return;
    if (!s_tts_stream_started) return;

    int64_t now = esp_timer_get_time() / 1000;
    if (s_active_segment_last_update_ms <= 0) {
        s_active_segment_last_update_ms = now;
    }
    if (!audio_is_non_music_playing()) {
        s_active_segment_last_update_ms = now;
        return;
    }

    int64_t delta_ms = now - s_active_segment_last_update_ms;
    s_active_segment_last_update_ms = now;
    if (delta_ms > 0 && delta_ms < 1000) {
        s_active_segment_elapsed_ms += (int)delta_ms;
    }

    int duration_ms = s_active_segment_ms[s_active_segment_index];
    while (s_active_segment_index + 1 < s_active_segment_count &&
           duration_ms > 0 && s_active_segment_elapsed_ms >= duration_ms) {
        s_active_segment_elapsed_ms -= duration_ms;
        s_active_segment_index++;
        aura_ui_set_dialogue(s_active_segments[s_active_segment_index], REPLY_DIALOGUE_TTL_TICKS);
        duration_ms = s_active_segment_ms[s_active_segment_index];
        ESP_LOGI(TAG, "Dialogue segmented advance %d/%d: %.40s",
                 s_active_segment_index + 1,
                 s_active_segment_count,
                 s_active_segments[s_active_segment_index]);
    }
    if (duration_ms > 0) {
        int progress = (int)(((int64_t)s_active_segment_elapsed_ms * 1000) / duration_ms);
        aura_ui_set_dialogue_progress(progress);
    }
}

static void ws_show_error_dialogue(const char *text)
{
    const char *msg = (text && text[0] != '\0') ? text : "出了点问题，你再试一次。";
    char tmp[512];
    ws_sanitize_dialogue_text(msg, tmp, sizeof(tmp));
    aura_ui_set_dialogue(tmp, 120);
    aura_ui_set_agent_visible(false);
}

static void ws_note_tts_started(void)
{
    if (s_tts_stream_started) return;
    s_tts_stream_started = true;
    s_tts_ui_pending = true;
    s_tts_playback_started_at_ms = esp_timer_get_time() / 1000;
    ESP_LOGI(TAG, "VOICE_TIMING playback_queued turn=%u stream=%d since_turn_start_ms=%lld since_first_tts_frame_ms=%lld since_first_pcm_queued_ms=%lld",
             (unsigned)s_active_tts_turn_id,
             s_active_tts_stream_id,
             (long long)(s_turn_started_at_ms > 0 ? s_tts_playback_started_at_ms - s_turn_started_at_ms : 0),
             (long long)(s_tts_first_frame_at_ms > 0 ? s_tts_playback_started_at_ms - s_tts_first_frame_at_ms : 0),
             (long long)(s_tts_first_pcm_queued_at_ms > 0 ? s_tts_playback_started_at_ms - s_tts_first_pcm_queued_at_ms : 0));
    ESP_LOGI(TAG, "TTS playback started (stream=%d)", s_active_tts_stream_id);
}

typedef struct {
    const char *id;
    const char *path;
} ws_local_audio_asset_t;

static const ws_local_audio_asset_t WS_LOCAL_AUDIO_ASSETS[] = {
    { "prompt_greeting_zh",   ASSETS_BASE_PATH "/sounds/prompt_greeting_zh.pcm" },
    { "prompt_greeting_en",   ASSETS_BASE_PATH "/sounds/prompt_greeting_en.pcm" },
    { "prompt_greeting_ja",   ASSETS_BASE_PATH "/sounds/prompt_greeting_ja.pcm" },
    { "prompt_clarify_zh",    ASSETS_BASE_PATH "/sounds/prompt_clarify_zh.pcm" },
    { "prompt_clarify_en",    ASSETS_BASE_PATH "/sounds/prompt_clarify_en.pcm" },
    { "prompt_clarify_ja",    ASSETS_BASE_PATH "/sounds/prompt_clarify_ja.pcm" },
    { "prompt_refuse_zh",     ASSETS_BASE_PATH "/sounds/prompt_refuse_zh.pcm" },
    { "prompt_refuse_en",     ASSETS_BASE_PATH "/sounds/prompt_refuse_en.pcm" },
    { "prompt_refuse_ja",     ASSETS_BASE_PATH "/sounds/prompt_refuse_ja.pcm" },
    { "prompt_background_zh", ASSETS_BASE_PATH "/sounds/prompt_bg_zh.pcm" },
    { "prompt_background_en", ASSETS_BASE_PATH "/sounds/prompt_bg_en.pcm" },
    { "prompt_background_ja", ASSETS_BASE_PATH "/sounds/prompt_bg_ja.pcm" },
    { "prompt_quota_zh",      ASSETS_BASE_PATH "/sounds/prompt_quota_zh.pcm" },
    { "prompt_quota_en",      ASSETS_BASE_PATH "/sounds/prompt_quota_en.pcm" },
    { "prompt_quota_ja",      ASSETS_BASE_PATH "/sounds/prompt_quota_ja.pcm" },
};

static bool ws_play_local_audio_id(const char *audio_id)
{
    if (!audio_id || audio_id[0] == '\0') return false;

    const char *path = NULL;
    for (size_t i = 0; i < sizeof(WS_LOCAL_AUDIO_ASSETS) / sizeof(WS_LOCAL_AUDIO_ASSETS[0]); ++i) {
        if (strcmp(audio_id, WS_LOCAL_AUDIO_ASSETS[i].id) == 0) {
            path = WS_LOCAL_AUDIO_ASSETS[i].path;
            break;
        }
    }
    if (!path) {
        ESP_LOGW(TAG, "Rejected unknown local audio id: %s", audio_id);
        return false;
    }

    audio_stop_playback();
    ws_reset_tts_stream();
    s_active_tts_stream_id = 1;
    s_active_tts_turn_id = s_turn_seq;
    s_tts_final_received = true;
    s_tts_active_until_ms = esp_timer_get_time() / 1000 + TTS_STREAM_ACTIVE_GRACE_MS;

    esp_err_t err = sfx_play_file(path);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Local audio unavailable id=%s path=%s err=0x%x", audio_id, path, err);
        ws_reset_tts_stream();
        return false;
    }

    int64_t now_ms = esp_timer_get_time() / 1000;
    s_last_tts_chunk_ms = now_ms;
    s_tts_first_pcm_queued_at_ms = now_ms;
    ws_note_tts_started();
    ESP_LOGI(TAG, "Local prompt queued id=%s path=%s", audio_id, path);
    return true;
}

static void ws_prepare_tts_stream(int stream_id)
{
    if (stream_id <= 0) stream_id = 1;
    if (s_active_tts_stream_id == stream_id) return;

    ESP_LOGI(TAG, "Starting TTS stream %d", stream_id);
    audio_stop_playback();
    s_active_tts_stream_id = stream_id;
    s_active_tts_turn_id = s_turn_seq;
    s_tts_stream_started = false;
    s_tts_active_until_ms = esp_timer_get_time() / 1000 + TTS_STREAM_ACTIVE_GRACE_MS;
    ws_reset_tts_opus_decoder();
}

static esp_err_t ws_append_tts_prefetch(const uint8_t *pcm, size_t len, int stream_id)
{
    if (!pcm || len == 0) {
        return ESP_OK;
    }

    if (s_tts_prefetch_stream_id != stream_id) {
        ws_clear_tts_prefetch();
        s_tts_prefetch_stream_id = stream_id;
    }

    size_t required = s_tts_prefetch_len + len;
    if (required > s_tts_prefetch_cap) {
        size_t new_cap = s_tts_prefetch_cap ? s_tts_prefetch_cap : TTS_PREFETCH_BYTES;
        while (new_cap < required) {
            new_cap *= 2;
        }
        uint8_t *new_buf = heap_caps_malloc(new_cap, MALLOC_CAP_SPIRAM);
        if (!new_buf) {
            ESP_LOGE(TAG, "Failed to alloc TTS prefetch buffer: %u bytes", (unsigned)new_cap);
            return ESP_ERR_NO_MEM;
        }
        if (s_tts_prefetch_buf && s_tts_prefetch_len > 0) {
            memcpy(new_buf, s_tts_prefetch_buf, s_tts_prefetch_len);
            heap_caps_free(s_tts_prefetch_buf);
        }
        s_tts_prefetch_buf = new_buf;
        s_tts_prefetch_cap = new_cap;
    }

    memcpy(s_tts_prefetch_buf + s_tts_prefetch_len, pcm, len);
    s_tts_prefetch_len += len;
    s_last_tts_chunk_ms = esp_timer_get_time() / 1000;
    return ESP_OK;
}

static esp_err_t ws_flush_tts_prefetch(int stream_id)
{
    if (!s_tts_prefetch_buf || s_tts_prefetch_len == 0) {
        return ESP_OK;
    }

    ws_prepare_tts_stream(stream_id);
    esp_err_t ret = s_tts_final_received
        ? audio_queue_pcm_copy_tail_source(s_tts_prefetch_buf, s_tts_prefetch_len, AUDIO_PLAYBACK_SOURCE_DEFAULT)
        : audio_queue_pcm_copy(s_tts_prefetch_buf, s_tts_prefetch_len);
    if (ret == ESP_OK) {
        int64_t now_ms = esp_timer_get_time() / 1000;
        if (s_tts_first_pcm_queued_at_ms == 0) {
            s_tts_first_pcm_queued_at_ms = now_ms;
            audio_debug_mark_tts_turn(
                s_active_tts_turn_id ? s_active_tts_turn_id : s_turn_seq,
                s_turn_started_at_ms,
                s_tts_first_pcm_queued_at_ms
            );
            ESP_LOGI(TAG, "VOICE_TIMING first_pcm_queued turn=%u stream=%d since_turn_start_ms=%lld since_first_tts_frame_ms=%lld bytes=%u final=%d threshold=%u",
                     (unsigned)(s_active_tts_turn_id ? s_active_tts_turn_id : s_turn_seq),
                     stream_id,
                     (long long)(s_turn_started_at_ms > 0 ? now_ms - s_turn_started_at_ms : 0),
                     (long long)(s_tts_first_frame_at_ms > 0 ? now_ms - s_tts_first_frame_at_ms : 0),
                     (unsigned)s_tts_prefetch_len,
                     s_tts_final_received ? 1 : 0,
                     (unsigned)TTS_PREFETCH_BYTES);
        }
        s_tts_active_until_ms = esp_timer_get_time() / 1000 + TTS_STREAM_ACTIVE_GRACE_MS;
        ws_note_tts_started();
        s_last_tts_chunk_ms = esp_timer_get_time() / 1000;
        ESP_LOGI(TAG, "TTS prefetch flushed: stream=%d bytes=%u final=%d",
                 stream_id, (unsigned)s_tts_prefetch_len, s_tts_final_received ? 1 : 0);
    }
    ws_clear_tts_prefetch();
    return ret;
}

static esp_err_t ws_queue_tts_pcm_chunk(const uint8_t *pcm, size_t len, int stream_id, bool replace_playback, bool is_final)
{
    bool final_chunk = is_final;
    if (is_final) {
        s_tts_final_received = true;
    }

    if (!pcm || len == 0) {
        if (!replace_playback && is_final) {
            return ws_flush_tts_prefetch(stream_id);
        }
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t ret;
    if (replace_playback) {
        audio_stop_playback();
        ws_reset_tts_stream();
        s_tts_final_received = final_chunk;
        ret = is_final
            ? audio_queue_pcm_copy_tail_source(pcm, len, AUDIO_PLAYBACK_SOURCE_DEFAULT)
            : audio_play_pcm_copy(pcm, len);
        if (ret == ESP_OK) {
            int64_t now_ms = esp_timer_get_time() / 1000;
            if (s_tts_first_pcm_queued_at_ms == 0) {
                s_tts_first_pcm_queued_at_ms = now_ms;
                audio_debug_mark_tts_turn(
                    s_active_tts_turn_id ? s_active_tts_turn_id : s_turn_seq,
                    s_turn_started_at_ms,
                    s_tts_first_pcm_queued_at_ms
                );
                ESP_LOGI(TAG, "VOICE_TIMING first_pcm_queued turn=%u stream=%d since_turn_start_ms=%lld since_first_tts_frame_ms=%lld bytes=%u final=%d replace=1",
                         (unsigned)(s_active_tts_turn_id ? s_active_tts_turn_id : s_turn_seq),
                         stream_id,
                         (long long)(s_turn_started_at_ms > 0 ? now_ms - s_turn_started_at_ms : 0),
                         (long long)(s_tts_first_frame_at_ms > 0 ? now_ms - s_tts_first_frame_at_ms : 0),
                         (unsigned)len,
                         is_final ? 1 : 0);
            }
            s_tts_active_until_ms = esp_timer_get_time() / 1000 + TTS_STREAM_ACTIVE_GRACE_MS;
            ws_note_tts_started();
            s_last_tts_chunk_ms = esp_timer_get_time() / 1000;
        }
    } else if (!s_tts_stream_started) {
        if (s_active_tts_stream_id != stream_id) {
            audio_stop_playback();
            ws_reset_tts_stream();
            s_tts_final_received = final_chunk;
            s_active_tts_stream_id = stream_id;
        }
        s_tts_active_until_ms = esp_timer_get_time() / 1000 + TTS_STREAM_ACTIVE_GRACE_MS;
        ret = ws_append_tts_prefetch(pcm, len, stream_id);
        if (ret == ESP_OK && (s_tts_prefetch_len >= TTS_PREFETCH_BYTES || is_final)) {
            ret = ws_flush_tts_prefetch(stream_id);
        }
    } else {
        s_tts_active_until_ms = esp_timer_get_time() / 1000 + TTS_STREAM_ACTIVE_GRACE_MS;
        ret = ws_append_tts_prefetch(pcm, len, stream_id);
        if (ret == ESP_OK && (s_tts_prefetch_len >= TTS_STREAM_QUEUE_BYTES || is_final)) {
            ret = ws_flush_tts_prefetch(stream_id);
        }
    }

    return ret;
}

static esp_err_t ws_queue_tts_base64_chunk(const char *audio_b64, int stream_id, bool replace_playback, bool is_final)
{
    if (!audio_b64 || audio_b64[0] == '\0') {
        return ws_queue_tts_pcm_chunk(NULL, 0, stream_id, replace_playback, is_final);
    }

    size_t audio_len = strlen(audio_b64);
    size_t out_len = 0;
    int rc = mbedtls_base64_decode(NULL, 0, &out_len,
                                   (const unsigned char *)audio_b64, audio_len);
    if (!(rc == MBEDTLS_ERR_BASE64_BUFFER_TOO_SMALL || out_len > 0)) {
        ESP_LOGW(TAG, "TTS base64 size probe failed: %d", rc);
        return ESP_FAIL;
    }

    uint8_t *pcm = heap_caps_malloc(out_len, MALLOC_CAP_SPIRAM);
    if (!pcm) {
        ESP_LOGW(TAG, "No memory for TTS PCM buffer: %u", (unsigned)out_len);
        return ESP_ERR_NO_MEM;
    }

    rc = mbedtls_base64_decode(
        pcm, out_len, &out_len,
        (const unsigned char *)audio_b64, audio_len
    );
    if (rc != 0) {
        ESP_LOGW(TAG, "TTS base64 decode failed: %d", rc);
        heap_caps_free(pcm);
        return ESP_FAIL;
    }

    esp_err_t ret = ws_queue_tts_pcm_chunk(pcm, out_len, stream_id, replace_playback, is_final);
    heap_caps_free(pcm);
    return ret;
}

static esp_err_t ws_queue_tts_opus_chunk(const uint8_t *opus, size_t len, int stream_id, bool replace_playback, bool is_final)
{
    if (!opus || len == 0) {
        return ws_queue_tts_pcm_chunk(NULL, 0, stream_id, replace_playback, is_final);
    }
    if (ws_ensure_tts_opus_decoder() != ESP_OK) {
        return ESP_ERR_NO_MEM;
    }

    const int16_t *pcm = NULL;
    size_t pcm_samples = 0;
    if (!aura_opus_decoder_decode(s_tts_opus_decoder, opus, len, &pcm, &pcm_samples)) {
        ESP_LOGW(TAG, "Failed to decode TTS Opus packet: %u bytes", (unsigned)len);
        return ESP_FAIL;
    }
    if (!pcm || pcm_samples == 0) {
        return is_final ? ws_queue_tts_pcm_chunk(NULL, 0, stream_id, replace_playback, true) : ESP_OK;
    }

    return ws_queue_tts_pcm_chunk(
        (const uint8_t *)pcm,
        pcm_samples * sizeof(int16_t),
        stream_id,
        replace_playback,
        is_final
    );
}

static uint32_t ws_read_le32(const uint8_t *p)
{
    return ((uint32_t)p[0]) |
           ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static void ws_handle_tts_binary_frame(const uint8_t *data, size_t len)
{
    if (!data || len < TTS_BINARY_HEADER_SIZE) {
        ESP_LOGW(TAG, "Ignoring short binary TTS frame: %u bytes", (unsigned)len);
        return;
    }
    if (memcmp(data, TTS_BINARY_MAGIC, 4) != 0) {
        ESP_LOGW(TAG, "Ignoring unknown binary WS frame");
        return;
    }

    int stream = (int)ws_read_le32(data + 4);
    uint32_t turn_id = ws_read_le32(data + 8);
    uint8_t flags = data[12];
    bool is_final = (flags & TTS_BINARY_FLAG_FINAL) != 0;
    bool is_opus = (flags & TTS_BINARY_FLAG_OPUS) != 0;
    const uint8_t *audio = data + TTS_BINARY_HEADER_SIZE;
    size_t audio_len = len - TTS_BINARY_HEADER_SIZE;

    static uint32_t s_tts_binary_log_counter = 0;
    s_tts_binary_log_counter++;
    if (is_final || s_tts_binary_log_counter == 1 || (s_tts_binary_log_counter % 100) == 0) {
        ESP_LOGI(TAG, "TTS binary frame stream=%d bytes=%u final=%d opus=%d count=%u",
                 stream, (unsigned)audio_len, is_final ? 1 : 0, is_opus ? 1 : 0,
                 (unsigned)s_tts_binary_log_counter);
    }

    if (!ws_turn_matches(turn_id)) {
        ESP_LOGW(TAG, "Ignoring binary TTS from stale turn %u (current=%u)",
                 (unsigned)turn_id, (unsigned)s_turn_seq);
        return;
    }

    s_active_tts_turn_id = turn_id ? turn_id : s_turn_seq;
    if (audio_len > 0 && s_tts_first_frame_at_ms == 0) {
        s_tts_first_frame_at_ms = esp_timer_get_time() / 1000;
        s_tts_first_frame_bytes = audio_len;
        ESP_LOGI(TAG, "VOICE_TIMING first_tts_frame turn=%u stream=%d since_turn_start_ms=%lld bytes=%u opus=%d final=%d",
                 (unsigned)s_active_tts_turn_id,
                 stream,
                 (long long)(s_turn_started_at_ms > 0 ? s_tts_first_frame_at_ms - s_turn_started_at_ms : 0),
                 (unsigned)audio_len,
                 is_opus ? 1 : 0,
                 is_final ? 1 : 0);
    }
    if (is_final && s_tts_final_frame_at_ms == 0) {
        s_tts_final_frame_at_ms = esp_timer_get_time() / 1000;
    }
    if (audio_len > 0 || is_final) {
        esp_err_t ret = is_opus
            ? ws_queue_tts_opus_chunk(audio, audio_len, stream, false, is_final)
            : ws_queue_tts_pcm_chunk(audio, audio_len, stream, false, is_final);
        if (ret != ESP_OK) {
            play_error_sfx_throttled();
        }
    }
    if (is_final) {
        ESP_LOGI(TAG, "VOICE_TIMING tts_final_frame turn=%u stream=%d since_turn_start_ms=%lld since_first_tts_frame_ms=%lld first_frame_bytes=%u",
                 (unsigned)s_active_tts_turn_id,
                 stream,
                 (long long)(s_turn_started_at_ms > 0 ? s_tts_final_frame_at_ms - s_turn_started_at_ms : 0),
                 (long long)(s_tts_first_frame_at_ms > 0 ? s_tts_final_frame_at_ms - s_tts_first_frame_at_ms : 0),
                 (unsigned)s_tts_first_frame_bytes);
        ESP_LOGI(TAG, "TTS binary stream %d marked final", stream);
    }
}

static void ws_handle_binary_ws_data(const uint8_t *data, int data_len, int payload_len, int payload_offset)
{
    if (!data || data_len <= 0 || payload_len <= 0) {
        return;
    }

    if (payload_len > WS_MAX_INBOUND_MESSAGE_BYTES) {
        ESP_LOGW(TAG, "Binary WS payload too large: %d bytes (max=%d), discarding",
                 payload_len, WS_MAX_INBOUND_MESSAGE_BYTES);
        ws_clear_binary_reassembly();
        return;
    }

    if (payload_len <= data_len && payload_offset == 0) {
        ws_handle_tts_binary_frame(data, (size_t)data_len);
        return;
    }

    if (payload_offset == 0) {
        ws_clear_binary_reassembly();
        s_binary_reassembly_total = payload_len;
        s_binary_reassembly_pos = 0;
        s_binary_reassembly_buf = heap_caps_malloc((size_t)payload_len, MALLOC_CAP_SPIRAM);
        if (!s_binary_reassembly_buf) {
            ESP_LOGE(TAG, "No PSRAM for binary reassembly: %d bytes", payload_len);
            s_binary_reassembly_total = 0;
            return;
        }
    }

    if (!s_binary_reassembly_buf ||
        payload_len != s_binary_reassembly_total ||
        payload_offset != s_binary_reassembly_pos ||
        s_binary_reassembly_pos + data_len > s_binary_reassembly_total) {
        ESP_LOGW(TAG, "Binary reassembly mismatch, discarding payload=%d offset=%d len=%d pos=%d total=%d",
                 payload_len, payload_offset, data_len,
                 s_binary_reassembly_pos, s_binary_reassembly_total);
        ws_clear_binary_reassembly();
        return;
    }

    memcpy(s_binary_reassembly_buf + s_binary_reassembly_pos, data, (size_t)data_len);
    s_binary_reassembly_pos += data_len;
    if (s_binary_reassembly_pos >= s_binary_reassembly_total) {
        ws_handle_tts_binary_frame(s_binary_reassembly_buf, (size_t)s_binary_reassembly_total);
        ws_clear_binary_reassembly();
    }
}

static void ws_free_queued_event(ws_queued_event_t *event)
{
    if (event && event->data) {
        heap_caps_free(event->data);
        event->data = NULL;
    }
}

static void ws_drain_event_queue(void)
{
    if (!s_ws_event_queue) {
        return;
    }

    ws_queued_event_t event;
    while (xQueueReceive(s_ws_event_queue, &event, 0) == pdTRUE) {
        esp_websocket_event_data_t ws_data = {0};
        ws_data.op_code = event.op_code;
        ws_data.data_len = event.data_len;
        ws_data.payload_len = event.payload_len;
        ws_data.payload_offset = event.payload_offset;
        ws_data.data_ptr = (char *)event.data;
        ws_data.error_handle.esp_tls_last_esp_err = event.esp_tls_last_esp_err;
        ws_process_queued_event(event.id, &ws_data);
        ws_free_queued_event(&event);
    }
}

static void ws_drop_queued_events(void)
{
    if (!s_ws_event_queue) {
        return;
    }

    ws_queued_event_t event;
    while (xQueueReceive(s_ws_event_queue, &event, 0) == pdTRUE) {
        ws_free_queued_event(&event);
    }
}

static void ws_event_handler(void *arg, esp_event_base_t base,
                             int32_t id, void *data)
{
    (void)arg;
    (void)base;
    esp_websocket_event_data_t *ws_data = (esp_websocket_event_data_t *)data;

    /*
     * Keep high-frequency binary TTS frames on the websocket client task.
     * Text/control events are queued to network_task so FSM/UI updates have a
     * single owner, but moving the binary audio parser there overflows the
     * smaller network task stack during Opus TTS streaming.
     */
    if (id == WEBSOCKET_EVENT_DATA && ws_data &&
        (ws_data->op_code == 2 || (ws_data->op_code == 0 && s_binary_reassembly_buf)) &&
        ws_data->data_len > 0) {
        ws_handle_binary_ws_data(
            (const uint8_t *)ws_data->data_ptr,
            ws_data->data_len,
            ws_data->payload_len,
            ws_data->payload_offset
        );
        return;
    }

    ws_queued_event_t event = {
        .id = id,
        .op_code = ws_data ? ws_data->op_code : 0,
        .data_len = ws_data ? ws_data->data_len : 0,
        .payload_len = ws_data ? ws_data->payload_len : 0,
        .payload_offset = ws_data ? ws_data->payload_offset : 0,
        .esp_tls_last_esp_err = ws_data ? ws_data->error_handle.esp_tls_last_esp_err : 0,
        .data = NULL,
    };

    if (event.data_len > 0 && ws_data && ws_data->data_ptr) {
        if (event.data_len > WS_MAX_INBOUND_MESSAGE_BYTES) {
            ESP_LOGW(TAG, "Dropping oversized WS frame chunk in callback: %d bytes", event.data_len);
            return;
        }
        event.data = heap_caps_malloc((size_t)event.data_len + 1, MALLOC_CAP_SPIRAM);
        if (!event.data) {
            ESP_LOGE(TAG, "No PSRAM for queued WS event: %d bytes", event.data_len);
            return;
        }
        memcpy(event.data, ws_data->data_ptr, (size_t)event.data_len);
        event.data[event.data_len] = '\0';
    }

    if (!s_ws_event_queue || xQueueSend(s_ws_event_queue, &event, 0) != pdTRUE) {
        ESP_LOGW(TAG, "WS event queue full or unavailable, dropping event id=%ld", (long)id);
        ws_free_queued_event(&event);
    }
}

/* ================================================================== */
/*  Init / Connect / Loop                                             */
/* ================================================================== */

esp_err_t ws_client_init(const char *uri)
{
    ws_init_device_identity();
    const char *effective_uri = (uri && uri[0] != '\0') ? uri : s_ws_uri;
    ws_uri_parts_t uri_parts;
    esp_err_t uri_err = ws_parse_uri_parts(effective_uri, &uri_parts);
    if (uri_err != ESP_OK) {
        ESP_LOGE(TAG, "Invalid WebSocket URI: %s", effective_uri ? effective_uri : "(null)");
        return uri_err;
    }
    if (effective_uri && effective_uri[0] != '\0') {
        strncpy(s_ws_uri, effective_uri, sizeof(s_ws_uri) - 1);
        s_ws_uri[sizeof(s_ws_uri) - 1] = '\0';
    }
    strncpy(s_ws_host, uri_parts.host, sizeof(s_ws_host) - 1);
    s_ws_host[sizeof(s_ws_host) - 1] = '\0';
    strncpy(s_ws_path, uri_parts.path, sizeof(s_ws_path) - 1);
    s_ws_path[sizeof(s_ws_path) - 1] = '\0';
    if (!s_ws_event_queue) {
        s_ws_event_queue = xQueueCreate(WS_EVENT_QUEUE_DEPTH, sizeof(ws_queued_event_t));
        if (!s_ws_event_queue) {
            ESP_LOGE(TAG, "Failed to create WS event queue");
            return ESP_FAIL;
        }
    } else {
        ws_drop_queued_events();
    }
    bool use_tls = uri_parts.tls;
    ws_log_heap_snapshot("init");

    esp_websocket_client_config_t cfg = {
        .uri                  = s_ws_uri,
        .host                 = use_tls ? s_ws_host : NULL,
        .port                 = use_tls ? uri_parts.port : 0,
        .path                 = use_tls ? s_ws_path : NULL,
        .buffer_size          = 16384,
        .task_stack           = WS_CLIENT_TASK_STACK_BYTES,
        .reconnect_timeout_ms = 5000,
        .network_timeout_ms   = 45000,
        .disable_pingpong_discon = true,   /* Cloudflare 吞 PONG，不按 PONG 缺失判断断连 */
        .ping_interval_sec    = 15,        /* 周期 PING 保活：喂网关 20s ping 窗口 + 撑住 NAT，
                                              防止 TCP 空闲卡死十几秒后被网关踢下线 */
        .task_prio            = 8,
        .transport            = use_tls ? WEBSOCKET_TRANSPORT_OVER_SSL : WEBSOCKET_TRANSPORT_OVER_TCP,
    };

    if (use_tls) {
        // Cloudflare 对 ESP32 默认 TLS 配置比较挑剔，这里显式固定为 TLS1.2。
        esp_transport_handle_t ssl = esp_transport_ssl_init();
        if (!ssl) {
            ESP_LOGE(TAG, "Failed to init SSL transport");
            return ESP_FAIL;
        }
        esp_transport_ssl_set_tls_version(ssl, ESP_TLS_VER_TLS_1_2);
        esp_transport_ssl_set_common_name(ssl, s_ws_host);
        esp_transport_ssl_crt_bundle_attach(ssl, esp_crt_bundle_attach);
        s_wss_transport = esp_transport_ws_init(ssl);
        if (!s_wss_transport) {
            ESP_LOGE(TAG, "Failed to init WSS transport");
            esp_transport_destroy(ssl);
            return ESP_FAIL;
        }
        esp_transport_ws_set_path(s_wss_transport, s_ws_path);
        cfg.ext_transport = s_wss_transport;
        cfg.cert_common_name = s_ws_host;
        cfg.skip_cert_common_name_check = false;
        cfg.crt_bundle_attach = esp_crt_bundle_attach;
    }

    s_client = esp_websocket_client_init(&cfg);
    if (!s_client) {
        ESP_LOGE(TAG, "Failed to init WebSocket client");
        return ESP_FAIL;
    }

    esp_websocket_register_events(s_client, WEBSOCKET_EVENT_ANY,
                                   ws_event_handler, NULL);
    ESP_LOGI(TAG, "WebSocket client initialized: %s", s_ws_uri);
    return ESP_OK;
}

esp_err_t ws_client_connect(void)
{
    ws_log_heap_snapshot("connect");
    s_last_connect_attempt_ms = esp_timer_get_time() / 1000;
    return esp_websocket_client_start(s_client);
}

void ws_client_loop(void)
{
    if (s_ota_paused) return;
    if (s_ota_resume_after_ms != 0) {
        int64_t now_ms = esp_timer_get_time() / 1000;
        if (now_ms < s_ota_resume_after_ms) return;
        s_ota_resume_after_ms = 0;
        (void)ws_force_rebuild("OTA completed");
        return;
    }
    ws_drain_event_queue();

    if (!s_connected) {
        if (wifi_manager_is_connected()) {
            int64_t now = esp_timer_get_time() / 1000;
            int64_t stalled_since = s_last_disconnect_ms > 0 ? s_last_disconnect_ms : s_last_connect_attempt_ms;
            bool client_thinks_connected = (s_client != NULL) && esp_websocket_client_is_connected(s_client);
            if (!client_thinks_connected &&
                stalled_since > 0 &&
                (now - stalled_since) >= WS_STALL_RECOVERY_MS) {
                ws_force_rebuild("stalled while Wi-Fi is up");
            }
        }
        return;
    }

    int64_t now = esp_timer_get_time() / 1000;

    /* 心跳 */
    if (now - s_last_heartbeat > HEARTBEAT_INTERVAL_MS) {
        char buf[256];
        s_heartbeat_seq++;
        snprintf(
            buf,
            sizeof(buf),
            "{\"type\":\"heartbeat\",\"payload\":{\"device_id\":\"%s\",\"boot_id\":\"%s\",\"connection_seq\":%u,\"seq\":%u,\"state\":\"%s\"}}",
            s_device_id,
            s_boot_id,
            (unsigned)s_connection_seq,
            (unsigned)s_heartbeat_seq,
            ws_state_name()
        );
        ws_client_send_text(buf);
        s_last_heartbeat = now;
    }

    /* 仅记录异常慢回复，不在本地制造“假超时”覆盖真实回复 */
    if (s_pending_dialogue && (now - s_dialogue_recv_ms > DIALOGUE_TTS_TIMEOUT_MS)) {
        ESP_LOGW(TAG, "TTS still pending after %d ms", DIALOGUE_TTS_TIMEOUT_MS);
    }

    ws_update_active_dialogue_segment();
}

bool ws_client_is_connected(void)
{
    return s_connected;
}

bool ws_client_is_ready(void)
{
    return s_connected && s_handshake_ready;
}

bool ws_client_is_tts_active(void)
{
    return (esp_timer_get_time() / 1000) < s_tts_active_until_ms;
}

bool ws_client_take_server_vad_stop(void)
{
    bool stopped = s_server_vad_stop_received;
    s_server_vad_stop_received = false;
    return stopped;
}

void ws_client_cancel_pending_reply(void)
{
    s_waiting_send_ack = false;
    s_reply_in_flight = false;
    ws_clear_pending_dialogue();
    ws_reset_tts_stream();
    ws_clear_text_reassembly();
    ws_clear_binary_reassembly();
    audio_stop_playback();
}

void ws_client_on_audio_loop(void)
{
    if (s_active_tts_turn_id != 0 && !ws_turn_matches(s_active_tts_turn_id)) {
        ESP_LOGW(TAG, "Stopping stale TTS playback for turn %u (current=%u)",
                 (unsigned)s_active_tts_turn_id, (unsigned)s_turn_seq);
        audio_stop_playback();
        ws_reset_tts_stream();
        ws_clear_binary_reassembly();
        return;
    }

    if (s_tts_ui_pending && audio_is_non_music_playing()) {
        s_tts_ui_pending = false;
        if (fsm_get_state() == AURA_STATE_PROCESSING || s_reply_in_flight) {
            fsm_handle_event(AURA_EVT_RESPONSE_TEXT);
        }
        ws_apply_pending_dialogue();
    }

    ws_update_active_dialogue_segment();

    int64_t now_ms = esp_timer_get_time() / 1000;
    if (s_tts_stream_started && s_tts_final_received && !audio_is_non_music_playing() &&
        (now_ms - s_last_tts_chunk_ms) >= TTS_DONE_SETTLE_MS) {
        if (fsm_get_state() == AURA_STATE_SPEAKING) {
            bool continue_listening = s_active_continue_listening_after_tts;
            ESP_LOGI(TAG, "TTS done settle continue=%d turn=%u",
                     continue_listening ? 1 : 0,
                     (unsigned)s_active_tts_turn_id);
            ESP_LOGI(TAG, "VOICE_TIMING playback_done turn=%u since_turn_start_ms=%lld since_playback_started_ms=%lld since_first_tts_frame_ms=%lld continue=%d",
                     (unsigned)s_active_tts_turn_id,
                     (long long)(s_turn_started_at_ms > 0 ? now_ms - s_turn_started_at_ms : 0),
                     (long long)(s_tts_playback_started_at_ms > 0 ? now_ms - s_tts_playback_started_at_ms : 0),
                     (long long)(s_tts_first_frame_at_ms > 0 ? now_ms - s_tts_first_frame_at_ms : 0),
                     continue_listening ? 1 : 0);
            if (continue_listening) {
                ws_clear_active_dialogue_segments();
                aura_ui_clear_dialogue();
            } else {
                aura_ui_limit_dialogue_ttl(REPLY_DIALOGUE_DONE_TICKS);
            }
            fsm_handle_event(continue_listening
                             ? AURA_EVT_TTS_DONE_CONTINUE
                             : AURA_EVT_TTS_DONE);
        }
        s_reply_in_flight = false;
        ws_reset_tts_stream();
        ws_clear_binary_reassembly();
    }
}

const char *ws_client_device_id(void)
{
    return s_device_id;
}

const char *ws_client_boot_id(void)
{
    return s_boot_id;
}

void ws_client_set_language(const char *language)
{
    const char *normalized = "zh";
    if (language && strcmp(language, "en") == 0) {
        normalized = "en";
    } else if (language && strcmp(language, "ja") == 0) {
        normalized = "ja";
    }
    if (strcmp(s_language, normalized) == 0) {
        return;
    }
    strlcpy(s_language, normalized, sizeof(s_language));
    ESP_LOGI(TAG, "Conversation language changed to %s", s_language);
    if (s_connected) {
        ws_send_hello();
    }
}

/* ================================================================== */
/*  Send helpers                                                      */
/* ================================================================== */

static esp_err_t ws_client_send_text_timeout(const char *text, int timeout_ms)
{
    if (!s_connected || !s_client) return ESP_ERR_INVALID_STATE;
    int ret = esp_websocket_client_send_text(s_client, text,
                                              strlen(text), pdMS_TO_TICKS(timeout_ms));
    return (ret >= 0) ? ESP_OK : ESP_FAIL;
}

esp_err_t ws_client_send_text(const char *text)
{
    return ws_client_send_text_timeout(text, WS_SEND_TIMEOUT_MS);
}

esp_err_t ws_client_send_dessert_purchase(const char *item_id, int beans)
{
    if (!item_id || item_id[0] == '\0') return ESP_ERR_INVALID_ARG;
    cJSON *root = cJSON_CreateObject();
    cJSON *payload = cJSON_AddObjectToObject(root, "payload");
    if (!root || !payload) {
        cJSON_Delete(root);
        return ESP_ERR_NO_MEM;
    }
    cJSON_AddStringToObject(root, "type", "dessert_purchase");
    cJSON_AddStringToObject(payload, "item_id", item_id);
    cJSON_AddNumberToObject(payload, "beans", ws_clamp_beans(beans));
    cJSON_AddStringToObject(payload, "device_id", s_device_id);
    cJSON_AddStringToObject(payload, "boot_id", s_boot_id);
    cJSON_AddNumberToObject(payload, "connection_seq", (double)s_connection_seq);
    char *text = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!text) return ESP_ERR_NO_MEM;
    s_dessert_purchase_pending = true;
    esp_err_t ret = ws_client_send_text_timeout(text, WS_CONTROL_SEND_TIMEOUT_MS);
    if (ret != ESP_OK) s_dessert_purchase_pending = false;
    cJSON_free(text);
    return ret;
}

bool ws_client_dessert_purchase_pending(void)
{
    return s_dessert_purchase_pending;
}

esp_err_t ws_client_send_start_with_server_vad(bool server_vad_enabled)
{
    ESP_LOGI(TAG, ">>> start (opus, 16kHz, server_vad=%d)", server_vad_enabled ? 1 : 0);
    char buf[512];
    ws_begin_new_turn();
    s_turn_seq++;
    s_turn_started_at_ms = esp_timer_get_time() / 1000;
    if (s_device_public_ip[0] != '\0') {
        snprintf(
            buf,
            sizeof(buf),
            "{\"type\":\"start\",\"sample_rate\":16000,\"format\":\"opus\",\"frame_duration\":60,"
            "\"device_public_ip\":\"%s\",\"language\":\"%s\","
            "\"payload\":{\"device_id\":\"%s\",\"boot_id\":\"%s\",\"connection_seq\":%u,\"turn_id\":%u,"
            "\"server_vad\":%s,\"device_public_ip\":\"%s\"}}",
            s_device_public_ip,
            s_language,
            s_device_id,
            s_boot_id,
            (unsigned)s_connection_seq,
            (unsigned)s_turn_seq,
            server_vad_enabled ? "true" : "false",
            s_device_public_ip
        );
    } else {
        snprintf(
            buf,
            sizeof(buf),
            "{\"type\":\"start\",\"sample_rate\":16000,\"format\":\"opus\",\"frame_duration\":60,\"language\":\"%s\","
            "\"payload\":{\"device_id\":\"%s\",\"boot_id\":\"%s\",\"connection_seq\":%u,\"turn_id\":%u,\"server_vad\":%s}}",
            s_language,
            s_device_id,
            s_boot_id,
            (unsigned)s_connection_seq,
            (unsigned)s_turn_seq,
            server_vad_enabled ? "true" : "false"
        );
    }
    esp_err_t ret = ws_client_send_text_timeout(buf, WS_CONTROL_SEND_TIMEOUT_MS);
    if (ret != ESP_OK) {
        play_error_sfx_throttled();
    } else {
        ESP_LOGI(TAG, "VOICE_TIMING listen_start_sent turn=%u server_vad=%d",
                 (unsigned)s_turn_seq,
                 server_vad_enabled ? 1 : 0);
    }
    return ret;
}

esp_err_t ws_client_send_start(void)
{
    return ws_client_send_start_with_server_vad(AURA_SERVER_VAD_DEFAULT);
}

esp_err_t ws_client_send_stop(void)
{
    ESP_LOGI(TAG, ">>> stop");
    char buf[256];
    snprintf(
        buf,
        sizeof(buf),
        "{\"type\":\"stop\",\"payload\":{\"device_id\":\"%s\",\"boot_id\":\"%s\",\"connection_seq\":%u,\"turn_id\":%u}}",
        s_device_id,
        s_boot_id,
        (unsigned)s_connection_seq,
        (unsigned)s_turn_seq
    );
    esp_err_t ret = ws_client_send_text_timeout(buf, WS_CONTROL_SEND_TIMEOUT_MS);
    if (ret == ESP_OK) {
        s_waiting_send_ack = true;
        s_reply_in_flight = true;
    } else {
        s_waiting_send_ack = false;
        s_reply_in_flight = false;
        play_error_sfx_throttled();
    }
    return ret;
}

esp_err_t ws_client_send_cancel(const char *reason)
{
    const char *safe_reason = reason && reason[0] ? reason : "client_cancel";
    ESP_LOGW(TAG, ">>> cancel (%s)", safe_reason);
    char buf[320];
    snprintf(
        buf,
        sizeof(buf),
        "{\"type\":\"cancel\",\"payload\":{\"device_id\":\"%s\",\"boot_id\":\"%s\",\"connection_seq\":%u,\"turn_id\":%u,\"reason\":\"%s\"}}",
        s_device_id,
        s_boot_id,
        (unsigned)s_connection_seq,
        (unsigned)s_turn_seq,
        safe_reason
    );
    s_waiting_send_ack = false;
    s_reply_in_flight = false;
    return ws_client_send_text_timeout(buf, WS_CONTROL_SEND_TIMEOUT_MS);
}

esp_err_t ws_client_send_pcm(const uint8_t *pcm, size_t len)
{
    if (!s_connected || !s_client) {
        ESP_LOGW(TAG, "send_pcm: not connected (connected=%d, client=%p)", s_connected, s_client);
        return ESP_ERR_INVALID_STATE;
    }

    for (int attempt = 1; attempt <= WS_AUDIO_SEND_RETRIES; attempt++) {
        int64_t send_start_ms = esp_timer_get_time() / 1000;
        int ret = esp_websocket_client_send_bin(s_client, (const char *)pcm,
                                                 len, pdMS_TO_TICKS(WS_AUDIO_SEND_TIMEOUT_MS));
        int64_t elapsed_ms = (esp_timer_get_time() / 1000) - send_start_ms;
        if (ret == (int)len) {
            if (elapsed_ms >= WS_AUDIO_SEND_SLOW_WARN_MS) {
                ESP_LOGW(TAG,
                         "VOICE_TIMING audio_packet_send_slow attempt=%d bytes=%d ms=%lld",
                         attempt, (int)len, (long long)elapsed_ms);
            }
            return ESP_OK;
        }

        ESP_LOGW(TAG,
                 "send_pcm attempt %d/%d failed: ret=%d len=%d connected=%d ms=%lld",
                 attempt, WS_AUDIO_SEND_RETRIES, ret, (int)len, s_connected ? 1 : 0,
                 (long long)elapsed_ms);
        if (!s_connected || !s_client) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(WS_AUDIO_SEND_RETRY_DELAY_MS * attempt));
    }

    play_error_sfx_throttled();
    return ESP_FAIL;
}

esp_err_t ws_client_send_button(button_event_t evt)
{
    char buf[128];
    const char *btn_name = "unknown";
    switch (evt) {
        case BTN_EVENT_KEY_SHORT:  btn_name = "key_short";  break;
        case BTN_EVENT_KEY_LONG:   btn_name = "key_long";   break;
        case BTN_EVENT_BOOT_SHORT: btn_name = "boot_short"; break;
        default: break;
    }
    snprintf(buf, sizeof(buf),
             "{\"type\":\"button_press\",\"payload\":{\"button\":\"%s\"}}",
             btn_name);
    return ws_client_send_text(buf);
}

esp_err_t ws_client_send_gpio_diag(int pin, int old_level, int new_level)
{
    char buf[160];
    snprintf(buf, sizeof(buf),
             "{\"type\":\"gpio_diag\",\"payload\":{\"pin\":%d,\"old\":%d,\"new\":%d}}",
             pin, old_level, new_level);
    return ws_client_send_text(buf);
}

esp_err_t ws_client_send_gpio_snapshot(const char *label, int key_level, int boot_level)
{
    char buf[192];
    snprintf(buf, sizeof(buf),
             "{\"type\":\"gpio_snapshot\",\"payload\":{\"label\":\"%s\",\"key\":%d,\"boot\":%d}}",
             label ? label : "snapshot", key_level, boot_level);
    return ws_client_send_text(buf);
}

/* ================================================================== */
/*  Event handler                                                     */
/* ================================================================== */

static void ws_process_queued_event(int32_t id, esp_websocket_event_data_t *ws_data)
{
    bool was_ready = s_connected || s_handshake_ready || g_state.ws_connected;

    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        ESP_LOGI(TAG, "Connected to backend");
        ws_log_heap_snapshot("connected");
        s_connection_seq++;
        s_connected          = true;
        s_last_disconnect_ms = 0;
        s_handshake_ready    = false;
        s_waiting_send_ack   = false;
        s_reply_in_flight    = false;
        ws_clear_pending_dialogue();
        ws_reset_tts_stream();
        ws_clear_text_reassembly();
        ws_clear_binary_reassembly();
        aura_ui_set_ws_connected(true);
        if (fsm_get_state() != AURA_STATE_PROCESSING)
            aura_ui_set_agent_visible(false);
        ws_show_connection_notice("连接中...");

        /* hello handshake — 设备身份 + 会话信息 + 音频能力 */
        ws_send_hello();
        break;

    case WEBSOCKET_EVENT_DISCONNECTED:
    {
        int64_t now_ms = esp_timer_get_time() / 1000;
        s_last_disconnect_ms = now_ms;
        bool media_active = ws_tts_or_audio_active();
        if (media_active) {
            s_connection_sfx_silent_until_ms =
                now_ms + CONNECTION_SFX_SUPPRESS_AFTER_TTS_DISCONNECT_MS;
        }
        ESP_LOGW(TAG, "Disconnected");
        ws_log_heap_snapshot("disconnected");
        s_connected          = false;
        s_handshake_ready    = false;
        s_waiting_send_ack   = false;
        s_reply_in_flight    = false;
        ws_clear_pending_dialogue();
        ws_reset_tts_stream();
        ws_clear_text_reassembly();
        ws_clear_binary_reassembly();
        aura_ui_set_ws_connected(false);
        aura_ui_set_agent_visible(false);
        ws_show_connection_notice("已断开，重连中...");
        if (was_ready) {
            ESP_LOGW(TAG, "Connection dropped; reconnecting silently");
        }

        /*
         * Link drops can happen immediately after the gateway has sent audio.
         * Do not inject an error chirp or abort the speaker while the DAC is
         * draining queued TTS; that was audible as a harsh tail glitch.
         */
        if (!media_active && fsm_get_state() != AURA_STATE_IDLE)
            fsm_handle_event(AURA_EVT_ABORT);
        break;
    }

    case WEBSOCKET_EVENT_DATA:
        if ((ws_data->op_code == 2 || (ws_data->op_code == 0 && s_binary_reassembly_buf)) &&
            ws_data->data_len > 0) {
            ws_handle_binary_ws_data(
                (const uint8_t *)ws_data->data_ptr,
                ws_data->data_len,
                ws_data->payload_len,
                ws_data->payload_offset
            );
            break;
        }
        /*
         * WebSocket text can arrive as an initial text frame(op_code=1)
         * followed by continuation frames(op_code=0). Full-size tts_audio
         * replies hit this path; if we ignore continuation frames, dialogue
         * arrives but audio never completes.
         */
        if ((ws_data->op_code == 1 || ws_data->op_code == 0) && ws_data->data_len > 0) {
            if (ws_data->payload_len <= 0 || ws_data->payload_len > WS_MAX_INBOUND_MESSAGE_BYTES) {
                ESP_LOGW(TAG, "Text WS payload invalid or too large: %d bytes (max=%d), discarding",
                         ws_data->payload_len, WS_MAX_INBOUND_MESSAGE_BYTES);
                ws_clear_text_reassembly();
                break;
            }
            if (ws_data->payload_len <= ws_data->data_len && ws_data->payload_offset == 0) {
                handle_server_message(ws_data->data_ptr, ws_data->data_len);
            } else {
                if (ws_data->payload_offset == 0) {
                    ws_clear_text_reassembly();
                    s_reassembly_total = ws_data->payload_len;
                    s_reassembly_pos   = 0;
                    s_reassembly_buf   = heap_caps_malloc(s_reassembly_total + 1, MALLOC_CAP_SPIRAM);
                    if (!s_reassembly_buf) {
                        ESP_LOGE(TAG, "No PSRAM for reassembly: %d bytes", s_reassembly_total);
                        break;
                    }
                }
                if (s_reassembly_buf && s_reassembly_pos + ws_data->data_len <= s_reassembly_total) {
                    memcpy(s_reassembly_buf + s_reassembly_pos, ws_data->data_ptr, ws_data->data_len);
                    s_reassembly_pos += ws_data->data_len;
                    if (s_reassembly_pos >= s_reassembly_total) {
                        s_reassembly_buf[s_reassembly_total] = '\0';
                        ESP_LOGD(TAG, "Reassembled %d bytes", s_reassembly_total);
                        handle_server_message(s_reassembly_buf, s_reassembly_total);
                        ws_clear_text_reassembly();
                    }
                } else {
                    ESP_LOGW(TAG, "Reassembly overflow, discarding");
                    ws_clear_text_reassembly();
                }
            }
        }
        break;

    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGE(TAG, "WebSocket error: esp_tls_last_error=%d",
                 ws_data->error_handle.esp_tls_last_esp_err);
        ws_log_heap_snapshot("error");
        if (!s_connected) {
            s_last_disconnect_ms = esp_timer_get_time() / 1000;
        }
        ws_show_connection_notice("连接中...");
        break;
    }
}

/* ================================================================== */
/*  Parse incoming server messages                                    */
/* ================================================================== */

static void handle_server_message(const char *json_str, int len)
{
    /*
     * Server 可能回复:
     *  1. JSON: {"type":"message","sender":"AI","text":"..."}
     *  2. JSON: {"type":"status","text":"AI thinking..."}
     *  3. JSON: {"type":"emotion","emotion":"..."}
     *  4. JSON: {"type":"hello","status":"ready"}
     *  5. Plain text: "Command recognized: ...", "ASR Error", etc.
     *
     * 对于 voice pipeline 我们最关心的是 (1):
     *   当 FSM 处于 PROCESSING 且收到 sender==AI 的 message → SPEAKING
     */

    /*
     * Fast path: 大型 tts_audio 消息不走 cJSON（避免 DRAM OOM）
     * 直接从原始 JSON 字符串中提取 base64 音频
     */
    if (len > 4096 && strstr(json_str, "\"tts_audio\"")) {
        const char *turn_tag = strstr(json_str, "\"turn_id\":");
        if (turn_tag) {
            uint32_t turn_id = (uint32_t)strtoul(turn_tag + strlen("\"turn_id\":"), NULL, 10);
            if (!ws_turn_matches(turn_id)) {
                ESP_LOGW(TAG, "Ignoring legacy TTS audio from stale turn %u (current=%u)",
                         (unsigned)turn_id, (unsigned)s_turn_seq);
                return;
            }
        }
        /* 验证是 PCM 格式 */
        if (!strstr(json_str, "\"pcm\"")) {
            ESP_LOGW(TAG, "TTS audio not PCM format, skipping");
            return;
        }
        /* 定位 "audio":"<base64>" 中的 base64 起始 */
        const char *needle = "\"audio\":\"";
        const char *audio_start = strstr(json_str, needle);
        if (!audio_start) {
            needle = "\"audio\": \"";
            audio_start = strstr(json_str, needle);
        }
        if (audio_start) {
            audio_start += strlen(needle);
            const char *audio_end = strchr(audio_start, '"');
            if (audio_end && audio_end > audio_start) {
                size_t b64_len = audio_end - audio_start;
                size_t out_len = 0;
                int rc = mbedtls_base64_decode(NULL, 0, &out_len,
                    (const unsigned char *)audio_start, b64_len);
                if ((rc == MBEDTLS_ERR_BASE64_BUFFER_TOO_SMALL || out_len > 0) && out_len < 512000) {
                    uint8_t *pcm = heap_caps_malloc(out_len, MALLOC_CAP_SPIRAM);
                    if (pcm) {
                        rc = mbedtls_base64_decode(pcm, out_len, &out_len,
                            (const unsigned char *)audio_start, b64_len);
                        if (rc == 0) {
                            ESP_LOGI(TAG, "TTS fast-path: %u bytes PCM", (unsigned)out_len);
                            audio_stop_playback();
                            ws_reset_tts_stream();
                            ws_clear_binary_reassembly();
                            s_active_tts_turn_id = s_turn_seq;
                            if (audio_play_pcm_copy(pcm, out_len) == ESP_OK) {
                                ws_note_tts_started();
                            } else {
                                play_error_sfx_throttled();
                            }
                        } else {
                            ESP_LOGW(TAG, "TTS fast-path base64 decode failed: %d", rc);
                            play_error_sfx_throttled();
                        }
                        heap_caps_free(pcm);
                    } else {
                        ESP_LOGW(TAG, "TTS fast-path: no PSRAM for %u bytes", (unsigned)out_len);
                        play_error_sfx_throttled();
                    }
                } else {
                    ESP_LOGW(TAG, "TTS fast-path: bad b64 size probe (rc=%d, out=%u)", rc, (unsigned)out_len);
                }
                return;
            }
        }
        ESP_LOGW(TAG, "TTS fast-path: could not locate audio field");
        return;
    }

    /* 尝试 JSON 解析 (小消息走 cJSON) */
    cJSON *root = cJSON_ParseWithLength(json_str, len);

    if (!root) {
        /* 不是 JSON — 纯文本回复 (例如 "Command recognized: ...")  */
        ESP_LOGI(TAG, "<<< plain: %.*s", len, json_str);

        /* 如果是 AI 回复的纯文本，仍然显示到屏幕 */
        if (fsm_get_state() == AURA_STATE_PROCESSING) {
            size_t copy_len = (size_t)len;
            if (copy_len >= sizeof(g_state.display_text))
                copy_len = sizeof(g_state.display_text) - 1;
            {
                char tmp[512];
                memcpy(tmp, json_str, copy_len);
                tmp[copy_len] = '\0';
                ws_buffer_pending_dialogue(tmp, -1, -1, 0, s_turn_seq, false);
            }
            ws_set_agent_progress(82, text_has_non_ascii(json_str) ? "正在生成语音" : "reply ready");
        }
        return;
    }

    /* 有效 JSON */
    const cJSON *type_j = cJSON_GetObjectItem(root, "type");
    const char  *mtype  = (type_j && cJSON_IsString(type_j)) ? type_j->valuestring : "";

    if (strcmp(mtype, "tts_audio_chunk") == 0) {
        ESP_LOGD(TAG, "<<< json type=%s", mtype);
    } else {
        ESP_LOGI(TAG, "<<< json type=%s", mtype);
    }

    /* ── message: AI 回复 ──────────────────── */
    if (strcmp(mtype, "message") == 0) {
        const cJSON *sender = cJSON_GetObjectItem(root, "sender");
        const cJSON *text   = cJSON_GetObjectItem(root, "text");

        if (text && cJSON_IsString(text)) {
            if (sender && cJSON_IsString(sender) &&
                strcmp(sender->valuestring, "AI") == 0 &&
                fsm_get_state() == AURA_STATE_PROCESSING) {
                ws_buffer_pending_dialogue(text->valuestring, -1, -1, 0, s_turn_seq, false);
                ws_set_agent_progress(82, "正在生成语音");
            } else if (fsm_get_state() != AURA_STATE_LISTENING) {
                aura_ui_set_dialogue(text->valuestring, REPLY_DIALOGUE_TTL_TICKS);
            }
        }
    }

    /* ── status: 状态文本 ("AI thinking...") ── */
    else if (strcmp(mtype, "status") == 0) {
        const cJSON *text = cJSON_GetObjectItem(root, "text");
        if (text && cJSON_IsString(text)) {
            ESP_LOGI(TAG, "Status: %s", text->valuestring);
            if (fsm_get_state() == AURA_STATE_PROCESSING) {
                int progress = g_state.agent_progress + 12;
                if (progress > 79) progress = 79;
                ws_set_agent_progress(progress, text_has_non_ascii(text->valuestring)
                                                ? text->valuestring : "思考中");
            }
        }
    }

    /* ── status_update: Aura companion 数值状态 ── */
    else if (strcmp(mtype, "status_update") == 0) {
        const cJSON *payload = cJSON_GetObjectItem(root, "payload");
        if (payload) {
            const cJSON *mood = cJSON_GetObjectItem(payload, "mood");
            const cJSON *energy = cJSON_GetObjectItem(payload, "energy");
            const cJSON *satiety = cJSON_GetObjectItem(payload, "satiety");
            const cJSON *beans = cJSON_GetObjectItem(payload, "beans");
            const cJSON *affinity_xp = cJSON_GetObjectItem(payload, "affinity_xp");
            const cJSON *affinity_level = cJSON_GetObjectItem(payload, "affinity_level");
            const cJSON *coins = cJSON_GetObjectItem(payload, "coins");
            const cJSON *scene = cJSON_GetObjectItem(payload, "scene");
            const cJSON *world_slot = cJSON_GetObjectItem(payload, "world_slot");
            const cJSON *outfit_mode = cJSON_GetObjectItem(payload, "outfit_mode");
            const cJSON *weather_temperature = cJSON_GetObjectItem(payload, "weather_temperature");
            const cJSON *weather_icon = cJSON_GetObjectItem(payload, "weather_icon");
            const cJSON *weather_city = cJSON_GetObjectItem(payload, "weather_city");
            const cJSON *quota = cJSON_GetObjectItem(payload, "quota");
            const cJSON *notes = cJSON_GetObjectItem(payload, "notes");

            if (mood && cJSON_IsNumber(mood))
                g_state.mood = ws_clamp_companion_stat(mood->valueint);
            if (energy && cJSON_IsNumber(energy))
                g_state.energy = ws_clamp_companion_stat(energy->valueint);
            if (satiety && cJSON_IsNumber(satiety))
                g_state.satiety = ws_clamp_companion_stat(satiety->valueint);
            if (affinity_xp && cJSON_IsNumber(affinity_xp))
                g_state.affinity = affinity_xp->valueint;
            if (affinity_level && cJSON_IsNumber(affinity_level))
                g_state.affinity_level = ws_clamp_affinity_level(affinity_level->valueint);
            if (beans && cJSON_IsNumber(beans))
                g_state.coins = ws_clamp_beans(beans->valueint);
            if (coins && cJSON_IsNumber(coins))
                g_state.coins = ws_clamp_beans(coins->valueint);
            if (scene && cJSON_IsString(scene)) {
                int next_scene = msg_scene_to_index(scene->valuestring);
                if (next_scene != g_state.current_scene) {
                    g_state.current_scene = next_scene;
                    aura_ui_reset_idle_surface();
                }
            }
            if (world_slot && cJSON_IsString(world_slot)) {
                g_state.world_sleeping = strcmp(world_slot->valuestring, "sleep") == 0;
            }
            if (outfit_mode && cJSON_IsString(outfit_mode)) {
                if (strcmp(outfit_mode->valuestring, "sleepwear") == 0) {
                    g_state.world_sleepwear_requested = true;
                    g_state.world_outfit_mode_ready = true;
                } else if (strcmp(outfit_mode->valuestring, "daywear") == 0) {
                    g_state.world_sleepwear_requested = false;
                    g_state.world_outfit_mode_ready = true;
                }
            }
            /* Outfit ownership/current outfit are now device-local until the
             * server speaks the same numeric outfit protocol. Do not let the
             * legacy string field ("default"/"sleepwear_basic") overwrite
             * user-selected shop outfits on every status_update. */
            if (weather_temperature && cJSON_IsNumber(weather_temperature)) {
                g_state.temperature = (float)weather_temperature->valuedouble;
                g_state.weather_valid = true;
            }
            if (weather_icon && cJSON_IsNumber(weather_icon)) {
                g_state.weather_icon = weather_icon->valueint;
                g_state.weather_valid = true;
            }
            if (weather_city && cJSON_IsString(weather_city)) {
                strlcpy(g_state.weather_city, weather_city->valuestring,
                        sizeof(g_state.weather_city));
            }
            if (notes && cJSON_IsArray(notes)) {
                for (int i = 0; i < 3; ++i) {
                    const cJSON *note = cJSON_GetArrayItem(notes, i);
                    if (note && cJSON_IsString(note)) {
                        strlcpy(g_state.info_notes[i], note->valuestring,
                                sizeof(g_state.info_notes[i]));
                    } else {
                        g_state.info_notes[i][0] = '\0';
                    }
                }
            }

            if (quota && cJSON_IsObject(quota)) {
                const cJSON *provider = cJSON_GetObjectItem(quota, "provider");
                const cJSON *provider_display = cJSON_GetObjectItem(quota, "provider_display");
                const cJSON *plan_display = cJSON_GetObjectItem(quota, "plan_display");
                const cJSON *headline = cJSON_GetObjectItem(quota, "headline");
                const cJSON *headline_short = cJSON_GetObjectItem(quota, "headline_short");
                const cJSON *summary_percent = cJSON_GetObjectItem(quota, "summary_percent");
                const cJSON *summary_display = cJSON_GetObjectItem(quota, "summary_display");
                const cJSON *primary = cJSON_GetObjectItem(quota, "primary");
                const cJSON *secondary = cJSON_GetObjectItem(quota, "secondary");
                const cJSON *primary_label = primary ? cJSON_GetObjectItem(primary, "label") : NULL;
                const cJSON *primary_display = primary ? cJSON_GetObjectItem(primary, "display") : NULL;
                const cJSON *primary_limit = primary ? cJSON_GetObjectItem(primary, "limit") : NULL;
                const cJSON *primary_remaining = primary ? cJSON_GetObjectItem(primary, "remaining") : NULL;
                const cJSON *secondary_label = secondary ? cJSON_GetObjectItem(secondary, "label") : NULL;
                const cJSON *secondary_display = secondary ? cJSON_GetObjectItem(secondary, "display") : NULL;
                const cJSON *secondary_limit = secondary ? cJSON_GetObjectItem(secondary, "limit") : NULL;
                const cJSON *secondary_remaining = secondary ? cJSON_GetObjectItem(secondary, "remaining") : NULL;

                if (provider && cJSON_IsString(provider)) {
                    strncpy(g_state.quota_provider, provider->valuestring, sizeof(g_state.quota_provider) - 1);
                    g_state.quota_provider[sizeof(g_state.quota_provider) - 1] = '\0';
                } else {
                    g_state.quota_provider[0] = '\0';
                }
                if (plan_display && cJSON_IsString(plan_display) && plan_display->valuestring) {
                    strncpy(g_state.quota_headline, plan_display->valuestring, sizeof(g_state.quota_headline) - 1);
                    g_state.quota_headline[sizeof(g_state.quota_headline) - 1] = '\0';
                } else if (headline_short && cJSON_IsString(headline_short) && headline_short->valuestring) {
                    strncpy(g_state.quota_headline, headline_short->valuestring, sizeof(g_state.quota_headline) - 1);
                    g_state.quota_headline[sizeof(g_state.quota_headline) - 1] = '\0';
                } else if (headline && cJSON_IsString(headline) && headline->valuestring) {
                    strncpy(g_state.quota_headline, headline->valuestring, sizeof(g_state.quota_headline) - 1);
                    g_state.quota_headline[sizeof(g_state.quota_headline) - 1] = '\0';
                } else if (provider_display && cJSON_IsString(provider_display) && provider_display->valuestring) {
                    strncpy(g_state.quota_headline, provider_display->valuestring, sizeof(g_state.quota_headline) - 1);
                    g_state.quota_headline[sizeof(g_state.quota_headline) - 1] = '\0';
                } else if (provider && cJSON_IsString(provider) && provider->valuestring) {
                    strncpy(g_state.quota_headline, provider->valuestring, sizeof(g_state.quota_headline) - 1);
                    g_state.quota_headline[sizeof(g_state.quota_headline) - 1] = '\0';
                } else {
                    g_state.quota_headline[0] = '\0';
                }
                if (summary_percent && cJSON_IsNumber(summary_percent))
                    g_state.quota_percent = summary_percent->valueint;
                else
                    g_state.quota_percent = 0;
                if (summary_display && cJSON_IsString(summary_display)) {
                    strncpy(g_state.quota_text, summary_display->valuestring, sizeof(g_state.quota_text) - 1);
                    g_state.quota_text[sizeof(g_state.quota_text) - 1] = '\0';
                } else {
                    g_state.quota_text[0] = '\0';
                }

                if (primary_label && cJSON_IsString(primary_label)) {
                    strncpy(g_state.quota_primary_label, primary_label->valuestring, sizeof(g_state.quota_primary_label) - 1);
                    g_state.quota_primary_label[sizeof(g_state.quota_primary_label) - 1] = '\0';
                } else {
                    g_state.quota_primary_label[0] = '\0';
                }
                if (primary_display && cJSON_IsString(primary_display) && primary_display->valuestring) {
                    strncpy(g_state.quota_primary_text, primary_display->valuestring, sizeof(g_state.quota_primary_text) - 1);
                    g_state.quota_primary_text[sizeof(g_state.quota_primary_text) - 1] = '\0';
                } else if (primary_label && cJSON_IsString(primary_label) && primary_label->valuestring) {
                    strncpy(g_state.quota_primary_text, primary_label->valuestring, sizeof(g_state.quota_primary_text) - 1);
                    g_state.quota_primary_text[sizeof(g_state.quota_primary_text) - 1] = '\0';
                } else {
                    g_state.quota_primary_text[0] = '\0';
                }
                if (primary_limit && cJSON_IsNumber(primary_limit) &&
                    primary_remaining && cJSON_IsNumber(primary_remaining) &&
                    primary_limit->valuedouble > 0.0) {
                    g_state.quota_primary_percent =
                        (int)((primary_remaining->valuedouble / primary_limit->valuedouble) * 100.0 + 0.5);
                    if (primary_label && cJSON_IsString(primary_label) &&
                        strcmp(primary_label->valuestring, "AURA") == 0) {
                        snprintf(g_state.quota_primary_text, sizeof(g_state.quota_primary_text),
                                 "AURA %.0f/%.0f", primary_remaining->valuedouble,
                                 primary_limit->valuedouble);
                    }
                } else {
                    g_state.quota_primary_percent = 0;
                }

                /* Older gateways sent "AURA + KIMI" as the title. The
                 * device keeps the compact title even while such a gateway
                 * is being restarted, so the panel cannot regress. */
                if (primary_label && cJSON_IsString(primary_label) &&
                    strcmp(primary_label->valuestring, "AURA") == 0) {
                    strlcpy(g_state.quota_headline, "AURA", sizeof(g_state.quota_headline));
                }

                if (secondary_label && cJSON_IsString(secondary_label)) {
                    strncpy(g_state.quota_secondary_label, secondary_label->valuestring, sizeof(g_state.quota_secondary_label) - 1);
                    g_state.quota_secondary_label[sizeof(g_state.quota_secondary_label) - 1] = '\0';
                } else {
                    g_state.quota_secondary_label[0] = '\0';
                }
                if (secondary_display && cJSON_IsString(secondary_display) && secondary_display->valuestring) {
                    strncpy(g_state.quota_secondary_text, secondary_display->valuestring, sizeof(g_state.quota_secondary_text) - 1);
                    g_state.quota_secondary_text[sizeof(g_state.quota_secondary_text) - 1] = '\0';
                } else if (secondary_label && cJSON_IsString(secondary_label) && secondary_label->valuestring) {
                    strncpy(g_state.quota_secondary_text, secondary_label->valuestring, sizeof(g_state.quota_secondary_text) - 1);
                    g_state.quota_secondary_text[sizeof(g_state.quota_secondary_text) - 1] = '\0';
                } else {
                    g_state.quota_secondary_text[0] = '\0';
                }
                if (secondary_limit && cJSON_IsNumber(secondary_limit) &&
                    secondary_remaining && cJSON_IsNumber(secondary_remaining) &&
                    secondary_limit->valuedouble > 0.0) {
                    g_state.quota_secondary_percent =
                        (int)((secondary_remaining->valuedouble / secondary_limit->valuedouble) * 100.0 + 0.5);
                } else {
                    g_state.quota_secondary_percent = 0;
                }
                bool secondary_is_five_hour =
                    (secondary_label && cJSON_IsString(secondary_label) &&
                     (strcmp(secondary_label->valuestring, "5h") == 0 ||
                      strcmp(secondary_label->valuestring, "five_hour") == 0)) ||
                    (secondary_display && cJSON_IsString(secondary_display) &&
                     (strstr(secondary_display->valuestring, "5h") ||
                      strstr(secondary_display->valuestring, "5 ") ||
                      strstr(secondary_display->valuestring, "5小时")));
                if (secondary_is_five_hour) {
                    int display_percent = g_state.quota_secondary_percent;
                    if ((!secondary_limit || !cJSON_IsNumber(secondary_limit) ||
                         !secondary_remaining || !cJSON_IsNumber(secondary_remaining)) &&
                        secondary_display && cJSON_IsString(secondary_display)) {
                        (void)ws_quota_ratio_percent(secondary_display->valuestring, &display_percent);
                    }
                    g_state.quota_secondary_percent = display_percent;
                    snprintf(g_state.quota_secondary_text, sizeof(g_state.quota_secondary_text),
                             "5h %d%%", display_percent);
                }
                g_state.quota_ready = true;
            }
            g_state.companion_state_ready = true;
            aura_companion_state_cache_save();

            ESP_LOGI(TAG, "Aura status updated: mood=%d energy=%d satiety=%d affinity=%d lv=%d beans=%d",
                     g_state.mood, g_state.energy, g_state.satiety, g_state.affinity, g_state.affinity_level, g_state.coins);
            aura_ui_mark_dirty();
        }
    }

    /* ── companion_action: 即时世界行为结果（甜品等） ── */
    else if (strcmp(mtype, "companion_action") == 0) {
        const cJSON *payload = cJSON_GetObjectItem(root, "payload");
        const cJSON *action = payload ? cJSON_GetObjectItem(payload, "action") : NULL;
        const cJSON *status = payload ? cJSON_GetObjectItem(payload, "status") : NULL;
        if (payload && action && cJSON_IsString(action) &&
            strcmp(action->valuestring, "dessert_purchase") == 0) {
            const cJSON *beans = cJSON_GetObjectItem(payload, "beans");
            const cJSON *effects = cJSON_GetObjectItem(payload, "effects");
            s_dessert_purchase_pending = false;
            if (status && cJSON_IsString(status) && strcmp(status->valuestring, "purchased") == 0) {
                if (beans && cJSON_IsNumber(beans))
                    g_state.coins = ws_clamp_beans(beans->valueint);
                if (effects && cJSON_IsObject(effects)) {
                    const cJSON *mood = cJSON_GetObjectItem(effects, "mood");
                    const cJSON *energy = cJSON_GetObjectItem(effects, "energy");
                    const cJSON *satiety = cJSON_GetObjectItem(effects, "satiety");
                    if (mood && cJSON_IsNumber(mood))
                        g_state.mood = ws_clamp_companion_stat(g_state.mood + mood->valueint);
                    if (energy && cJSON_IsNumber(energy))
                        g_state.energy = ws_clamp_companion_stat(g_state.energy + energy->valueint);
                    if (satiety && cJSON_IsNumber(satiety))
                        g_state.satiety = ws_clamp_companion_stat(g_state.satiety + satiety->valueint);
                }
                g_state.companion_state_ready = true;
                aura_companion_state_cache_save();
                const char *ok_text = g_state.ui_language == 1
                    ? "Dessert enjoyed. Feeling better~"
                    : g_state.ui_language == 2
                    ? "おやつで気分が上がったよ~"
                    : "甜品到账，心情变好了~";
                aura_ui_set_dialogue(ok_text, 25);
            } else {
                if (beans && cJSON_IsNumber(beans))
                    g_state.coins = ws_clamp_beans(beans->valueint);
                const char *failed_text = g_state.ui_language == 1
                    ? "Dessert purchase failed"
                    : g_state.ui_language == 2
                    ? "おやつの購入に失敗しました"
                    : "甜品购买失败";
                aura_ui_set_dialogue(failed_text, 25);
            }
            aura_ui_mark_dirty();
        }
    }

    /* ── companion_settlement: 任务结算面板 ── */
    else if (strcmp(mtype, "companion_settlement") == 0) {
        const cJSON *payload = cJSON_GetObjectItem(root, "payload");
        if (payload) {
            const cJSON *beans = cJSON_GetObjectItem(payload, "beans_delta");
            const cJSON *energy = cJSON_GetObjectItem(payload, "energy_delta");
            const cJSON *mood = cJSON_GetObjectItem(payload, "mood_delta");
            const cJSON *duration = cJSON_GetObjectItem(payload, "duration_seconds");
            int beans_v = (beans && cJSON_IsNumber(beans)) ? beans->valueint : 0;
            int energy_v = (energy && cJSON_IsNumber(energy)) ? energy->valueint : 0;
            int mood_v = (mood && cJSON_IsNumber(mood)) ? mood->valueint : 0;
            int duration_v = (duration && cJSON_IsNumber(duration)) ? duration->valueint : 0;

            /* Store settlement deltas for UI card display */
            g_state.settle_beans_delta = beans_v;
            g_state.settle_energy_delta = energy_v;
            g_state.settle_mood_delta = mood_v;
            g_state.settle_duration = duration_v;

            /* Bypass FSM guard — settlement can arrive in any state */
            {
                char summary[48];
                snprintf(summary, sizeof(summary), "+%d豆 体%d 心%+d %ds",
                         beans_v, energy_v, mood_v, duration_v);
                aura_ui_set_agent_panel(true, 100, "SETTLE", summary);
            }
            aura_ui_mark_dirty();
        }
    }

    /* ── emotion ───────────────────────────── */
    else if (strcmp(mtype, "emotion") == 0) {
        const cJSON *emo = cJSON_GetObjectItem(root, "emotion");
        if (emo && cJSON_IsString(emo)) {
            ESP_LOGI(TAG, "Emotion: %s", emo->valuestring);
            /* Store emotion for portrait display */
            strncpy(g_state.current_emotion, emo->valuestring,
                    sizeof(g_state.current_emotion) - 1);
            g_state.current_emotion[sizeof(g_state.current_emotion) - 1] = '\0';
            aura_ui_mark_dirty();
            if (strcmp(emo->valuestring, "sent") == 0 &&
                fsm_get_state() == AURA_STATE_PROCESSING) {
                g_state.current_pose = 4;
                ws_set_agent_progress(35, "已发送");
            } else if (strcmp(emo->valuestring, "reply") == 0) {
                ws_set_agent_progress(82, "语音准备中...");
            }
        }
    }

    /* ── dialogue (旧协议，保持兼容) ───────── */
    else if (strcmp(mtype, "dialogue") == 0) {
        const cJSON *payload = cJSON_GetObjectItem(root, "payload");
        if (payload) {
            const cJSON *text  = cJSON_GetObjectItem(payload, "text");
            const cJSON *pose  = cJSON_GetObjectItem(payload, "pose");
            const cJSON *scene = cJSON_GetObjectItem(payload, "scene");
            const cJSON *coins = cJSON_GetObjectItem(payload, "coins_earned");
            const cJSON *continue_item = cJSON_GetObjectItem(payload, "continue_listening");
            const cJSON *deferred_item = cJSON_GetObjectItem(payload, "deferred");
            const cJSON *local_audio_item = cJSON_GetObjectItem(payload, "local_audio_id");
            bool was_processing = (fsm_get_state() == AURA_STATE_PROCESSING);
            int pose_idx = -1;
            int scene_idx = -1;
            int coins_earned = 0;
            uint32_t turn_id = ws_message_turn_id(payload);
            bool continue_listening = continue_item && cJSON_IsBool(continue_item) && cJSON_IsTrue(continue_item);
            bool deferred_reply = deferred_item && cJSON_IsBool(deferred_item) && cJSON_IsTrue(deferred_item);

            if (!ws_turn_matches(turn_id)) {
                ESP_LOGW(TAG, "Ignoring dialogue from stale turn %u (current=%u)",
                         (unsigned)turn_id, (unsigned)s_turn_seq);
                cJSON_Delete(root);
                return;
            }

            if (pose && cJSON_IsString(pose))
                pose_idx = msg_pose_to_index(pose->valuestring);
            if (scene && cJSON_IsString(scene))
                scene_idx = msg_scene_to_index(scene->valuestring);
            if (coins && cJSON_IsNumber(coins))
                coins_earned = coins->valueint;

            if (text && cJSON_IsString(text))
                ws_buffer_pending_dialogue(text->valuestring, pose_idx, scene_idx, coins_earned, turn_id, continue_listening);
            else
                ws_buffer_pending_dialogue("", pose_idx, scene_idx, coins_earned, turn_id, continue_listening);
            ws_load_pending_dialogue_segments(payload);
            bool local_audio_started = false;
            if (local_audio_item && cJSON_IsString(local_audio_item)) {
                local_audio_started = ws_play_local_audio_id(local_audio_item->valuestring);
                if (!local_audio_started) {
                    ws_clear_pending_dialogue();
                    s_reply_in_flight = false;
                    ws_show_error_dialogue("本地提示音缺失，请更新后重试。");
                }
            }
            if (deferred_reply) {
                s_reply_in_flight = true;
            }
            ws_set_agent_progress(82, local_audio_started ? "正在播放" : "正在合成语音");

            /*
             * 不立刻 dirty/刷屏 — 等 tts_audio 到了再一起显示
             * 这样用户看到文字和听到语音是同步的
             */
            ESP_LOGI(TAG, "Dialogue buffered (waiting for TTS): %.40s...",
                     s_pending_text);
            ESP_LOGI(TAG, "Dialogue flags: turn=%u deferred=%d continue=%d state=%s reply_in_flight=%d",
                     (unsigned)turn_id,
                     deferred_reply ? 1 : 0,
                     continue_listening ? 1 : 0,
                     fsm_state_name(fsm_get_state()),
                     s_reply_in_flight ? 1 : 0);

            if (!was_processing) {
                /* IDLE 状态下仅忽略真正的主动欢迎语；迟到回复仍然接住 */
                if (fsm_get_state() == AURA_STATE_IDLE && !s_reply_in_flight && !deferred_reply) {
                    ws_clear_pending_dialogue();
                } else if (fsm_get_state() != AURA_STATE_IDLE) {
                    ws_apply_pending_dialogue();
                }
            }
        }
    }

    else if (strcmp(mtype, "dialogue_segment") == 0) {
        const cJSON *payload = cJSON_GetObjectItem(root, "payload");
        ws_note_dialogue_segment_timing(payload);
    }

    /* ── tts_audio_chunk: 分块语音回复 ──── */
    else if (strcmp(mtype, "tts_audio_chunk") == 0) {
        const cJSON *payload = cJSON_GetObjectItem(root, "payload");
        if (payload) {
            const cJSON *audio = cJSON_GetObjectItem(payload, "audio");
            const cJSON *format = cJSON_GetObjectItem(payload, "format");
            const cJSON *stream_id = cJSON_GetObjectItem(payload, "stream_id");
            const cJSON *is_final = cJSON_GetObjectItem(payload, "is_final");
            int stream = (stream_id && cJSON_IsNumber(stream_id)) ? stream_id->valueint : 0;
            uint32_t turn_id = ws_message_turn_id(payload);

            if (!ws_turn_matches(turn_id)) {
                ESP_LOGW(TAG, "Ignoring TTS chunk from stale turn %u (current=%u)",
                         (unsigned)turn_id, (unsigned)s_turn_seq);
                cJSON_Delete(root);
                return;
            }
            s_active_tts_turn_id = turn_id ? turn_id : s_turn_seq;

            if (audio && cJSON_IsString(audio) &&
                format && cJSON_IsString(format) &&
                strcmp(format->valuestring, "pcm") == 0 &&
                audio->valuestring[0] != '\0') {
                if (ws_queue_tts_base64_chunk(
                        audio->valuestring,
                        stream,
                        false,
                        is_final && cJSON_IsBool(is_final) && cJSON_IsTrue(is_final)
                    ) != ESP_OK) {
                    play_error_sfx_throttled();
                }
            } else if (is_final && cJSON_IsBool(is_final) && cJSON_IsTrue(is_final)) {
                if (ws_queue_tts_base64_chunk("", stream, false, true) != ESP_OK) {
                    play_error_sfx_throttled();
                }
            }

            if (is_final && cJSON_IsBool(is_final) && cJSON_IsTrue(is_final)) {
                ESP_LOGI(TAG, "TTS stream %d marked final", stream);
            }
        }
    }

    /* ── tts_audio: 语音回复 ────────────── */
    else if (strcmp(mtype, "tts_audio") == 0) {
        const cJSON *payload = cJSON_GetObjectItem(root, "payload");
        if (payload) {
            const cJSON *audio = cJSON_GetObjectItem(payload, "audio");
            const cJSON *format = cJSON_GetObjectItem(payload, "format");
            uint32_t turn_id = ws_message_turn_id(payload);
            if (!ws_turn_matches(turn_id)) {
                ESP_LOGW(TAG, "Ignoring TTS audio from stale turn %u (current=%u)",
                         (unsigned)turn_id, (unsigned)s_turn_seq);
                cJSON_Delete(root);
                return;
            }
            s_active_tts_turn_id = turn_id ? turn_id : s_turn_seq;
            if (audio && cJSON_IsString(audio) &&
                format && cJSON_IsString(format) &&
                strcmp(format->valuestring, "pcm") == 0) {
                if (ws_queue_tts_base64_chunk(audio->valuestring, 0, true, true) != ESP_OK) {
                    play_error_sfx_throttled();
                }
            }
        }
    }

    /* ── asr_result: 识别结果 ────────────── */
    else if (strcmp(mtype, "asr_result") == 0) {
        char *json_str = cJSON_PrintUnformatted(root);
        if (json_str) {
            ESP_LOGI(TAG, "ASR full: %s", json_str);
            free(json_str);
        }
        const cJSON *payload = cJSON_GetObjectItem(root, "payload");
        if (payload) {
            const cJSON *text = cJSON_GetObjectItem(payload, "text");
            if (text && cJSON_IsString(text) && strlen(text->valuestring) > 0) {
                ESP_LOGI(TAG, "ASR: %s", text->valuestring);
                if (fsm_get_state() == AURA_STATE_PROCESSING) {
                    ws_set_agent_progress(60, "识别完成");
                }
            } else if (fsm_get_state() == AURA_STATE_IDLE) {
                ESP_LOGI(TAG, "Ignoring empty ASR result after local cancellation");
                s_waiting_send_ack = false;
            } else {
                ESP_LOGW(TAG, "ASR recognition failed — resetting to IDLE");
                s_waiting_send_ack = false;
                play_error_sfx_throttled();
                /* ASR 失败 → 回到 IDLE，否则永远卡在 PROCESSING */
                fsm_handle_event(AURA_EVT_ABORT);
            }
        }
    }

    /* ── system: pong 等系统消息 ────────── */
    else if (strcmp(mtype, "system") == 0) {
        const cJSON *payload = cJSON_GetObjectItem(root, "payload");
        if (payload) {
            const cJSON *action = cJSON_GetObjectItem(payload, "action");
            const cJSON *status = cJSON_GetObjectItem(payload, "status");
            if (action && cJSON_IsString(action))
                ESP_LOGD(TAG, "System: %s", action->valuestring);
            if (action && cJSON_IsString(action) &&
                strcmp(action->valuestring, "audio_received") == 0) {
                if (s_waiting_send_ack) {
                    s_waiting_send_ack = false;
                    ESP_LOGI(TAG, "Server acknowledged audio payload");
                    sfx_play(SFX_SENT);
                    if (fsm_get_state() == AURA_STATE_PROCESSING) {
                        ws_set_agent_progress(40, "已发送");
                    }
                }
            }
            if (action && cJSON_IsString(action) &&
                strcmp(action->valuestring, "server_vad_stop") == 0) {
                s_server_vad_stop_received = true;
                ESP_LOGI(TAG, "Server VAD requested local recording stop");
                if (fsm_get_state() == AURA_STATE_LISTENING) {
                    fsm_handle_event(AURA_EVT_VOICE_STOP);
                }
            }
            if (action && cJSON_IsString(action) &&
                strcmp(action->valuestring, "tts_failed") == 0) {
                ESP_LOGW(TAG, "Server reported TTS failure");
                play_error_sfx_throttled();
                ws_clear_pending_dialogue();
                ws_show_error_dialogue("回复生成失败了，你再试一次。");
                s_reply_in_flight = false;
                if (fsm_get_state() == AURA_STATE_PROCESSING ||
                    fsm_get_state() == AURA_STATE_SPEAKING) {
                    fsm_handle_event(AURA_EVT_ABORT);
                }
            }
            if (action && cJSON_IsString(action) &&
                strcmp(action->valuestring, "auth_token_update") == 0) {
                const cJSON *auth_token = cJSON_GetObjectItem(payload, "auth_token");
                if (auth_token && cJSON_IsString(auth_token) && auth_token->valuestring) {
                    strncpy(s_auth_token, auth_token->valuestring, sizeof(s_auth_token) - 1);
                    s_auth_token[sizeof(s_auth_token) - 1] = '\0';
                    ws_trim_string(s_auth_token);
                    esp_err_t err = ws_store_auth_token(s_auth_token);
                    if (err == ESP_OK) {
                        ESP_LOGI(TAG, "Updated device auth token from gateway");
                        aura_ui_set_agent_panel(g_state.agent_panel_visible,
                                                g_state.agent_progress,
                                                NULL,
                                                "令牌已更新");
                    } else {
                        ESP_LOGW(TAG, "Failed saving updated auth token: 0x%x", err);
                    }
                }
            }
            if (action && cJSON_IsString(action) &&
                strcmp(action->valuestring, "background_task_progress") == 0) {
                const cJSON *progress = cJSON_GetObjectItem(payload, "progress");
                const cJSON *elapsed = cJSON_GetObjectItem(payload, "elapsed_seconds");
                int progress_v = (progress && cJSON_IsNumber(progress)) ? progress->valueint : 10;
                char status_buf[32];
                if (elapsed && cJSON_IsNumber(elapsed)) {
                    snprintf(status_buf, sizeof(status_buf), "打工中 %ds", elapsed->valueint);
                } else {
                    snprintf(status_buf, sizeof(status_buf), "打工中...");
                }
                /* 面板已在绘制层避让字幕（上移），“我去查”提示保留到
                 * 自身 TTL 结束，让用户明确知道任务在跑而不是卡住。 */
                ws_set_background_work_panel(progress_v, status_buf);
            }
            if (action && cJSON_IsString(action) &&
                (strcmp(action->valuestring, "background_task_failed") == 0 ||
                 strcmp(action->valuestring, "background_task_timeout") == 0)) {
                /* 任务没跑成就别留着打工面板了 */
                aura_ui_set_agent_visible(false);
                aura_ui_mark_dirty();
            }
            if (action && cJSON_IsString(action) &&
                strcmp(action->valuestring, "music_control") == 0) {
                const cJSON *command = cJSON_GetObjectItem(payload, "command");
                const cJSON *wait_for_tts = cJSON_GetObjectItem(payload, "wait_for_tts");
                bool wait_window = true;
                if (wait_for_tts && cJSON_IsBool(wait_for_tts)) {
                    wait_window = cJSON_IsTrue(wait_for_tts);
                }
                if (command && cJSON_IsString(command)) {
                    if (strcmp(command->valuestring, "play") == 0) {
                        esp_err_t ret = music_player_request_play(wait_window);
                        ESP_LOGI(TAG, "Music play requested ret=0x%x", ret);
                    } else if (strcmp(command->valuestring, "next") == 0) {
                        esp_err_t ret = music_player_request_next();
                        ESP_LOGI(TAG, "Music next requested ret=0x%x", ret);
                    } else if (strcmp(command->valuestring, "stop") == 0) {
                        music_player_request_stop();
                        ESP_LOGI(TAG, "Music stop requested");
                    }
                }
            }
            if (action && cJSON_IsString(action) &&
                strcmp(action->valuestring, "hello") == 0 &&
                status && cJSON_IsString(status) &&
                strcmp(status->valuestring, "ready") == 0 &&
                !s_handshake_ready) {
                s_handshake_ready = true;
                ESP_LOGI(TAG, "Server handshake ready");
                if ((esp_timer_get_time() / 1000) >= s_connection_sfx_silent_until_ms) {
                    sfx_play(SFX_REPLY);
                } else {
                    ESP_LOGI(TAG, "Suppressing reconnect reply SFX during TTS recovery window");
                }
                /* 连接成功，清除开机对话框 */
                aura_ui_clear_dialogue();
            }
        }
    }

    /* ── hello ack ─────────────────────────── */
    else if (strcmp(mtype, "hello") == 0) {
        ESP_LOGI(TAG, "Server hello acknowledged");
        if (!s_handshake_ready) {
            s_handshake_ready = true;
            if ((esp_timer_get_time() / 1000) >= s_connection_sfx_silent_until_ms) {
                sfx_play(SFX_REPLY);
            } else {
                ESP_LOGI(TAG, "Suppressing reconnect reply SFX during TTS recovery window");
            }
            aura_ui_clear_dialogue();
        }
    }

    cJSON_Delete(root);
}
