/**
 * 网关地址解析实现 — 见 net_discovery.h 的顺序说明
 */
#include "net_discovery.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_netif_ip_addr.h"
#include "mdns.h"
#include "nvs.h"

#include "aura_config.h"
#include "wifi_manager.h"

static const char *TAG = "net_disc";

#define AURA_MDNS_SERVICE   "_aura-lily"
#define AURA_MDNS_PROTO     "_tcp"
#define AURA_MDNS_TIMEOUT_MS 2500
#define AURA_MDNS_MAX_RESULTS 8
#define AURA_WS_DEFAULT_PORT 8787

#define NVS_NAMESPACE_DEVICE "device"
#define NVS_KEY_LAST_WS_URI  "last_ws_uri"

static bool s_mdns_ready = false;

/* mdns 组件的任务/缓冲会占用内部 RAM，而 WS 客户端任务需要 12KB 内部栈；
 * 因此只在查询期间短暂初始化，查完立即 mdns_free() 归还内存。 */
static esp_err_t ensure_mdns(void)
{
    if (s_mdns_ready) {
        return ESP_OK;
    }
    esp_err_t err = mdns_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "mdns_init failed: 0x%x", err);
        return err;
    }
    mdns_hostname_set("aura-lily-device");
    s_mdns_ready = true;
    return ESP_OK;
}

static void release_mdns(void)
{
    if (s_mdns_ready) {
        mdns_free();
        s_mdns_ready = false;
    }
}

esp_err_t net_discovery_mdns_query(char *uri, size_t uri_len, uint32_t timeout_ms)
{
    if (!uri || uri_len == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    uri[0] = '\0';
    if (ensure_mdns() != ESP_OK) {
        return ESP_FAIL;
    }

    mdns_result_t *results = NULL;
    esp_err_t err = mdns_query_ptr(AURA_MDNS_SERVICE, AURA_MDNS_PROTO,
                                   timeout_ms, AURA_MDNS_MAX_RESULTS, &results);
    if (err != ESP_OK || !results) {
        if (results) {
            mdns_query_results_free(results);
        }
        release_mdns();
        return ESP_ERR_NOT_FOUND;
    }

    esp_err_t found = ESP_ERR_NOT_FOUND;
    for (mdns_result_t *r = results; r && found != ESP_OK; r = r->next) {
        const char *path = "/ws";
        for (size_t i = 0; i < r->txt_count; ++i) {
            if (r->txt[i].key && strcmp(r->txt[i].key, "path") == 0 &&
                r->txt[i].value && r->txt[i].value[0] == '/') {
                path = r->txt[i].value;
                break;
            }
        }
        for (mdns_ip_addr_t *a = r->addr; a; a = a->next) {
            if (a->addr.type == ESP_IPADDR_TYPE_V4) {
                snprintf(uri, uri_len, "ws://" IPSTR ":%u%s",
                         IP2STR(&a->addr.u_addr.ip4),
                         (unsigned)(r->port ? r->port : AURA_WS_DEFAULT_PORT),
                         path);
                found = ESP_OK;
                break;
            }
        }
    }
    mdns_query_results_free(results);
    release_mdns();
    return found;
}

esp_err_t net_discovery_load_last_good(char *uri, size_t uri_len)
{
    if (!uri || uri_len == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE_DEVICE, NVS_READONLY, &nvs) != ESP_OK) {
        return ESP_ERR_NOT_FOUND;
    }
    size_t len = uri_len;
    esp_err_t err = nvs_get_str(nvs, NVS_KEY_LAST_WS_URI, uri, &len);
    nvs_close(nvs);
    if (err != ESP_OK || uri[0] == '\0') {
        uri[0] = '\0';
        return err != ESP_OK ? err : ESP_ERR_INVALID_ARG;
    }
    return ESP_OK;
}

esp_err_t net_discovery_save_last_good(const char *uri)
{
    if (!uri || uri[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    /* 已缓存相同地址则跳过，减少 NVS 磨损。 */
    char current[AURA_WS_URI_MAX_LEN] = {0};
    if (net_discovery_load_last_good(current, sizeof(current)) == ESP_OK &&
        strcmp(current, uri) == 0) {
        return ESP_OK;
    }
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(NVS_NAMESPACE_DEVICE, NVS_READWRITE, &nvs);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_str(nvs, NVS_KEY_LAST_WS_URI, uri);
    if (err == ESP_OK) {
        err = nvs_commit(nvs);
    }
    nvs_close(nvs);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Cached last-good WS URI: %s", uri);
    }
    return err;
}

esp_err_t net_discovery_resolve_ws_uri(char *uri, size_t uri_len)
{
    if (!uri || uri_len == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    /* 首查紧跟在拿到 IP 之后，组播可能尚未就绪，失败时重试一次。 */
    for (int attempt = 0; attempt < 2; ++attempt) {
        if (net_discovery_mdns_query(uri, uri_len, AURA_MDNS_TIMEOUT_MS) == ESP_OK) {
            ESP_LOGI(TAG, "WS URI via mDNS discovery: %s", uri);
            return ESP_OK;
        }
    }
    /* 用户显式配置的地址优先于"上次成功"缓存：换网络后缓存的旧内网 IP
     * 很可能已失效（如公司→家），而配网页填的公网/固定地址仍然有效。 */
    if (wifi_manager_load_server_uri(uri, uri_len) == ESP_OK && uri[0] != '\0') {
        ESP_LOGI(TAG, "WS URI via provisioned config: %s", uri);
        return ESP_OK;
    }
    if (net_discovery_load_last_good(uri, uri_len) == ESP_OK) {
        ESP_LOGI(TAG, "WS URI via last-good cache: %s", uri);
        return ESP_OK;
    }
    strncpy(uri, WS_URI_DEFAULT, uri_len - 1);
    uri[uri_len - 1] = '\0';
    ESP_LOGW(TAG, "WS URI fallback to compile default: %s", uri);
    return ESP_OK;
}
