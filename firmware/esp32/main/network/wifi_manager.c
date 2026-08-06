/**
 * WiFi STA 模式管理 — 连接 + 自动重连
 */
#include "wifi_manager.h"
#include "aura_config.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_http_server.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "nvs_flash.h"
#include "esp_sntp.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "cJSON.h"
#include <string.h>
#include <time.h>
#include <sys/time.h>
#include <stdio.h>
#include <stdlib.h>

static const char *TAG = "wifi";
extern EventGroupHandle_t g_event_group;
#define EVT_WIFI_CONNECTED BIT0
#define WIFI_CREDENTIAL_SLOT_COUNT 2
#define RETRIES_PER_NETWORK 3

static int s_retry_count = 0;
#define MAX_RETRY 10
static int s_active_credential_slot = -1;
static uint8_t s_available_credential_slots = 0;
static bool s_needs_provisioning = false;
static bool s_provisioning_active = false;
static bool s_wifi_started = false;
static bool s_wifi_initialized = false;
static bool s_sta_autoconnect_enabled = true;
static esp_netif_t *s_sta_netif = NULL;
static esp_netif_t *s_ap_netif = NULL;
static httpd_handle_t s_prov_httpd = NULL;
static char s_prov_ssid[32] = {0};
static const char *s_prov_url = "http://192.168.4.1";
static wifi_err_reason_t s_last_disconnect_reason = WIFI_REASON_UNSPECIFIED;

static esp_err_t wifi_load_credentials_slot(
    int slot, char *ssid, size_t ssid_len, char *password, size_t password_len);
static int wifi_load_preferred_slot(void);
static esp_err_t wifi_connect_credential_slot(int slot);
static bool wifi_valid_server_uri(const char *uri);

static const char *wifi_disconnect_reason_label(wifi_err_reason_t reason)
{
    switch (reason) {
        case WIFI_REASON_AUTH_FAIL: return "认证失败";
        case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT: return "四次握手超时";
        case WIFI_REASON_HANDSHAKE_TIMEOUT: return "握手超时";
        case WIFI_REASON_NO_AP_FOUND: return "未找到热点";
        case WIFI_REASON_NO_AP_FOUND_W_COMPATIBLE_SECURITY: return "热点加密不兼容";
        case WIFI_REASON_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD: return "认证模式不匹配";
        case WIFI_REASON_ASSOC_FAIL: return "关联失败";
        case WIFI_REASON_CONNECTION_FAIL: return "连接失败";
        case WIFI_REASON_BEACON_TIMEOUT: return "信号超时";
        default: return "未知原因";
    }
}

static bool wifi_sta_autoconnect_allowed(void)
{
    return !s_provisioning_active && s_sta_autoconnect_enabled;
}

static bool wifi_valid_server_uri(const char *uri)
{
    if (!uri || uri[0] == '\0') return false;
    size_t len = strlen(uri);
    if (len >= AURA_WS_URI_MAX_LEN) return false;
    return strncmp(uri, "ws://", 5) == 0 || strncmp(uri, "wss://", 6) == 0;
}

