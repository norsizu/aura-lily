#include "ota_update.h"

#include <ctype.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_spiffs.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"
#include "nvs.h"

#include "aura_config.h"

#define OTA_MANIFEST_MAX_BYTES (32 * 1024)
#define OTA_IO_BUFFER_BYTES     4096
#define OTA_HTTP_BUFFER_BYTES   2048
#define OTA_TASK_STACK_BYTES    8192
#define OTA_URL_MAX_BYTES       256
#define OTA_RESOURCE_PATH_MAX   64
#define OTA_NVS_NAMESPACE       "ota"
#define OTA_NVS_ASSET_VERSION   "assets_ver"
#define OTA_NVS_PENDING_PATH    "res_pending"
#define OTA_RESOURCE_NEW_PATH   SPIFFS_MOUNT_POINT "/.ota_new"
#define OTA_RESOURCE_OLD_PATH   SPIFFS_MOUNT_POINT "/.ota_old"
#define OTA_BUFFERED_RESOURCE_MAX_BYTES (256 * 1024)

static const char *TAG = "aura_ota";
static volatile bool s_update_running;
static bool s_resources_only;
static aura_ota_status_cb_t s_status_callback;
static void *s_status_context;

typedef struct {
    char *data;
    size_t length;
    size_t capacity;
    bool overflow;
} manifest_buffer_t;

static void report_status(aura_ota_status_t status, int progress)
{
    if (s_status_callback) {
        s_status_callback(status, progress, s_status_context);
    }
}

static bool valid_https_url(const char *url)
{
    return url && strncmp(url, "https://", 8) == 0 && strlen(url) < OTA_URL_MAX_BYTES;
}

static bool valid_sha256_hex(const char *hex)
{
    if (!hex || strlen(hex) != 64) return false;
    for (int i = 0; i < 64; ++i) {
        if (!isxdigit((unsigned char)hex[i])) return false;
    }
    return true;
}

static bool sha256_matches(const unsigned char digest[32], const char *expected)
{
    static const char digits[] = "0123456789abcdef";
    char actual[65];
    for (int i = 0; i < 32; ++i) {
        actual[i * 2] = digits[digest[i] >> 4];
        actual[i * 2 + 1] = digits[digest[i] & 0x0f];
    }
    actual[64] = '\0';
    return strcasecmp(actual, expected) == 0;
}

static int version_compare(const char *left, const char *right)
{
    if (!left) left = "";
    if (!right) right = "";
    while (*left || *right) {
        char *left_end = NULL;
        char *right_end = NULL;
        unsigned long left_part = strtoul(left, &left_end, 10);
        unsigned long right_part = strtoul(right, &right_end, 10);
        if (left_part != right_part) return left_part > right_part ? 1 : -1;
        left = left_end;
        right = right_end;
        while (*left && !isdigit((unsigned char)*left)) ++left;
        while (*right && !isdigit((unsigned char)*right)) ++right;
    }
    return 0;
}

static esp_err_t manifest_http_event(esp_http_client_event_t *event)
{
    manifest_buffer_t *buffer = (manifest_buffer_t *)event->user_data;
    if (event->event_id != HTTP_EVENT_ON_DATA || !buffer || event->data_len <= 0) {
        return ESP_OK;
    }
    if (buffer->length + (size_t)event->data_len >= buffer->capacity) {
        buffer->overflow = true;
        return ESP_FAIL;
    }
    memcpy(buffer->data + buffer->length, event->data, (size_t)event->data_len);
    buffer->length += (size_t)event->data_len;
    buffer->data[buffer->length] = '\0';
    return ESP_OK;
}

