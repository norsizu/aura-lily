/**
 * WiFi 管理器
 */
#pragma once
#include <stdbool.h>
#include "esp_err.h"

esp_err_t wifi_manager_init(void);
esp_err_t wifi_manager_connect(void);
bool wifi_manager_is_connected(void);
int wifi_manager_get_rssi(void);  // 返回 RSSI 值
bool wifi_manager_has_credentials(void);
esp_err_t wifi_manager_save_credentials(const char *ssid, const char *password);
bool wifi_manager_get_saved_ssid(int slot, char *ssid, size_t ssid_len);
esp_err_t wifi_manager_select_saved_network(int slot);
esp_err_t wifi_manager_connect_saved_network(int slot);
esp_err_t wifi_manager_clear_credentials(void);
esp_err_t wifi_manager_load_server_uri(char *uri, size_t uri_len);
esp_err_t wifi_manager_save_server_uri(const char *uri);
bool wifi_manager_needs_provisioning(void);
esp_err_t wifi_manager_start_provisioning(void);
void wifi_manager_stop_provisioning(void);
bool wifi_manager_is_provisioning(void);
const char *wifi_manager_get_provisioning_ssid(void);
const char *wifi_manager_get_provisioning_url(void);