static const char *PROVISION_HTML =
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>Aura 配网</title>"
    "<style>"
    "body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;margin:0;"
    "background:#f5f5f7;color:#111;padding:24px}"
    "main{max-width:460px;margin:0 auto;background:#fff;padding:24px;border-radius:20px;"
    "box-shadow:0 12px 40px rgba(0,0,0,.08)}"
    "h2{margin:0 0 8px;font-size:24px}"
    "p{line-height:1.6;color:#444}"
    ".field{margin:18px 0 0}"
    "label{display:block;font-size:15px;font-weight:600;margin-bottom:8px}"
    "input{width:100%%;box-sizing:border-box;padding:14px 16px;border:1px solid #ddd;border-radius:12px;font-size:16px}"
    ".row{display:flex;gap:10px;align-items:center;margin-top:12px}"
    "button{padding:13px 16px;border:0;border-radius:12px;background:#111;color:#fff;font-size:16px;font-weight:600}"
    "button.secondary{background:#ececf0;color:#111}"
    ".check{display:flex;gap:10px;align-items:center;margin-top:12px;font-size:14px;color:#444}"
    ".check input{width:auto;transform:scale(1.1)}"
    "#msg{margin-top:14px;font-size:14px;color:#555;min-height:22px}"
    "#saved{margin-top:10px;font-size:13px;color:#666;line-height:1.5}"
    "#saved_networks{display:grid;gap:8px;margin-top:10px}"
    "#saved_networks button{width:100%;text-align:left;background:#f1f1f5;color:#111;font-size:14px}"
    "#networks{margin-top:14px;display:grid;gap:8px}"
    ".net{padding:12px 14px;border:1px solid #e6e6ea;border-radius:12px;background:#fafafa;cursor:pointer}"
    ".net strong{display:block;font-size:15px}"
    ".net small{display:block;color:#666;margin-top:4px}"
    ".muted{font-size:13px;color:#666;margin-top:16px}"
    "</style></head>"
    "<body><main>"
    "<h2>Aura Wi-Fi 配网</h2>"
    "<p>连接到设备热点后，选择附近 Wi-Fi，填写本地 Aura Lily 网关地址，设备会自动重启并连接。</p>"
    "<div class='field'>"
    "<label for='ssid'>Wi-Fi 名称</label>"
    "<input id='ssid' placeholder='点击下方列表选择，或手动输入 SSID'>"
    "</div>"
    "<div class='field'>"
    "<label for='password'>Wi-Fi 密码</label>"
    "<input id='password' type='password' placeholder='输入密码'>"
    "</div>"
    "<div class='field'>"
    "<label for='server_uri'>Aura Lily 网关 WebSocket 地址</label>"
    "<input id='server_uri' placeholder='ws://你的电脑IP:8787/ws'>"
    "</div>"
    "<label class='check'><input id='showpass' type='checkbox' onchange='togglePass()'>显示密码，避免输错</label>"
    "<div class='row'>"
    "<button class='secondary' onclick='scan()'>扫描附近 Wi-Fi</button>"
    "<button onclick='save()'>保存并连接</button>"
    "</div>"
    "<div id='msg'></div>"
    "<div id='saved'></div>"
    "<div id='saved_networks'></div>"
    "<div id='networks'></div>"
    "<div class='muted'>ESP32 不能使用 127.0.0.1；本地部署请填写电脑的局域网或 Tailscale IP。</div>"
    "</main>"
    "<script>"
    "function setMsg(t){document.getElementById('msg').textContent=t||''}"
    "function setSaved(t){document.getElementById('saved').textContent=t||''}"
    "function esc(s){return (s||'').replace(/[&<>\\\"']/g,m=>({\"&\":\"&amp;\",\"<\":\"&lt;\",\">\":\"&gt;\",\"\\\"\":\"&quot;\",\"'\":\"&#39;\"}[m]))}"
    "function togglePass(){document.getElementById('password').type=document.getElementById('showpass').checked?'text':'password'}"
    "async function selectSaved(slot,ssid){"
    "setMsg('正在切换到 '+ssid+'...');"
    "const res=await fetch('/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slot:slot})});"
    "const data=await res.json().catch(()=>({ok:false,error:'bad_json'}));"
    "setMsg(data.ok?'已选择 '+ssid+'，设备即将重启并连接。':'切换失败：'+(data.error||'unknown'));"
    "}"
    "async function loadStatus(){"
    "const res=await fetch('/status');"
    "const data=await res.json().catch(()=>({ok:false}));"
    "if(!data.ok) return;"
    "const lines=[];"
    "const savedWrap=document.getElementById('saved_networks'); savedWrap.innerHTML='';"
    "if(data.saved_networks&&data.saved_networks.length){"
    "lines.push('已保存 Wi-Fi（点击即可切换）：');"
    "data.saved_networks.forEach(n=>{const b=document.createElement('button'); b.type='button'; b.textContent=(n.preferred?'当前：':'切换到：')+n.ssid; b.onclick=()=>selectSaved(n.slot,n.ssid); savedWrap.appendChild(b);});"
    "}else if(data.saved_ssids&&data.saved_ssids.length){lines.push('已保存 Wi-Fi：'+data.saved_ssids.join('、'));}"
    "if(data.server_uri){document.getElementById('server_uri').value=data.server_uri; lines.push('当前网关：'+data.server_uri);}"
    "if(data.last_error) lines.push('上次失败原因：'+data.last_error);"
    "setSaved(lines.join('  '));"
    "}"
    "async function scan(){"
    "setMsg('正在扫描附近 Wi-Fi...');"
    "const wrap=document.getElementById('networks'); wrap.innerHTML='';"
    "const res=await fetch('/scan');"
    "const data=await res.json().catch(()=>({ok:false,error:'bad_json'}));"
    "if(!data.ok){setMsg('扫描失败：'+(data.error||'unknown')); return;}"
    "setMsg(data.networks && data.networks.length ? '请选择一个 Wi-Fi' : '没有扫描到可用 Wi-Fi');"
    "(data.networks||[]).forEach(n=>{"
    "const div=document.createElement('div'); div.className='net';"
    "div.innerHTML='<strong>'+esc(n.ssid)+'</strong><small>信号 '+n.rssi+' dBm · '+esc(n.auth)+' · 信道 '+n.channel+'</small>';"
    "div.onclick=()=>{document.getElementById('ssid').value=n.ssid; document.getElementById('password').focus();};"
    "wrap.appendChild(div);"
    "});"
    "}"
    "async function save(){"
    "setMsg('正在保存...');"
    "const res=await fetch('/configure',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({ssid:document.getElementById('ssid').value,password:document.getElementById('password').value,server_uri:document.getElementById('server_uri').value})});"
    "const data=await res.json().catch(()=>({ok:false,error:'bad_json'}));"
    "setMsg(data.ok?'已保存，设备即将重启并连接 Wi-Fi。':'保存失败：'+(data.error||'unknown'));"
    "}"
    "loadStatus();"
    "scan();"
    "</script>"
    "</body></html>";

