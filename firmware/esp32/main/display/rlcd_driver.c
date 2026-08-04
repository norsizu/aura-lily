/**
 * ST7305 RLCD SPI 驱动 — Waveshare 4.2" RLCD
 * 400x300, 1-bit 黑白反射屏
 *
 * 参考: markbirss/ESPHome-ST7305-RLCD, Waveshare 官方 Arduino 驱动
 * 像素布局: Landscape 2×4 block (每字节 = 2列 × 4行)
 */
#include "rlcd_driver.h"
#include "aura_config.h"
#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>

static const char *TAG = "rlcd";
static spi_device_handle_t s_spi = NULL;

// ST7305 Waveshare 400x300 地址窗口
#define COL_START  0x12
#define COL_END    0x2A
#define ROW_START  0x00
#define ROW_END    0xC7

// 像素查找表 (存 PSRAM)
static uint16_t *s_pixel_index_lut = NULL;  // buffer byte index
static uint8_t  *s_pixel_bit_lut   = NULL;  // bit mask

// 刷屏用 DMA bounce buffer：必须开机一次性分配。
// 以前每次 flush 临时 malloc，agent 回合内部内存吃紧时分配失败，
// 就直接从 PSRAM 推屏 → 某个 1KB 块错位，屏上出现一条竖向花屏带。
#define RLCD_TX_CHUNK 1024
static uint8_t *s_dma_tx_buf = NULL;

static void rlcd_wait_for_te_window(void)
{
    int initial = gpio_get_level(RLCD_PIN_TE);
    int64_t deadline_us = esp_timer_get_time() + 25000;

    while (gpio_get_level(RLCD_PIN_TE) == initial && esp_timer_get_time() < deadline_us) {
        esp_rom_delay_us(50);
    }
}

/* ── SPI 命令/数据发送 ─────────────────────────────── */
static void rlcd_cmd(uint8_t cmd)
{
    gpio_set_level(RLCD_PIN_DS, 0);  // Command mode
    gpio_set_level(RLCD_PIN_CS, 0);
    spi_transaction_t t = {
        .length = 8,
        .tx_buffer = &cmd,
    };
    spi_device_polling_transmit(s_spi, &t);
    gpio_set_level(RLCD_PIN_CS, 1);
}

static void rlcd_data_byte(uint8_t val)
{
    gpio_set_level(RLCD_PIN_DS, 1);  // Data mode
    gpio_set_level(RLCD_PIN_CS, 0);
    spi_transaction_t t = {
        .length = 8,
        .tx_buffer = &val,
    };
    spi_device_polling_transmit(s_spi, &t);
    gpio_set_level(RLCD_PIN_CS, 1);
}

static void rlcd_data(const uint8_t *data, size_t len)
{
    if (len == 0) return;
    gpio_set_level(RLCD_PIN_DS, 1);  // Data mode
    gpio_set_level(RLCD_PIN_CS, 0);
    spi_transaction_t t = {
        .length = len * 8,
        .tx_buffer = data,
    };
    spi_device_polling_transmit(s_spi, &t);
    gpio_set_level(RLCD_PIN_CS, 1);
}

/* ── 写内存时 CS 保持低 ──────────────────────────── */
static void rlcd_write_framebuffer(const uint8_t *buf, size_t len)
{
    // Memory Write: CS 必须在 cmd+data 期间持续拉低
    gpio_set_level(RLCD_PIN_DS, 0);  // Command mode
    gpio_set_level(RLCD_PIN_CS, 0);
    uint8_t cmd = 0x2C;
    spi_transaction_t t1 = { .length = 8, .tx_buffer = &cmd };
    spi_device_polling_transmit(s_spi, &t1);

    gpio_set_level(RLCD_PIN_DS, 1);  // Data mode, CS still low
    // 分块发送；经内部 DMA bounce buffer（init 时预分配），
    // 避免直接从 PSRAM 推屏时偶发花屏/块错位
    const size_t CHUNK = RLCD_TX_CHUNK;
    uint8_t *dma_tx_buf = s_dma_tx_buf;
    for (size_t off = 0; off < len; off += CHUNK) {
        size_t n = (len - off > CHUNK) ? CHUNK : (len - off);
        const void *tx_ptr = buf + off;
        if (dma_tx_buf) {
            memcpy(dma_tx_buf, buf + off, n);
            tx_ptr = dma_tx_buf;
        }
        spi_transaction_t t2 = { .length = n * 8, .tx_buffer = tx_ptr };
        spi_device_polling_transmit(s_spi, &t2);
    }
    gpio_set_level(RLCD_PIN_CS, 1);
}