static esp_err_t fetch_manifest_at(const char *url, cJSON **manifest_out)
{
    if (!valid_https_url(url) || !manifest_out) return ESP_ERR_INVALID_ARG;
    esp_err_t result = ESP_FAIL;
    manifest_buffer_t buffer = {
        .data = calloc(1, OTA_MANIFEST_MAX_BYTES),
        .capacity = OTA_MANIFEST_MAX_BYTES,
    };
    if (!buffer.data) return ESP_ERR_NO_MEM;

    esp_http_client_config_t config = {
        .url = url,
        .event_handler = manifest_http_event,
        .user_data = &buffer,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 15000,
        .keep_alive_enable = true,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        free(buffer.data);
        return ESP_ERR_NO_MEM;
    }

    result = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    if (result == ESP_OK && status == 200 && !buffer.overflow) {
        cJSON *manifest = cJSON_ParseWithLength(buffer.data, buffer.length);
        if (manifest) {
            *manifest_out = manifest;
            result = ESP_OK;
        } else {
            result = ESP_ERR_INVALID_RESPONSE;
        }
    } else if (result == ESP_OK) {
        ESP_LOGE(TAG, "Manifest HTTP status=%d overflow=%d", status, buffer.overflow);
        result = ESP_ERR_INVALID_RESPONSE;
    }

    esp_http_client_cleanup(client);
    free(buffer.data);
    return result;
}

static esp_err_t open_download(const char *url, esp_http_client_handle_t *client_out)
{
    if (!valid_https_url(url) || !client_out) return ESP_ERR_INVALID_ARG;
    esp_http_client_config_t config = {
        .url = url,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 20000,
        .keep_alive_enable = false,
        .buffer_size = OTA_HTTP_BUFFER_BYTES,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) return ESP_ERR_NO_MEM;
    esp_err_t err = esp_http_client_open(client, 0);
    if (err == ESP_OK) {
        int64_t content_length = esp_http_client_fetch_headers(client);
        if (content_length < 0) {
            ESP_LOGE(TAG, "Download header fetch failed: %lld url=%s",
                     (long long)content_length, url);
            err = ESP_ERR_INVALID_RESPONSE;
        } else if (esp_http_client_get_status_code(client) != 200) {
            ESP_LOGE(TAG, "Download HTTP status=%d url=%s",
                     esp_http_client_get_status_code(client), url);
            err = ESP_ERR_INVALID_RESPONSE;
        }
    }
    if (err != ESP_OK) {
        esp_http_client_cleanup(client);
        return err;
    }
    *client_out = client;
    return ESP_OK;
}

