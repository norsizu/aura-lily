/**
 * Aura 豆豆 — 硬件引脚定义与系统常量
 * 基于 Waveshare ESP32-S3-RLCD-4.2 开发板
 */
#pragma once

#include "driver/gpio.h"
#include "hal/adc_types.h"
#include "sdkconfig.h"

// ── Battery monitor (Waveshare RLCD 4.2) ───────────────
#define RLCD_BATTERY_ADC_UNIT        ADC_UNIT_1
#define RLCD_BATTERY_ADC_CHANNEL     ADC_CHANNEL_3   // GPIO4 / BAT_ADC
#define RLCD_BATTERY_ADC_GPIO        GPIO_NUM_4
#define RLCD_BATTERY_DIVIDER_TOP_OHM 200000.0f
#define RLCD_BATTERY_DIVIDER_BOTTOM_OHM 100000.0f

// ── RLCD SPI 引脚 ──────────────────────────────────────
#define RLCD_PIN_DS      GPIO_NUM_5    // Data/Command (DC)
#define RLCD_PIN_TE      GPIO_NUM_6    // Tearing Effect
#define RLCD_PIN_SCK     GPIO_NUM_11   // SPI Clock
#define RLCD_PIN_DIN     GPIO_NUM_12   // SPI MOSI
#define RLCD_PIN_CS      GPIO_NUM_40   // Chip Select
#define RLCD_PIN_RESET   GPIO_NUM_41   // Reset

#define RLCD_WIDTH       400
#define RLCD_HEIGHT      300
#define RLCD_FB_SIZE     (RLCD_WIDTH * RLCD_HEIGHT / 8)  // 1-bit: 15000 bytes
#define RLCD_SPI_HOST    SPI2_HOST
#define RLCD_SPI_FREQ    40000000      // 40MHz

// ── I2S 音频引脚 (ES8311 + ES7210) ────────────────────
#define I2S_PIN_DOUT     GPIO_NUM_8    // I2S Data Out (to ES8311 speaker)
#define I2S_PIN_SCLK     GPIO_NUM_9    // I2S Bit Clock (BCLK)
#define I2S_PIN_DIN      GPIO_NUM_10   // I2S Data In (from ES7210 mic)
#define I2S_PIN_MCLK     GPIO_NUM_16   // Master Clock
#define I2S_PIN_LRCK     GPIO_NUM_45   // Word Select (LRCLK)
#define PA_CTRL_PIN      GPIO_NUM_46   // Amplifier enable (HIGH = on)

#define AUDIO_SAMPLE_RATE  16000
#define AUDIO_BITS         16
#define AUDIO_CHANNELS     1

// ── I2C 总线 (SHTC3, PCF85063, ES8311, ES7210 共享) ──
#define I2C_SDA_PIN      GPIO_NUM_13
#define I2C_SCL_PIN      GPIO_NUM_14
#define I2C_PORT         I2C_NUM_0
#define I2C_FREQ         400000        // 400kHz

// I2C 地址
#define ES8311_ADDR      0x18
#define ES7210_ADDR      0x40
#define SHTC3_ADDR       0x70
#define PCF85063_ADDR    0x51

// ── SPIFFS (内嵌资源，Flash) ──────────────────────────
#define SPIFFS_MOUNT_POINT  "/spiffs"
#define ASSETS_BASE_PATH    SPIFFS_MOUNT_POINT   // 资源优先从 SPIFFS 读

// ── SD Card (SDMMC 1-bit，可选外部存储) ───────────────
#define SDMMC_CMD_PIN    GPIO_NUM_21
#define SDMMC_CLK_PIN    GPIO_NUM_38
#define SDMMC_DATA_PIN   GPIO_NUM_39
#define SDMMC_D3_PIN     GPIO_NUM_17   // TF 的 D3/CS，1-bit 模式下也需要保持高电平
#define SD_MOUNT_POINT   "/sdcard"

// ── RTC ───────────────────────────────────────────────
#define RTC_INT_PIN      GPIO_NUM_15

// ── 按键 ──────────────────────────────────────────────
#define BTN_KEY_PIN      GPIO_NUM_18   // KEY：语音录音；菜单内确认；长按返回
#define BTN_BOOT_PIN     GPIO_NUM_0    // BOOT：打开菜单；菜单内移动选项
// PWR 是板载电源管理按键，不作为普通 ESP32 GPIO 读取。

// ── 音频 ──────────────────────────────────────────────
#define AURA_DEFAULT_OUTPUT_VOLUME 75   // 开机默认音量，TTS/提示音/音乐共用

// ── FreeRTOS 任务优先级与栈 ───────────────────────────
#define TASK_DISPLAY_PRIO     3
#define TASK_DISPLAY_STACK    8192

#define TASK_AUDIO_PRIO       5
#define TASK_AUDIO_STACK      8192

#define TASK_NETWORK_PRIO     4
#define TASK_NETWORK_STACK    8192

#define TASK_SENSOR_PRIO      2
#define TASK_SENSOR_STACK     4096

#define TASK_INPUT_PRIO       4
#define TASK_INPUT_STACK      20480

#define TASK_UPLOAD_PRIO      4
/* 实测栈高水位剩 ~22KB（用量 ~27KB），48KB 太浪费；内部 RAM 只有 ~8KB 空闲时
 * WiFi/LWIP 拿不到缓冲会整条 TCP 停摆。压到 36KB 仍留 ~9KB 余量。 */
#define TASK_UPLOAD_STACK     36864

// ── 网络配置 ──────────────────────────────────────────
#ifndef CONFIG_AURA_WS_URI_DEFAULT
#define CONFIG_AURA_WS_URI_DEFAULT ""
#endif
#define AURA_WS_URI_MAX_LEN 160
#define WS_URI_DEFAULT    CONFIG_AURA_WS_URI_DEFAULT
#define HEARTBEAT_INTERVAL_MS  30000
#define AURA_PROTOCOL_VERSION  1
#define AURA_DEVICE_NAME       "aura-esp32"
#define AURA_DEVICE_AUTH_TOKEN ""
#define AURA_SERVER_VAD_DEFAULT false
#define AURA_DEVICE_PUBLIC_IP_LOOKUP 1
#define AURA_GPIO_SCAN_DIAG 0

/* Configure these public metadata URLs in menuconfig for your own release host. */
#ifndef CONFIG_AURA_OTA_MANIFEST_URL
#define CONFIG_AURA_OTA_MANIFEST_URL ""
#endif
#ifndef CONFIG_AURA_OTA_RESOURCES_MANIFEST_URL
#define CONFIG_AURA_OTA_RESOURCES_MANIFEST_URL ""
#endif
#define AURA_OTA_MANIFEST_URL CONFIG_AURA_OTA_MANIFEST_URL
#define AURA_OTA_RESOURCES_MANIFEST_URL CONFIG_AURA_OTA_RESOURCES_MANIFEST_URL
#define AURA_ASSETS_VERSION "0.15.4"

// ── Atlas 九宫格 ─────────────────────────────────────
#define ATLAS_COLS       3
#define ATLAS_ROWS       3
#define POSE_COUNT       9

// ── UI 常量 ──────────────────────────────────────────
#define STATUS_BAR_HEIGHT  19   /* 新外壳顶部有遮挡，黑条加高 5px 让下方内容整体下移 */
#define BLOCK_MARGIN       4
#define BLOCK_PADDING      3
#define FONT_SMALL         8
#define FONT_MEDIUM        12

// ── 面板状态范围 ──────────────────────────────────────
#define AURA_COMPANION_STAT_MAX 100
#define AURA_BEANS_MAX          999