static const char *wifi_auth_mode_label(wifi_auth_mode_t mode)
{
    switch (mode) {
        case WIFI_AUTH_OPEN: return "开放网络";
        case WIFI_AUTH_WEP: return "WEP";
        case WIFI_AUTH_WPA_PSK: return "WPA";
        case WIFI_AUTH_WPA2_PSK: return "WPA2";
        case WIFI_AUTH_WPA_WPA2_PSK: return "WPA/WPA2";
        case WIFI_AUTH_WPA3_PSK: return "WPA3";
        case WIFI_AUTH_WPA2_WPA3_PSK: return "WPA2/WPA3";
        default: return "未知加密";
    }
}

static esp_err_t provisioning_scan_get(httpd_req_t *req)
{
    wifi_scan_config_t scan_cfg = {
        .show_hidden = false,
    };
    esp_err_t err = esp_wifi_scan_start(&scan_cfg, true);
    if (err != ESP_OK) {
        httpd_resp_set_status(req, "500 Internal Server Error");
        return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"scan_start_failed\"}");
    }

    uint16_t count = 16;
    wifi_ap_record_t records[16];
    memset(records, 0, sizeof(records));
    err = esp_wifi_scan_get_ap_records(&count, records);
    if (err != ESP_OK) {
        httpd_resp_set_status(req, "500 Internal Server Error");
        return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"scan_read_failed\"}");
    }

    cJSON *root = cJSON_CreateObject();
    cJSON *items = cJSON_AddArrayToObject(root, "networks");
    if (!root || !items) {
        cJSON_Delete(root);
        httpd_resp_set_status(req, "500 Internal Server Error");
        return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"alloc_failed\"}");
    }
    cJSON_AddBoolToObject(root, "ok", true);

    for (int i = 0; i < count; i++) {
        if (records[i].ssid[0] == '\0') continue;
        cJSON *item = cJSON_CreateObject();
        if (!item) continue;
        cJSON_AddStringToObject(item, "ssid", (const char *)records[i].ssid);
        cJSON_AddNumberToObject(item, "rssi", records[i].rssi);
        cJSON_AddNumberToObject(item, "channel", records[i].primary);
        cJSON_AddStringToObject(item, "auth", wifi_auth_mode_label(records[i].authmode));
        cJSON_AddItemToArray(items, item);
    }

    char *body = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!body) {
        httpd_resp_set_status(req, "500 Internal Server Error");
        return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"json_failed\"}");
    }

    httpd_resp_set_type(req, "application/json");
    esp_err_t ret = httpd_resp_sendstr(req, body);
    cJSON_free(body);
    return ret;
}

static void wifi_restart_task(void *arg)
{
    vTaskDelay(pdMS_TO_TICKS(1200));
    esp_restart();
}