static void rlcd_reset(void)
{
    gpio_set_level(RLCD_PIN_RESET, 1);
    vTaskDelay(pdMS_TO_TICKS(50));
    gpio_set_level(RLCD_PIN_RESET, 0);
    vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(RLCD_PIN_RESET, 1);
    vTaskDelay(pdMS_TO_TICKS(50));
}

/* ── Landscape 2×4 像素查找表 ────────────────────── */
static void init_landscape_lut(void)
{
    // 每字节 = 2列 × 4行:
    //   Bit 7: (row0, col0)  Bit 6: (row0, col1)
    //   Bit 5: (row1, col0)  Bit 4: (row1, col1)
    //   Bit 3: (row2, col0)  Bit 2: (row2, col1)
    //   Bit 1: (row3, col0)  Bit 0: (row3, col1)
    const uint16_t H4 = RLCD_HEIGHT >> 2;  // 300/4 = 75

    for (uint16_t y = 0; y < RLCD_HEIGHT; y++) {
        uint16_t inv_y = RLCD_HEIGHT - 1 - y;
        uint16_t block_y = inv_y >> 2;
        uint8_t local_y = inv_y & 3;

        for (uint16_t x = 0; x < RLCD_WIDTH; x++) {
            uint16_t byte_x = x >> 1;
            uint8_t local_x = x & 1;

            uint16_t buffer_idx = byte_x * H4 + block_y;
            uint8_t bit = 7 - ((local_y << 1) | local_x);

            uint32_t pixel_idx = (uint32_t)x * RLCD_HEIGHT + y;
            s_pixel_index_lut[pixel_idx] = buffer_idx;
            s_pixel_bit_lut[pixel_idx] = (1 << bit);
        }
    }
    ESP_LOGI(TAG, "Landscape LUT built: %dx%d, H4=%d", RLCD_WIDTH, RLCD_HEIGHT, H4);
}