static int read_download(esp_http_client_handle_t client, void *buffer, size_t length)
{
    while (true) {
        errno = 0;
        int count = esp_http_client_read(client, buffer, (int)length);
        if (count != 0) return count;
        if (esp_http_client_is_complete_data_received(client)) return 0;
        if (errno == ECONNRESET || errno == ENOTCONN) return -1;
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

static bool resource_path_is_safe(const char *path)
{
    if (!path || !path[0] || path[0] == '/' || strstr(path, "..") || strchr(path, '\\')) {
        return false;
    }
    size_t length = strlen(path);
    return length < OTA_RESOURCE_PATH_MAX &&
           length < CONFIG_SPIFFS_OBJ_NAME_LEN;
}

static esp_err_t nvs_set_pending_resource(const char *path)
{
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(OTA_NVS_NAMESPACE, NVS_READWRITE, &nvs);
    if (err != ESP_OK) return err;
    if (path) {
        err = nvs_set_str(nvs, OTA_NVS_PENDING_PATH, path);
    } else {
        err = nvs_erase_key(nvs, OTA_NVS_PENDING_PATH);
        if (err == ESP_ERR_NVS_NOT_FOUND) err = ESP_OK;
    }
    if (err == ESP_OK) err = nvs_commit(nvs);
    nvs_close(nvs);
    return err;
}

static esp_err_t replace_resource_file(const char *relative_path)
{
    char target[OTA_RESOURCE_PATH_MAX + sizeof(SPIFFS_MOUNT_POINT) + 2];
    snprintf(target, sizeof(target), SPIFFS_MOUNT_POINT "/%s", relative_path);

    unlink(OTA_RESOURCE_OLD_PATH);
    esp_err_t err = nvs_set_pending_resource(target);
    if (err != ESP_OK) return err;

    bool had_old = access(target, F_OK) == 0;
    if (had_old && rename(target, OTA_RESOURCE_OLD_PATH) != 0) {
        nvs_set_pending_resource(NULL);
        return ESP_FAIL;
    }
    if (rename(OTA_RESOURCE_NEW_PATH, target) != 0) {
        if (had_old) (void)rename(OTA_RESOURCE_OLD_PATH, target);
        nvs_set_pending_resource(NULL);
        return ESP_FAIL;
    }

    unlink(OTA_RESOURCE_OLD_PATH);
    return nvs_set_pending_resource(NULL);
}

/* Verified buffered resources can be retried safely if power is interrupted. */
static esp_err_t replace_resource_from_buffer(const char *relative_path,
                                              const unsigned char *data,
                                              size_t length)
{
    char target[OTA_RESOURCE_PATH_MAX + sizeof(SPIFFS_MOUNT_POINT) + 2];
    snprintf(target, sizeof(target), SPIFFS_MOUNT_POINT "/%s", relative_path);

    /* Remove the old file first so fragmented/full SPIFFS partitions can
     * immediately reuse its pages. The manifest version remains unchanged
     * until every file succeeds, so an interrupted write is retried. */
    unlink(target);
    FILE *file = fopen(target, "wb");
    if (!file) {
        size_t total = 0, used = 0;
        esp_spiffs_info("assets", &total, &used);
        ESP_LOGE(TAG, "Resource open failed path=%s errno=%d (%s) free=%u/%u",
                 target, errno, strerror(errno),
                 (unsigned)(total > used ? total - used : 0), (unsigned)total);
        return ESP_FAIL;
    }
    bool write_ok = fwrite(data, 1, length, file) == length;
    if (write_ok && fflush(file) != 0) write_ok = false;
    if (fclose(file) != 0) write_ok = false;
    if (!write_ok) {
        ESP_LOGE(TAG, "Resource write failed path=%s errno=%d (%s)",
                 target, errno, strerror(errno));
        unlink(target);
        return ESP_FAIL;
    }
    return ESP_OK;
}

static esp_err_t download_small_resource(const cJSON *item, int index, int count,
                                         size_t expected_size)
{
    const cJSON *path_item = cJSON_GetObjectItemCaseSensitive(item, "path");
    const cJSON *url_item = cJSON_GetObjectItemCaseSensitive(item, "url");
    const cJSON *sha_item = cJSON_GetObjectItemCaseSensitive(item, "sha256");
    unsigned char *data = heap_caps_malloc(expected_size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!data) return ESP_ERR_NO_MEM;

    esp_http_client_handle_t client = NULL;
    esp_err_t err = open_download(url_item->valuestring, &client);
    size_t total = 0;
    int last_progress = -1;
    while (err == ESP_OK && total < expected_size) {
        int received = read_download(client, data + total, expected_size - total);
        if (received <= 0) {
            if (received < 0) err = ESP_FAIL;
            break;
        }
        total += (size_t)received;
        int item_progress = (int)((total * 100U) / expected_size);
        int overall = (index * 100 + (item_progress > 100 ? 100 : item_progress)) / count;
        if (overall / 10 != last_progress / 10) {
            last_progress = overall;
            report_status(AURA_OTA_DOWNLOADING_RESOURCES, overall);
        }
    }
    bool complete = client && esp_http_client_is_complete_data_received(client);
    if (client) esp_http_client_cleanup(client);

    unsigned char digest[32];
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    mbedtls_sha256_starts(&sha, 0);
    mbedtls_sha256_update(&sha, data, total);
    mbedtls_sha256_finish(&sha, digest);
    mbedtls_sha256_free(&sha);
    if (err == ESP_OK && (!complete || total != expected_size)) {
        ESP_LOGE(TAG, "Small resource download incomplete path=%s got=%u expected=%u complete=%d",
                 path_item->valuestring, (unsigned)total, (unsigned)expected_size, complete);
        err = ESP_ERR_INVALID_SIZE;
    }
    if (err == ESP_OK && !sha256_matches(digest, sha_item->valuestring)) {
        ESP_LOGE(TAG, "Small resource checksum mismatch path=%s", path_item->valuestring);
        err = ESP_ERR_INVALID_CRC;
    }
    if (err == ESP_OK) err = replace_resource_from_buffer(path_item->valuestring, data, total);
    heap_caps_free(data);
    return err;
}

static esp_err_t download_resource(const cJSON *item, int index, int count)
{
    const cJSON *path_item = cJSON_GetObjectItemCaseSensitive(item, "path");
    const cJSON *url_item = cJSON_GetObjectItemCaseSensitive(item, "url");
    const cJSON *sha_item = cJSON_GetObjectItemCaseSensitive(item, "sha256");
    const cJSON *size_item = cJSON_GetObjectItemCaseSensitive(item, "size");
    if (!cJSON_IsString(path_item) || !resource_path_is_safe(path_item->valuestring) ||
        !cJSON_IsString(url_item) || !valid_https_url(url_item->valuestring) ||
        !cJSON_IsString(sha_item) || !valid_sha256_hex(sha_item->valuestring) ||
        !cJSON_IsNumber(size_item) || size_item->valuedouble <= 0) {
        return ESP_ERR_INVALID_ARG;
    }

    size_t expected_size = (size_t)size_item->valuedouble;
    if (expected_size <= OTA_BUFFERED_RESOURCE_MAX_BYTES) {
        return download_small_resource(item, index, count, expected_size);
    }

    esp_http_client_handle_t client = NULL;
    esp_err_t err = open_download(url_item->valuestring, &client);
    if (err != ESP_OK) return err;

    unlink(OTA_RESOURCE_NEW_PATH);
    FILE *file = fopen(OTA_RESOURCE_NEW_PATH, "wb");
    if (!file) {
        size_t total = 0, used = 0;
        esp_spiffs_info("assets", &total, &used);
        ESP_LOGE(TAG, "Resource temp open failed path=%s errno=%d (%s) free=%u/%u",
                 OTA_RESOURCE_NEW_PATH, errno, strerror(errno),
                 (unsigned)(total > used ? total - used : 0), (unsigned)total);
        esp_http_client_cleanup(client);
        return ESP_FAIL;
    }

    unsigned char *io = malloc(OTA_IO_BUFFER_BYTES);
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    mbedtls_sha256_starts(&sha, 0);
    size_t total = 0;
    int last_progress = -1;
    if (!io) err = ESP_ERR_NO_MEM;
    while (err == ESP_OK) {
        int received = read_download(client, io, OTA_IO_BUFFER_BYTES);
        if (received < 0) {
            err = ESP_FAIL;
            break;
        }
        if (received == 0) break;
        if (fwrite(io, 1, (size_t)received, file) != (size_t)received) {
            err = ESP_FAIL;
            break;
        }
        mbedtls_sha256_update(&sha, io, (size_t)received);
        total += (size_t)received;
        int item_progress = (int)((total * 100U) / (size_t)size_item->valuedouble);
        int overall = (index * 100 + (item_progress > 100 ? 100 : item_progress)) / count;
        if (overall / 10 != last_progress / 10) {
            last_progress = overall;
            report_status(AURA_OTA_DOWNLOADING_RESOURCES, overall);
        }
    }

    unsigned char digest[32];
    mbedtls_sha256_finish(&sha, digest);
    mbedtls_sha256_free(&sha);
    free(io);
    if (fclose(file) != 0 && err == ESP_OK) err = ESP_FAIL;
    if (!esp_http_client_is_complete_data_received(client) && err == ESP_OK) err = ESP_FAIL;
    esp_http_client_cleanup(client);

    if (err == ESP_OK && total != (size_t)size_item->valuedouble) err = ESP_ERR_INVALID_SIZE;
    if (err == ESP_OK && !sha256_matches(digest, sha_item->valuestring)) err = ESP_ERR_INVALID_CRC;
    if (err == ESP_OK) err = replace_resource_file(path_item->valuestring);
    if (err != ESP_OK) unlink(OTA_RESOURCE_NEW_PATH);
    return err;
}

static esp_err_t get_asset_version(char *version, size_t version_size)
{
    if (!version || version_size == 0) return ESP_ERR_INVALID_ARG;
    version[0] = '\0';
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(OTA_NVS_NAMESPACE, NVS_READONLY, &nvs);
    if (err != ESP_OK) {
        strlcpy(version, AURA_ASSETS_VERSION, version_size);
        return err;
    }
    size_t length = version_size;
    err = nvs_get_str(nvs, OTA_NVS_ASSET_VERSION, version, &length);
    nvs_close(nvs);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        strlcpy(version, AURA_ASSETS_VERSION, version_size);
    }
    return err;
}

static esp_err_t set_asset_version(const char *version)
{
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(OTA_NVS_NAMESPACE, NVS_READWRITE, &nvs);
    if (err != ESP_OK) return err;
    err = nvs_set_str(nvs, OTA_NVS_ASSET_VERSION, version);
    if (err == ESP_OK) err = nvs_commit(nvs);
    nvs_close(nvs);
    return err;
}

static esp_err_t update_resources_if_needed(const cJSON *resources, bool *updated)
{
    if (!cJSON_IsObject(resources)) return ESP_OK;
    const cJSON *version_item = cJSON_GetObjectItemCaseSensitive(resources, "version");
    const cJSON *files = cJSON_GetObjectItemCaseSensitive(resources, "files");
    if (!cJSON_IsString(version_item) || !cJSON_IsArray(files)) return ESP_ERR_INVALID_ARG;

    char current[32] = {0};
    (void)get_asset_version(current, sizeof(current));
    if (strcmp(current, version_item->valuestring) == 0) return ESP_OK;

    int count = cJSON_GetArraySize(files);
    if (count <= 0) return ESP_ERR_INVALID_ARG;
    for (int i = 0; i < count; ++i) {
        esp_err_t err = download_resource(cJSON_GetArrayItem(files, i), i, count);
        if (err != ESP_OK) return err;
    }
    esp_err_t err = set_asset_version(version_item->valuestring);
    if (err == ESP_OK) *updated = true;
    return err;
}

static esp_err_t update_app_if_needed(const cJSON *app, bool *updated)
{
    if (!cJSON_IsObject(app)) return ESP_OK;
    const cJSON *version_item = cJSON_GetObjectItemCaseSensitive(app, "version");
    const cJSON *url_item = cJSON_GetObjectItemCaseSensitive(app, "url");
    const cJSON *sha_item = cJSON_GetObjectItemCaseSensitive(app, "sha256");
    const cJSON *size_item = cJSON_GetObjectItemCaseSensitive(app, "size");
    if (!cJSON_IsString(version_item) || !cJSON_IsString(url_item) ||
        !valid_https_url(url_item->valuestring) || !cJSON_IsString(sha_item) ||
        !valid_sha256_hex(sha_item->valuestring) || !cJSON_IsNumber(size_item) ||
        size_item->valuedouble <= 0) {
        return ESP_ERR_INVALID_ARG;
    }

    const esp_app_desc_t *running_desc = esp_app_get_description();
    if (version_compare(version_item->valuestring, running_desc->version) <= 0) return ESP_OK;

    const esp_partition_t *partition = esp_ota_get_next_update_partition(NULL);
    if (!partition || (size_t)size_item->valuedouble > partition->size) return ESP_ERR_INVALID_SIZE;

    esp_http_client_handle_t client = NULL;
    esp_err_t err = open_download(url_item->valuestring, &client);
    if (err != ESP_OK) return err;

    esp_ota_handle_t handle = 0;
    err = esp_ota_begin(partition, (size_t)size_item->valuedouble, &handle);
    unsigned char *io = malloc(OTA_IO_BUFFER_BYTES);
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    mbedtls_sha256_starts(&sha, 0);
    size_t total = 0;
    int last_progress = -1;
    if (!io && err == ESP_OK) err = ESP_ERR_NO_MEM;
    while (err == ESP_OK) {
        int received = read_download(client, io, OTA_IO_BUFFER_BYTES);
        if (received < 0) {
            err = ESP_FAIL;
            break;
        }
        if (received == 0) break;
        err = esp_ota_write(handle, io, (size_t)received);
        if (err != ESP_OK) break;
        mbedtls_sha256_update(&sha, io, (size_t)received);
        total += (size_t)received;
        int progress = (int)((total * 100U) / (size_t)size_item->valuedouble);
        if (progress / 10 != last_progress / 10) {
            last_progress = progress;
            report_status(AURA_OTA_DOWNLOADING_APP, progress > 100 ? 100 : progress);
        }
    }

    unsigned char digest[32];
    mbedtls_sha256_finish(&sha, digest);
    mbedtls_sha256_free(&sha);
    free(io);
    if (!esp_http_client_is_complete_data_received(client) && err == ESP_OK) err = ESP_FAIL;
    esp_http_client_cleanup(client);
    if (err == ESP_OK && total != (size_t)size_item->valuedouble) err = ESP_ERR_INVALID_SIZE;
    if (err == ESP_OK && !sha256_matches(digest, sha_item->valuestring)) err = ESP_ERR_INVALID_CRC;

    report_status(AURA_OTA_VERIFYING, 100);
    if (err == ESP_OK) {
        err = esp_ota_end(handle);
        handle = 0;
    }
    if (err != ESP_OK && handle) esp_ota_abort(handle);
    if (err != ESP_OK) return err;

    esp_app_desc_t downloaded_desc;
    err = esp_ota_get_partition_description(partition, &downloaded_desc);
    if (err != ESP_OK || strcmp(downloaded_desc.version, version_item->valuestring) != 0) {
        ESP_LOGE(TAG, "Manifest/app version mismatch manifest=%s image=%s",
                 version_item->valuestring, err == ESP_OK ? downloaded_desc.version : "?");
        return ESP_ERR_INVALID_VERSION;
    }
    err = esp_ota_set_boot_partition(partition);
    if (err == ESP_OK) *updated = true;
    return err;
}

static esp_err_t ota_update_run_current_mode(void)
{
    bool resources_only = s_resources_only;
    cJSON *manifest = NULL;
    bool resources_updated = false;
    bool app_updated = false;
    report_status(AURA_OTA_CHECKING, 0);

    esp_err_t err = fetch_manifest_at(
        resources_only ? AURA_OTA_RESOURCES_MANIFEST_URL : AURA_OTA_MANIFEST_URL,
        &manifest);
    const cJSON *schema = manifest ? cJSON_GetObjectItemCaseSensitive(manifest, "schema") : NULL;
    if (err == ESP_OK && (!cJSON_IsNumber(schema) || schema->valueint != 1)) {
        err = ESP_ERR_INVALID_VERSION;
    }
    const cJSON *resources = manifest ? cJSON_GetObjectItemCaseSensitive(manifest, "resources") : NULL;
    if (err == ESP_OK && cJSON_IsObject(resources)) {
        err = update_resources_if_needed(resources, &resources_updated);
    } else if (err == ESP_OK && !resources_only) {
        const cJSON *resources_url = cJSON_GetObjectItemCaseSensitive(manifest, "resources_url");
        if (cJSON_IsString(resources_url) && valid_https_url(resources_url->valuestring)) {
            cJSON *resources_manifest = NULL;
            err = fetch_manifest_at(resources_url->valuestring, &resources_manifest);
            const cJSON *resources_schema = resources_manifest
                ? cJSON_GetObjectItemCaseSensitive(resources_manifest, "schema") : NULL;
            if (err == ESP_OK && (!cJSON_IsNumber(resources_schema) || resources_schema->valueint != 1)) {
                err = ESP_ERR_INVALID_VERSION;
            }
            if (err == ESP_OK) {
                err = update_resources_if_needed(
                    cJSON_GetObjectItemCaseSensitive(resources_manifest, "resources"),
                    &resources_updated);
            }
            cJSON_Delete(resources_manifest);
        }
    }
    if (err == ESP_OK && !resources_only) {
        err = update_app_if_needed(
            cJSON_GetObjectItemCaseSensitive(manifest, "app"), &app_updated);
    }
    cJSON_Delete(manifest);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OTA failed: %s (0x%x)", esp_err_to_name(err), err);
        report_status(AURA_OTA_FAILED, 0);
    } else if (resources_updated || app_updated) {
        report_status(AURA_OTA_RESTARTING, 100);
        vTaskDelay(pdMS_TO_TICKS(1500));
        esp_restart();
    } else {
        report_status(AURA_OTA_UP_TO_DATE, 100);
    }

    s_update_running = false;
    s_status_callback = NULL;
    s_status_context = NULL;
    return err;
}