static esp_err_t provisioning_root_get(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    return httpd_resp_send(req, PROVISION_HTML, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t provisioning_status_get(httpd_req_t *req)
{
    char server_uri[AURA_WS_URI_MAX_LEN] = {0};
    cJSON *root = cJSON_CreateObject();
    if (!root) {
        httpd_resp_set_status(req, "500 Internal Server Error");
        return httpd_resp_sendstr(req, "{\"ok\":false}");
    }
    cJSON_AddBoolToObject(root, "ok", true);
    cJSON_AddStringToObject(root, "ssid", s_prov_ssid);
    cJSON_AddStringToObject(root, "url", s_prov_url);
    if (wifi_manager_load_server_uri(server_uri, sizeof(server_uri)) == ESP_OK) {
        cJSON_AddStringToObject(root, "server_uri", server_uri);
    } else {
        cJSON_AddStringToObject(root, "server_uri", WS_URI_DEFAULT);
    }
    cJSON_AddBoolToObject(root, "provisioning", s_provisioning_active);
    cJSON_AddNumberToObject(root, "preferred_slot", wifi_load_preferred_slot());
    cJSON *saved_ssids = cJSON_AddArrayToObject(root, "saved_ssids");
    cJSON *saved_networks = cJSON_AddArrayToObject(root, "saved_networks");
    for (int slot = 0; saved_ssids && slot < WIFI_CREDENTIAL_SLOT_COUNT; ++slot) {
        char saved_ssid[33] = {0};
        char saved_password[65] = {0};
        if (wifi_load_credentials_slot(
                slot, saved_ssid, sizeof(saved_ssid),
                saved_password, sizeof(saved_password)) == ESP_OK) {
            cJSON_AddItemToArray(saved_ssids, cJSON_CreateString(saved_ssid));
            if (saved_networks) {
                cJSON *network = cJSON_CreateObject();
                if (network) {
                    cJSON_AddNumberToObject(network, "slot", slot);
                    cJSON_AddStringToObject(network, "ssid", saved_ssid);
                    cJSON_AddBoolToObject(network, "preferred", slot == wifi_load_preferred_slot());
                    cJSON_AddItemToArray(saved_networks, network);
                }
            }
        }
    }
    if (s_last_disconnect_reason != 0) {
        char error_line[96];
        snprintf(
            error_line,
            sizeof(error_line),
            "%s (%d)",
            wifi_disconnect_reason_label(s_last_disconnect_reason),
            (int)s_last_disconnect_reason
        );
        cJSON_AddStringToObject(root, "last_error", error_line);
    }
    char *body = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!body) {
        httpd_resp_set_status(req, "500 Internal Server Error");
        return httpd_resp_sendstr(req, "{\"ok\":false}");
    }
    httpd_resp_set_type(req, "application/json");
    esp_err_t ret = httpd_resp_sendstr(req, body);
    cJSON_free(body);
    return ret;
}

static esp_err_t provisioning_select_post(httpd_req_t *req)
{
    char buf[128];
    int total = req->content_len;
    if (total <= 0 || total >= (int)sizeof(buf)) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"payload_too_large\"}");
    }
    int read_total = 0;
    while (read_total < total) {
        int read = httpd_req_recv(req, buf + read_total, total - read_total);
        if (read <= 0) {
            httpd_resp_set_status(req, "400 Bad Request");
            return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"recv_failed\"}");
        }
        read_total += read;
    }
    buf[read_total] = '\0';
    cJSON *root = cJSON_Parse(buf);
    const cJSON *slot_item = root ? cJSON_GetObjectItem(root, "slot") : NULL;
    int slot = (slot_item && cJSON_IsNumber(slot_item)) ? slot_item->valueint : -1;
    esp_err_t err = wifi_manager_select_saved_network(slot);
    cJSON_Delete(root);
    if (err != ESP_OK) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"invalid_saved_network\"}");
    }
    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, "{\"ok\":true}");
    xTaskCreate(wifi_restart_task, "wifi_restart", 2048, NULL, 3, NULL);
    return ESP_OK;
}

static esp_err_t provisioning_configure_post(httpd_req_t *req)
{
    char buf[512];
    int total = req->content_len;
    if (total <= 0 || total >= (int)sizeof(buf)) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"payload_too_large\"}");
    }

    int read_total = 0;
    while (read_total < total) {
        int read = httpd_req_recv(req, buf + read_total, total - read_total);
        if (read <= 0) {
            httpd_resp_set_status(req, "400 Bad Request");
            return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"recv_failed\"}");
        }
        read_total += read;
    }
    buf[read_total] = '\0';

    cJSON *root = cJSON_Parse(buf);
    if (!root) {
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"invalid_json\"}");
    }

    const cJSON *ssid = cJSON_GetObjectItem(root, "ssid");
    const cJSON *password = cJSON_GetObjectItem(root, "password");
    const cJSON *server_uri = cJSON_GetObjectItem(root, "server_uri");
    const char *ssid_str = (ssid && cJSON_IsString(ssid)) ? ssid->valuestring : "";
    const char *pass_str = (password && cJSON_IsString(password)) ? password->valuestring : "";
    const char *server_uri_str = (server_uri && cJSON_IsString(server_uri)) ? server_uri->valuestring : "";

    if (!ssid_str || ssid_str[0] == '\0') {
        cJSON_Delete(root);
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"missing_ssid\"}");
    }

    if (server_uri_str && server_uri_str[0] != '\0' && !wifi_valid_server_uri(server_uri_str)) {
        cJSON_Delete(root);
        httpd_resp_set_status(req, "400 Bad Request");
        return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"invalid_server_uri\"}");
    }

    esp_err_t err = wifi_manager_save_credentials(ssid_str, pass_str);
    if (err == ESP_OK && server_uri_str && server_uri_str[0] != '\0') {
        err = wifi_manager_save_server_uri(server_uri_str);
    }
    cJSON_Delete(root);
    if (err != ESP_OK) {
        httpd_resp_set_status(req, "500 Internal Server Error");
        return httpd_resp_sendstr(req, "{\"ok\":false,\"error\":\"save_failed\"}");
    }

    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, "{\"ok\":true}");
    xTaskCreate(wifi_restart_task, "wifi_restart", 2048, NULL, 3, NULL);
    return ESP_OK;
}

