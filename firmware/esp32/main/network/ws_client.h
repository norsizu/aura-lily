/**
 * WebSocket 客户端 — 与 Aura 后端通信
 *
 * Protocol (matches voice_command_server.py):
 *   → {"type":"start"}        开始录音会话
 *   → binary PCM int16 16kHz  录音数据帧
 *   → {"type":"stop"}         停止录音，触发 ASR→LLM
 *   ← {"type":"message","sender":"AI","text":"..."}  回复
 */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include "esp_err.h"
#include "input/buttons.h"

/* ── 生命周期 ──────────────────────────────── */
esp_err_t ws_client_init(const char *uri);
esp_err_t ws_client_connect(void);
esp_err_t ws_client_apply_uri(const char *uri);  /* mDNS 重发现后切换网关地址并重建连接 */
void      ws_client_loop(void);               /* 心跳 & 重连 */
bool      ws_client_is_connected(void);
bool      ws_client_is_ready(void);
bool      ws_client_is_tts_active(void);
void      ws_client_prepare_for_ota(void);
void      ws_client_resume_after_ota(void);
void      ws_client_on_audio_loop(void);
void      ws_client_cancel_pending_reply(void);
bool      ws_client_take_server_vad_stop(void);
const char *ws_client_device_id(void);
const char *ws_client_boot_id(void);
void      ws_client_set_language(const char *language);

/* ── 通用文本发送 ─────────────────────────── */
esp_err_t ws_client_send_text(const char *text);

/* ── 语音交互协议 ─────────────────────────── */
esp_err_t ws_client_send_start(void);          /* {"type":"start"} */
esp_err_t ws_client_send_start_with_server_vad(bool server_vad_enabled);
esp_err_t ws_client_send_stop(void);           /* {"type":"stop"}  */
esp_err_t ws_client_send_cancel(const char *reason);
esp_err_t ws_client_send_pcm(const uint8_t *pcm, size_t len);  /* binary frame */

/* ── 按键上报 (保留兼容) ──────────────────── */
esp_err_t ws_client_send_button(button_event_t evt);
esp_err_t ws_client_send_gpio_diag(int pin, int old_level, int new_level);
esp_err_t ws_client_send_gpio_snapshot(const char *label, int key_level, int boot_level);
esp_err_t ws_client_send_dessert_purchase(const char *item_id, int beans);
bool ws_client_dessert_purchase_pending(void);