/* ── ST7305 初始化序列 ───────────────────────────── */
esp_err_t rlcd_init(void)
{
    ESP_LOGI(TAG, "Initializing ST7305 RLCD %dx%d", RLCD_WIDTH, RLCD_HEIGHT);

    // GPIO 配置
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << RLCD_PIN_DS) | (1ULL << RLCD_PIN_RESET) | (1ULL << RLCD_PIN_CS),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
    };
    gpio_config(&io_conf);

    // TE pin 输入
    gpio_config_t te_conf = {
        .pin_bit_mask = (1ULL << RLCD_PIN_TE),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&te_conf);

    // SPI 总线 — CS 手动控制（Memory Write 需要持续拉低）
    spi_bus_config_t bus_cfg = {
        .mosi_io_num = RLCD_PIN_DIN,
        .miso_io_num = -1,
        .sclk_io_num = RLCD_PIN_SCK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = RLCD_FB_SIZE + 64,
    };
    ESP_ERROR_CHECK(spi_bus_initialize(RLCD_SPI_HOST, &bus_cfg, SPI_DMA_CH_AUTO));

    spi_device_interface_config_t dev_cfg = {
        .clock_speed_hz = RLCD_SPI_FREQ,
        .mode = 0,
        .spics_io_num = -1,  // 手动控制 CS
        .queue_size = 2,
    };
    ESP_ERROR_CHECK(spi_bus_add_device(RLCD_SPI_HOST, &dev_cfg, &s_spi));

    // 分配像素查找表 (PSRAM)
    uint32_t total_pixels = RLCD_WIDTH * RLCD_HEIGHT;  // 120000
    s_pixel_index_lut = heap_caps_calloc(total_pixels, sizeof(uint16_t), MALLOC_CAP_SPIRAM);
    s_pixel_bit_lut   = heap_caps_calloc(total_pixels, sizeof(uint8_t),  MALLOC_CAP_SPIRAM);
    if (!s_pixel_index_lut || !s_pixel_bit_lut) {
        ESP_LOGE(TAG, "Failed to alloc LUT in PSRAM (%lu pixels)", (unsigned long)total_pixels);
        return ESP_ERR_NO_MEM;
    }
    // 刷屏 DMA bounce buffer 一次性分配好；运行中内存紧张也不影响刷屏
    s_dma_tx_buf = heap_caps_malloc(RLCD_TX_CHUNK, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    if (!s_dma_tx_buf) {
        ESP_LOGW(TAG, "Failed to alloc DMA tx bounce buffer; will push from PSRAM directly");
    }
    init_landscape_lut();

    // 硬件复位
    rlcd_reset();

    // ── ST7305 初始化命令序列 (Waveshare 参考) ──────

    // NVM Load Control
    rlcd_cmd(0xD6);
    rlcd_data_byte(0x17);
    rlcd_data_byte(0x02);

    // Booster Enable
    rlcd_cmd(0xD1);
    rlcd_data_byte(0x01);

    // Gate Voltage Setting (VGH/VGL)
    rlcd_cmd(0xC0);
    rlcd_data_byte(0x11);
    rlcd_data_byte(0x04);

    // VSHP Setting (正源电压 - 高功率模式)
    rlcd_cmd(0xC1);
    rlcd_data_byte(0x69);
    rlcd_data_byte(0x69);
    rlcd_data_byte(0x69);
    rlcd_data_byte(0x69);

    // VSLP Setting (正源电压 - 低功率模式)
    rlcd_cmd(0xC2);
    rlcd_data_byte(0x19);
    rlcd_data_byte(0x19);
    rlcd_data_byte(0x19);
    rlcd_data_byte(0x19);

    // VSHN Setting (负源电压 - 高功率模式)
    rlcd_cmd(0xC4);
    rlcd_data_byte(0x4B);
    rlcd_data_byte(0x4B);
    rlcd_data_byte(0x4B);
    rlcd_data_byte(0x4B);

    // VSLN Setting (负源电压 - 低功率模式)
    rlcd_cmd(0xC5);
    rlcd_data_byte(0x19);
    rlcd_data_byte(0x19);
    rlcd_data_byte(0x19);
    rlcd_data_byte(0x19);

    // OSC Setting (振荡器频率)
    rlcd_cmd(0xD8);
    rlcd_data_byte(0x80);
    rlcd_data_byte(0xE9);

    // Frame Rate Control
    rlcd_cmd(0xB2);
    rlcd_data_byte(0x02);

    // Gate EQ Control (High Power Mode)
    rlcd_cmd(0xB3);
    rlcd_data_byte(0xE5);
    rlcd_data_byte(0xF6);
    rlcd_data_byte(0x05);
    rlcd_data_byte(0x46);
    rlcd_data_byte(0x77);
    rlcd_data_byte(0x77);
    rlcd_data_byte(0x77);
    rlcd_data_byte(0x77);
    rlcd_data_byte(0x76);
    rlcd_data_byte(0x45);

    // Gate EQ Control (Low Power Mode)
    rlcd_cmd(0xB4);
    rlcd_data_byte(0x05);
    rlcd_data_byte(0x46);
    rlcd_data_byte(0x77);
    rlcd_data_byte(0x77);
    rlcd_data_byte(0x77);
    rlcd_data_byte(0x77);
    rlcd_data_byte(0x76);
    rlcd_data_byte(0x45);

    // Gate Timing Control
    rlcd_cmd(0x62);
    rlcd_data_byte(0x32);
    rlcd_data_byte(0x03);
    rlcd_data_byte(0x1F);

    // Source EQ Enable
    rlcd_cmd(0xB7);
    rlcd_data_byte(0x13);

    // Gate Line Setting (300 lines = 100*3)
    rlcd_cmd(0xB0);
    rlcd_data_byte(0x64);

    // Sleep Out
    rlcd_cmd(0x11);
    vTaskDelay(pdMS_TO_TICKS(200));

    // Source Voltage Select
    rlcd_cmd(0xC9);
    rlcd_data_byte(0x00);

    // MADCTL (MX=1, DO=1)
    rlcd_cmd(0x36);
    rlcd_data_byte(0x48);

    // Data Format: 1-bit mono
    rlcd_cmd(0x3A);
    rlcd_data_byte(0x11);

    // Gamma Mode: Monochrome
    rlcd_cmd(0xB9);
    rlcd_data_byte(0x20);

    // Panel Setting: 1-dot inversion, frame inversion, interlace
    rlcd_cmd(0xB8);
    rlcd_data_byte(0x29);

    // Display Inversion On
    rlcd_cmd(0x21);

    // Column Address Set
    rlcd_cmd(0x2A);
    rlcd_data_byte(COL_START);
    rlcd_data_byte(COL_END);

    // Row Address Set
    rlcd_cmd(0x2B);
    rlcd_data_byte(ROW_START);
    rlcd_data_byte(ROW_END);

    // Tearing Effect Line On
    rlcd_cmd(0x35);
    rlcd_data_byte(0x00);

    // Auto Power Down Control
    rlcd_cmd(0xD0);
    rlcd_data_byte(0xFF);

    // High Power Mode
    rlcd_cmd(0x38);

    // Display On
    rlcd_cmd(0x29);

    // 清屏白色
    rlcd_clear(0xFF);

    ESP_LOGI(TAG, "ST7305 RLCD initialized successfully");
    return ESP_OK;
}