static void ota_update_task(void *arg)
{
    (void)arg;
    (void)ota_update_run_current_mode();
    vTaskDeleteWithCaps(NULL);
}

bool ota_update_is_running(void)
{
    return s_update_running;
}

static esp_err_t ota_update_start_mode(aura_ota_status_cb_t callback, void *ctx,
                                       bool resources_only)
{
    if (s_update_running) return ESP_ERR_INVALID_STATE;
    s_update_running = true;
    s_resources_only = resources_only;
    s_status_callback = callback;
    s_status_context = ctx;
    multi_heap_info_t internal_info = {0};
    heap_caps_get_info(&internal_info, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    ESP_LOGI(TAG, "Starting OTA task mode=%s internal_free=%u largest=%u stack=%u",
             resources_only ? "resources" : "full",
             (unsigned)internal_info.total_free_bytes,
             (unsigned)internal_info.largest_free_block,
             (unsigned)OTA_TASK_STACK_BYTES);
    if (xTaskCreateWithCaps(
            ota_update_task,
            "ota_update",
            OTA_TASK_STACK_BYTES,
            NULL,
            3,
            NULL,
            MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT) != pdPASS) {
        heap_caps_get_info(&internal_info, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        ESP_LOGE(TAG, "OTA task allocation failed internal_free=%u largest=%u stack=%u",
                 (unsigned)internal_info.total_free_bytes,
                 (unsigned)internal_info.largest_free_block,
                 (unsigned)OTA_TASK_STACK_BYTES);
        s_update_running = false;
        s_status_callback = NULL;
        s_status_context = NULL;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t ota_update_start(aura_ota_status_cb_t callback, void *ctx)
{
    return ota_update_start_mode(callback, ctx, false);
}

esp_err_t ota_update_start_resources(aura_ota_status_cb_t callback, void *ctx)
{
    return ota_update_start_mode(callback, ctx, true);
}

esp_err_t ota_update_run_blocking(aura_ota_status_cb_t callback, void *ctx,
                                  bool resources_only)
{
    if (s_update_running) return ESP_ERR_INVALID_STATE;
    s_update_running = true;
    s_resources_only = resources_only;
    s_status_callback = callback;
    s_status_context = ctx;
    multi_heap_info_t internal_info = {0};
    heap_caps_get_info(&internal_info, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    ESP_LOGI(TAG, "Running OTA on caller task mode=%s internal_free=%u largest=%u",
             resources_only ? "resources" : "full",
             (unsigned)internal_info.total_free_bytes,
             (unsigned)internal_info.largest_free_block);
    return ota_update_run_current_mode();
}

void ota_update_recover_resources(void)
{
    char target[OTA_RESOURCE_PATH_MAX + sizeof(SPIFFS_MOUNT_POINT) + 2] = {0};
    nvs_handle_t nvs;
    if (nvs_open(OTA_NVS_NAMESPACE, NVS_READONLY, &nvs) == ESP_OK) {
        size_t length = sizeof(target);
        (void)nvs_get_str(nvs, OTA_NVS_PENDING_PATH, target, &length);
        nvs_close(nvs);
    }

    if (target[0] && access(OTA_RESOURCE_OLD_PATH, F_OK) == 0) {
        /* A pending marker means the replacement was not committed.  Always
         * prefer the backed-up file, even if a partial target exists. */
        unlink(target);
        if (rename(OTA_RESOURCE_OLD_PATH, target) == 0) {
            ESP_LOGW(TAG, "Recovered interrupted resource update: %s", target);
        }
    }
    unlink(OTA_RESOURCE_NEW_PATH);
    if (target[0]) (void)nvs_set_pending_resource(NULL);
}

esp_err_t ota_update_confirm_running_app(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state;
    esp_err_t err = esp_ota_get_state_partition(running, &state);
    if (err == ESP_ERR_NOT_SUPPORTED || err == ESP_ERR_NOT_FOUND) return ESP_OK;
    if (err != ESP_OK) return err;
    if (state != ESP_OTA_IMG_PENDING_VERIFY) return ESP_OK;

    err = esp_ota_mark_app_valid_cancel_rollback();
    if (err == ESP_OK) ESP_LOGI(TAG, "New OTA app marked valid after health checkpoint");
    return err;
}

bool ota_update_running_app_pending_verify(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state;
    return esp_ota_get_state_partition(running, &state) == ESP_OK &&
           state == ESP_OTA_IMG_PENDING_VERIFY;
}