static esp_err_t wifi_start_provisioning_httpd(void)
{
    if (s_prov_httpd) return ESP_OK;

    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = 80;
    config.ctrl_port = 32768;
    config.max_uri_handlers = 8;

    esp_err_t err = httpd_start(&s_prov_httpd, &config);
    if (err != ESP_OK) {
        return err;
    }

    httpd_uri_t root = {
        .uri = "/",
        .method = HTTP_GET,
        .handler = provisioning_root_get,
        .user_ctx = NULL,
    };
    httpd_uri_t status = {
        .uri = "/status",
        .method = HTTP_GET,
        .handler = provisioning_status_get,
        .user_ctx = NULL,
    };
    httpd_uri_t scan = {
        .uri = "/scan",
        .method = HTTP_GET,
        .handler = provisioning_scan_get,
        .user_ctx = NULL,
    };
    httpd_uri_t configure = {
        .uri = "/configure",
        .method = HTTP_POST,
        .handler = provisioning_configure_post,
        .user_ctx = NULL,
    };
    httpd_uri_t select_saved = {
        .uri = "/select",
        .method = HTTP_POST,
        .handler = provisioning_select_post,
        .user_ctx = NULL,
    };

    httpd_register_uri_handler(s_prov_httpd, &root);
    httpd_register_uri_handler(s_prov_httpd, &status);
    httpd_register_uri_handler(s_prov_httpd, &scan);
    httpd_register_uri_handler(s_prov_httpd, &configure);
    httpd_register_uri_handler(s_prov_httpd, &select_saved);
    return ESP_OK;
}

static const char *wifi_ssid_key(int slot)
{
    return slot == 0 ? "ssid" : "ssid2";
}

static const char *wifi_password_key(int slot)
{
    return slot == 0 ? "pass" : "pass2";
}

static esp_err_t wifi_load_credentials_slot(
    int slot, char *ssid, size_t ssid_len, char *password, size_t password_len)
{
    if (slot < 0 || slot >= WIFI_CREDENTIAL_SLOT_COUNT ||
        !ssid || !password || ssid_len == 0 || password_len == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t nvs;
    if (nvs_open("wifi", NVS_READONLY, &nvs) != ESP_OK) {
        return ESP_ERR_NOT_FOUND;
    }

    size_t len = ssid_len;
    esp_err_t err = nvs_get_str(nvs, wifi_ssid_key(slot), ssid, &len);
    if (err != ESP_OK || ssid[0] == '\0') {
        nvs_close(nvs);
        return err != ESP_OK ? err : ESP_ERR_NOT_FOUND;
    }

    len = password_len;
    err = nvs_get_str(nvs, wifi_password_key(slot), password, &len);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        password[0] = '\0';
        err = ESP_OK;
    }
    nvs_close(nvs);
    return err;
}

static int wifi_load_preferred_slot(void)
{
    nvs_handle_t nvs;
    uint8_t preferred = 0;
    if (nvs_open("wifi", NVS_READONLY, &nvs) == ESP_OK) {
        if (nvs_get_u8(nvs, "preferred", &preferred) != ESP_OK ||
            preferred >= WIFI_CREDENTIAL_SLOT_COUNT) {
            preferred = 0;
        }
        nvs_close(nvs);
    }
    return (int)preferred;
}

static void wifi_remember_preferred_slot(int slot)
{
    if (slot < 0 || slot >= WIFI_CREDENTIAL_SLOT_COUNT) return;
    nvs_handle_t nvs;
    if (nvs_open("wifi", NVS_READWRITE, &nvs) != ESP_OK) return;
    if (nvs_set_u8(nvs, "preferred", (uint8_t)slot) == ESP_OK) {
        (void)nvs_commit(nvs);
    }
    nvs_close(nvs);
}

