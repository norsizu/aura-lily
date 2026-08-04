/**
 * 网关地址解析 — mDNS 服务发现 + 多级回退
 *
 * 解析顺序（开源部署主路径是 mDNS，避免把某台机器的 IP/主机名写死进固件）：
 *   1. mDNS 浏览 _aura-lily._tcp（服务端/宿主机广播，换网络自动跟随）
 *   2. NVS 缓存的“上次成功连接”地址（广播临时不可用时兜底）
 *   3. 配网页面填写的 server_uri（用户显式配置，如公网 wss://）
 *   4. 编译期默认 WS_URI_DEFAULT（开发兜底）
 */
#pragma once
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

/* 按上述顺序解析出 WebSocket URI；总会写出一个可用候选并返回 ESP_OK。 */
esp_err_t net_discovery_resolve_ws_uri(char *uri, size_t uri_len);

/* 仅做一次 mDNS 浏览；找到返回 ESP_OK，否则 ESP_ERR_NOT_FOUND。 */
esp_err_t net_discovery_mdns_query(char *uri, size_t uri_len, uint32_t timeout_ms);

/* “上次成功连接”地址的 NVS 缓存（namespace=device, key=last_ws_uri）。 */
esp_err_t net_discovery_load_last_good(char *uri, size_t uri_len);
esp_err_t net_discovery_save_last_good(const char *uri);