/* ── 刷新整屏 ──────────────────────────────────── */
void rlcd_flush(const uint8_t *framebuffer)
{
    rlcd_wait_for_te_window();

    // 设置地址窗口
    rlcd_cmd(0x38);  // High Power Mode
    rlcd_cmd(0x29);  // Display On

    rlcd_cmd(0x2A);
    rlcd_data_byte(COL_START);
    rlcd_data_byte(COL_END);

    rlcd_cmd(0x2B);
    rlcd_data_byte(ROW_START);
    rlcd_data_byte(ROW_END);

    // Memory Write (CS 持续拉低)
    rlcd_write_framebuffer(framebuffer, RLCD_FB_SIZE);
}

void rlcd_flush_partial(const uint8_t *data, int x, int y, int w, int h)
{
    // ST7305 partial update 比较复杂，先用全屏刷新
    // TODO: 实现真正的局部刷新
    (void)x; (void)y; (void)w; (void)h;
    rlcd_flush(data);
}

void rlcd_clear(uint8_t color)
{
    uint8_t *buf = heap_caps_calloc(1, RLCD_FB_SIZE, MALLOC_CAP_DMA);
    if (buf) {
        memset(buf, color, RLCD_FB_SIZE);
        rlcd_flush(buf);
        free(buf);
    }
}

/* ── 像素操作（通过 LUT 转换坐标） ────────────── */
void rlcd_set_pixel(uint8_t *framebuffer, int x, int y, bool black)
{
    if (x < 0 || x >= RLCD_WIDTH || y < 0 || y >= RLCD_HEIGHT) return;

    uint32_t pixel_idx = (uint32_t)x * RLCD_HEIGHT + y;
    uint16_t buf_idx = s_pixel_index_lut[pixel_idx];
    uint8_t bit_mask = s_pixel_bit_lut[pixel_idx];

    if (black) {
        framebuffer[buf_idx] &= ~bit_mask;  // Black = bit clear
    } else {
        framebuffer[buf_idx] |= bit_mask;   // White = bit set
    }
}

void rlcd_sleep(void)
{
    rlcd_cmd(0x28);  // Display OFF
    rlcd_cmd(0x10);  // Sleep In
}

void rlcd_wake(void)
{
    rlcd_cmd(0x11);  // Sleep Out
    vTaskDelay(pdMS_TO_TICKS(200));
    rlcd_cmd(0x38);  // High Power Mode
    rlcd_cmd(0x29);  // Display ON
}