static esp_err_t wifi_connect_credential_slot(int slot)
{
    wifi_config_t wifi_cfg = {
        .sta = {
            .scan_method = WIFI_ALL_CHANNEL_SCAN,
            .sort_method = WIFI_CONNECT_AP_BY_SIGNAL,
            .threshold.authmode = WIFI_AUTH_OPEN,
        },
    };
    esp_err_t err = wifi_load_credentials_slot(
        slot,
        (char *)wifi_cfg.sta.ssid, sizeof(wifi_cfg.sta.ssid),
        (char *)wifi_cfg.sta.password, sizeof(wifi_cfg.sta.password));
    if (err != ESP_OK) return err;

    err = esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg);
    if (err != ESP_OK) return err;
    s_active_credential_slot = slot;
    ESP_LOGI(TAG, "Trying saved WiFi %d/%d: SSID=%s",
             slot + 1, WIFI_CREDENTIAL_SLOT_COUNT, wifi_cfg.sta.ssid);
    return esp_wifi_connect();
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        s_wifi_started = true;
        if (!wifi_sta_autoconnect_allowed()) {
            ESP_LOGI(TAG, "STA started with autoconnect disabled, skipping connect");
            return;
        }
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *event = (wifi_event_sta_disconnected_t *)data;
        xEventGroupClearBits(g_event_group, EVT_WIFI_CONNECTED);
        if (event) {
            s_last_disconnect_reason = event->reason;
            ESP_LOGW(
                TAG,
                "STA disconnected: reason=%d (%s)",
                (int)event->reason,
                wifi_disconnect_reason_label(event->reason)
            );
        }
        if (!wifi_sta_autoconnect_allowed()) {
            ESP_LOGI(TAG, "STA disconnected during provisioning, reconnect suppressed");
            return;
        }
        bool has_alternate = s_active_credential_slot >= 0 &&
            (s_available_credential_slots & (1U << (1 - s_active_credential_slot))) != 0;
        int retry_limit = has_alternate ? RETRIES_PER_NETWORK : MAX_RETRY;
        if (s_retry_count < retry_limit) {
            esp_wifi_connect();
            s_retry_count++;
            ESP_LOGW(TAG, "Reconnecting saved WiFi slot %d... (%d/%d)",
                     s_active_credential_slot + 1, s_retry_count, retry_limit);
        } else if (has_alternate) {
            int alternate = 1 - s_active_credential_slot;
            s_retry_count = 0;
            ESP_LOGW(TAG, "Switching to saved WiFi slot %d", alternate + 1);
            if (wifi_connect_credential_slot(alternate) != ESP_OK) {
                ESP_LOGE(TAG, "Could not switch to saved WiFi slot %d", alternate + 1);
            }
        } else {
            ESP_LOGE(TAG, "Max retries reached");
            s_needs_provisioning = true;
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        if (!wifi_sta_autoconnect_allowed()) {
            ESP_LOGW(TAG, "Ignoring unexpected STA IP while provisioning is active");
            xEventGroupClearBits(g_event_group, EVT_WIFI_CONNECTED);
            esp_wifi_disconnect();
            return;
        }
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "Connected! IP: " IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_count = 0;
        s_needs_provisioning = false;
        s_last_disconnect_reason = 0;
        wifi_remember_preferred_slot(s_active_credential_slot);
        xEventGroupSetBits(g_event_group, EVT_WIFI_CONNECTED);

        /* ── SNTP 校时 (UTC+8 / China Standard Time) ── */
        /* ESP/newlib 上直接使用 POSIX TZ 字符串更稳定，避免 IANA 时区数据库缺失 */
        setenv("TZ", "CST-8", 1);
        tzset();
        if (esp_sntp_enabled()) esp_sntp_stop();
        esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
        esp_sntp_setservername(0, "ntp.aliyun.com");
        esp_sntp_setservername(1, "pool.ntp.org");
        esp_sntp_init();
        ESP_LOGI(TAG, "SNTP started (UTC+8)");
    }
}

esp_err_t wifi_manager_init(void)
{
    if (s_wifi_initialized) {
        return ESP_OK;
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    if (!s_sta_netif) {
        s_sta_netif = esp_netif_create_default_wifi_sta();
    }

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));

    s_wifi_initialized = true;
    return ESP_OK;
}

esp_err_t wifi_manager_connect(void)
{
    // 从 NVS 读取 WiFi 配置，如果没有则用默认值
    wifi_config_t wifi_cfg = {
        .sta = {
            .scan_method = WIFI_ALL_CHANNEL_SCAN,
            .sort_method = WIFI_CONNECT_AP_BY_SIGNAL,
            .threshold.authmode = WIFI_AUTH_OPEN,
        },
    };

    s_available_credential_slots = 0;
    for (int slot = 0; slot < WIFI_CREDENTIAL_SLOT_COUNT; ++slot) {
        char ssid[33] = {0};
        char password[65] = {0};
        if (wifi_load_credentials_slot(
                slot, ssid, sizeof(ssid), password, sizeof(password)) == ESP_OK) {
            s_available_credential_slots |= (uint8_t)(1U << slot);
        }
    }
    int preferred_slot = wifi_load_preferred_slot();
    if ((s_available_credential_slots & (1U << preferred_slot)) == 0) {
        preferred_slot = (s_available_credential_slots & 1U) ? 0 : 1;
    }

    // 先尝试从 NVS 读取，旧版 ssid/pass 会自动作为第一个槽位继续使用
    if (wifi_load_credentials_slot(
            preferred_slot,
            (char *)wifi_cfg.sta.ssid, sizeof(wifi_cfg.sta.ssid),
            (char *)wifi_cfg.sta.password, sizeof(wifi_cfg.sta.password)) == ESP_OK) {
        s_active_credential_slot = preferred_slot;
        ESP_LOGI(
            TAG,
            "WiFi config from NVS slot %d: SSID=%s password_len=%d",
            preferred_slot + 1,
            wifi_cfg.sta.ssid,
            (int)strlen((const char *)wifi_cfg.sta.password)
        );
    } else {
        // SD 卡配置 fallback
        FILE *f = fopen(SD_MOUNT_POINT "/wifi.txt", "r");
        if (f) {
            char line[128];
            if (fgets(line, sizeof(line), f)) {
                line[strcspn(line, "\r\n")] = 0;
                strncpy((char *)wifi_cfg.sta.ssid, line, sizeof(wifi_cfg.sta.ssid) - 1);
            }
            if (fgets(line, sizeof(line), f)) {
                line[strcspn(line, "\r\n")] = 0;
                strncpy((char *)wifi_cfg.sta.password, line, sizeof(wifi_cfg.sta.password) - 1);
            }
            fclose(f);
            s_active_credential_slot = -1;
            ESP_LOGI(TAG, "WiFi config from SD: SSID=%s", wifi_cfg.sta.ssid);
        } else {
            ESP_LOGW(TAG, "No NVS/SD config, entering provisioning mode");
            s_needs_provisioning = true;
            return ESP_ERR_NOT_FOUND;
        }
    }

    s_provisioning_active = false;
    s_sta_autoconnect_enabled = false;
    s_retry_count = 0;
    xEventGroupClearBits(g_event_group, EVT_WIFI_CONNECTED);
    if (s_wifi_started) {
        esp_wifi_disconnect();
        esp_wifi_stop();
        s_wifi_started = false;
    }

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    s_sta_autoconnect_enabled = true;
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    s_wifi_started = true;

    ESP_LOGI(TAG, "WiFi connecting to: %s", wifi_cfg.sta.ssid);
    return ESP_OK;
}

bool wifi_manager_is_connected(void)
{
    return (xEventGroupGetBits(g_event_group) & EVT_WIFI_CONNECTED) != 0;
}

int wifi_manager_get_rssi(void)
{
    wifi_ap_record_t info;
    if (esp_wifi_sta_get_ap_info(&info) == ESP_OK) {
        return info.rssi;
    }
    return -100;
}

bool wifi_manager_has_credentials(void)
{
    for (int slot = 0; slot < WIFI_CREDENTIAL_SLOT_COUNT; ++slot) {
        char ssid[33] = {0};
        char password[65] = {0};
        if (wifi_load_credentials_slot(
                slot, ssid, sizeof(ssid), password, sizeof(password)) == ESP_OK) {
            return true;
        }
    }
    return false;
}

esp_err_t wifi_manager_load_server_uri(char *uri, size_t uri_len)
{
    if (!uri || uri_len == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t nvs;
    if (nvs_open("device", NVS_READONLY, &nvs) != ESP_OK) {
        return ESP_ERR_NOT_FOUND;
    }

    size_t len = uri_len;
    esp_err_t err = nvs_get_str(nvs, "server_uri", uri, &len);
    nvs_close(nvs);
    if (err != ESP_OK || !wifi_valid_server_uri(uri)) {
        if (uri_len > 0) uri[0] = '\0';
        return err != ESP_OK ? err : ESP_ERR_INVALID_ARG;
    }
    return ESP_OK;
}

esp_err_t wifi_manager_save_server_uri(const char *uri)
{
    if (!wifi_valid_server_uri(uri)) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t nvs;
    esp_err_t err = nvs_open("device", NVS_READWRITE, &nvs);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_str(nvs, "server_uri", uri);
    if (err == ESP_OK) {
        err = nvs_commit(nvs);
    }
    nvs_close(nvs);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Server URI saved: %s", uri);
    }
    return err;
}

esp_err_t wifi_manager_save_credentials(const char *ssid, const char *password)
{
    if (!ssid || ssid[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }

    int target_slot = -1;
    int preferred_slot = wifi_load_preferred_slot();
    for (int slot = 0; slot < WIFI_CREDENTIAL_SLOT_COUNT; ++slot) {
        char saved_ssid[33] = {0};
        char saved_password[65] = {0};
        if (wifi_load_credentials_slot(
                slot, saved_ssid, sizeof(saved_ssid),
                saved_password, sizeof(saved_password)) == ESP_OK) {
            if (strcmp(saved_ssid, ssid) == 0) {
                target_slot = slot;
                break;
            }
        } else if (target_slot < 0) {
            target_slot = slot;
        }
    }
    if (target_slot < 0) {
        int slot_to_preserve = s_active_credential_slot >= 0
            ? s_active_credential_slot : preferred_slot;
        target_slot = 1 - slot_to_preserve;
    }

    nvs_handle_t nvs;
    esp_err_t err = nvs_open("wifi", NVS_READWRITE, &nvs);
    if (err != ESP_OK) {
        return err;
    }

    err = nvs_set_str(nvs, wifi_ssid_key(target_slot), ssid);
    if (err == ESP_OK) {
        err = nvs_set_str(nvs, wifi_password_key(target_slot), password ? password : "");
    }
    if (err == ESP_OK) {
        err = nvs_set_u8(nvs, "preferred", (uint8_t)target_slot);
    }
    if (err == ESP_OK) {
        err = nvs_commit(nvs);
    }
    nvs_close(nvs);

    if (err == ESP_OK) {
        s_needs_provisioning = false;
        ESP_LOGI(TAG, "WiFi credentials saved in slot %d for SSID=%s password_len=%d",
                 target_slot + 1, ssid, (int)strlen(password ? password : ""));
    }
    return err;
}

esp_err_t wifi_manager_select_saved_network(int slot)
{
    char ssid[33] = {0};
    char password[65] = {0};
    if (wifi_load_credentials_slot(slot, ssid, sizeof(ssid), password, sizeof(password)) != ESP_OK) {
        return ESP_ERR_NOT_FOUND;
    }
    nvs_handle_t nvs;
    esp_err_t err = nvs_open("wifi", NVS_READWRITE, &nvs);
    if (err != ESP_OK) return err;
    err = nvs_set_u8(nvs, "preferred", (uint8_t)slot);
    if (err == ESP_OK) err = nvs_commit(nvs);
    nvs_close(nvs);
    if (err == ESP_OK) {
        s_active_credential_slot = slot;
        s_needs_provisioning = false;
        ESP_LOGI(TAG, "Selected saved WiFi slot %d for next connection: SSID=%s", slot + 1, ssid);
    }
    return err;
}

bool wifi_manager_get_saved_ssid(int slot, char *ssid, size_t ssid_len)
{
    char password[65] = {0};
    if (!ssid || ssid_len == 0) return false;
    ssid[0] = '\0';
    return wifi_load_credentials_slot(slot, ssid, ssid_len, password, sizeof(password)) == ESP_OK;
}

esp_err_t wifi_manager_connect_saved_network(int slot)
{
    esp_err_t err = wifi_manager_select_saved_network(slot);
    if (err != ESP_OK) return err;
    if (wifi_manager_is_provisioning()) {
        wifi_manager_stop_provisioning();
    }
    return wifi_manager_connect();
}

esp_err_t wifi_manager_clear_credentials(void)
{
    nvs_handle_t nvs;
    esp_err_t err = nvs_open("wifi", NVS_READWRITE, &nvs);
    if (err != ESP_OK) {
        return err;
    }

    const char *keys[] = {"ssid", "pass", "ssid2", "pass2", "preferred"};
    err = ESP_OK;
    for (size_t i = 0; i < sizeof(keys) / sizeof(keys[0]); ++i) {
        esp_err_t erase_err = nvs_erase_key(nvs, keys[i]);
        if (erase_err != ESP_OK && erase_err != ESP_ERR_NVS_NOT_FOUND) {
            err = erase_err;
            break;
        }
    }
    if (err == ESP_OK) {
        err = nvs_commit(nvs);
    }
    nvs_close(nvs);
    if (err == ESP_OK) {
        s_needs_provisioning = true;
        s_active_credential_slot = -1;
        s_available_credential_slots = 0;
        ESP_LOGI(TAG, "WiFi credentials cleared");
    }
    return err;
}

bool wifi_manager_needs_provisioning(void)
{
    return s_needs_provisioning;
}

esp_err_t wifi_manager_start_provisioning(void)
{
    if (s_provisioning_active) {
        return ESP_OK;
    }

    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(s_prov_ssid, sizeof(s_prov_ssid), "Aura-%02X%02X%02X", mac[3], mac[4], mac[5]);

    if (!s_ap_netif) {
        s_ap_netif = esp_netif_create_default_wifi_ap();
    }

    wifi_config_t ap_cfg = {
        .ap = {
            .ssid_len = 0,
            .channel = 1,
            .max_connection = 4,
            .authmode = WIFI_AUTH_OPEN,
            .pmf_cfg = {
                .required = false,
            },
        },
    };
    strncpy((char *)ap_cfg.ap.ssid, s_prov_ssid, sizeof(ap_cfg.ap.ssid) - 1);

    wifi_config_t sta_cfg = {0};

    s_provisioning_active = true;
    s_sta_autoconnect_enabled = false;
    s_needs_provisioning = true;
    s_retry_count = 0;
    xEventGroupClearBits(g_event_group, EVT_WIFI_CONNECTED);
    esp_wifi_disconnect();
    if (s_wifi_started) {
        esp_wifi_stop();
        s_wifi_started = false;
    }

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta_cfg));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_cfg));
    if (!s_wifi_started) {
        ESP_ERROR_CHECK(esp_wifi_start());
        s_wifi_started = true;
    }
    esp_err_t err = wifi_start_provisioning_httpd();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start provisioning HTTP server: 0x%x", err);
        s_provisioning_active = false;
        return err;
    }

    ESP_LOGW(TAG, "Provisioning AP started: SSID=%s URL=%s", s_prov_ssid, s_prov_url);
    return ESP_OK;
}

void wifi_manager_stop_provisioning(void)
{
    if (s_prov_httpd) {
        httpd_stop(s_prov_httpd);
        s_prov_httpd = NULL;
    }
    if (s_provisioning_active) {
        esp_wifi_set_mode(WIFI_MODE_STA);
    }
    s_provisioning_active = false;
    s_sta_autoconnect_enabled = true;
}

bool wifi_manager_is_provisioning(void)
{
    return s_provisioning_active;
}

const char *wifi_manager_get_provisioning_ssid(void)
{
    return s_prov_ssid;
}

const char *wifi_manager_get_provisioning_url(void)
{
    return s_prov_url;
}
