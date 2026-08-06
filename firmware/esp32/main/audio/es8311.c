/**
 * ES8311 I2C 控制 — 低功耗音频编解码芯片
 * 基于 Waveshare ESP32-S3-RLCD-4.2 官方 SDK 寄存器配置
 *
 * MCLK = 4.096MHz (ESP32 I2S driver: 16kHz × 256)
 * Sample rate = 16kHz, 32-bit TDM-compatible I2S Philips, Slave mode
 */
#include "es8311.h"
#include "aura_config.h"
#include "driver/i2c.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "es8311";

/* ── I2C 读写 ──────────────────────────────────────── */

static esp_err_t es8311_write_reg(uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = {reg, val};
    esp_err_t ret = i2c_master_write_to_device(I2C_PORT, ES8311_ADDR, buf, 2, pdMS_TO_TICKS(100));
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Write reg 0x%02X=0x%02X FAILED: %s", reg, val, esp_err_to_name(ret));
    }
    return ret;
}

static uint8_t es8311_read_reg(uint8_t reg)
{
    uint8_t val = 0;
    i2c_master_write_read_device(I2C_PORT, ES8311_ADDR, &reg, 1, &val, 1, pdMS_TO_TICKS(100));
    return val;
}

/* ── 初始化 ────────────────────────────────────────── */

esp_err_t es8311_prepare_pa_muted(void)
{
    /* Set the output latch before switching the pin to output mode. */
    gpio_set_level(PA_CTRL_PIN, 0);
    gpio_config_t pa_conf = {
        .pin_bit_mask = (1ULL << PA_CTRL_PIN),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t ret = gpio_config(&pa_conf);
    if (ret == ESP_OK) {
        gpio_set_level(PA_CTRL_PIN, 0);
    }
    return ret;
}

esp_err_t es8311_init(void)
{
    ESP_LOGI(TAG, "Initializing ES8311 (addr=0x%02X)", ES8311_ADDR);

    /* Keep the external amplifier off while codec clocks and references settle. */
    esp_err_t ret = es8311_prepare_pa_muted();
    if (ret != ESP_OK) {
        return ret;
    }

    // 验证 chip ID
    uint8_t id1 = es8311_read_reg(0xFD);
    uint8_t id2 = es8311_read_reg(0xFE);
    uint8_t ver = es8311_read_reg(0xFF);
    ESP_LOGI(TAG, "Chip ID: 0x%02X 0x%02X, Version: 0x%02X", id1, id2, ver);

    // ── Step 1: Reset ──
    es8311_write_reg(0x00, 0x1F);    // Soft reset
    vTaskDelay(pdMS_TO_TICKS(20));
    es8311_write_reg(0x00, 0x80);    // Power up, slave mode (bit6=0)

    // ── Step 2: Clock config for MCLK=4.096MHz, Fs=16kHz ──
    // REG01: MCLK from pin (bit7=0), normal MCLK (bit6=0)
    es8311_write_reg(0x01, 0x3F);

    // REG02: pre_div=1 (bits4-0=0x00), pre_multi=1 (bits7-5=0x00), adc_amp=0
    //   coeff: pre_div=0x01 → reg value = (pre_div-1) | (pre_multi << 5)
    //   pre_multi=1 → 0x00<<5 = 0x00
    //   pre_div=1 → 0x00
    es8311_write_reg(0x02, 0x00);

    // REG03: ADC osr = 0x10 (32x), fs_mode=0 (single speed)
    es8311_write_reg(0x03, 0x10);

    // REG04: DAC osr = 0x20 (64x)
    es8311_write_reg(0x04, 0x20);

    // REG05: adc_div=1, dac_div=1 → reg = ((adc_div-1)<<4) | (dac_div-1) = 0x00
    es8311_write_reg(0x05, 0x00);

    // REG06: bclk_div=4 (slave 模式下此值不影响实际 BCLK，但保持和 Waveshare SDK 一致)
    uint8_t reg06 = es8311_read_reg(0x06);
    reg06 &= 0xE0;
    reg06 |= 0x03;    // bclk_div=4 → 4-1=3
    es8311_write_reg(0x06, reg06);

    // REG07/08: LRCK divider = 256 (slave 模式下不影响实际 LRCK，保持 Waveshare SDK 值)
    uint8_t reg07 = es8311_read_reg(0x07);
    reg07 &= 0xC0;
    reg07 |= 0x00;    // lrck_h
    es8311_write_reg(0x07, reg07);
    es8311_write_reg(0x08, 0xFF);    // lrck_l

    // ── Step 3: I2S format ──
    // REG09 (SDP In / DAC): I2S normal format, 16-bit (matches TDM 16-bit)
    //   bits[1:0]=00 (I2S normal), bits[3:2]=11 (16-bit)
    es8311_write_reg(0x09, 0x0C);

    // REG0A (SDP Out / ADC): I2S normal format, 16-bit
    es8311_write_reg(0x0A, 0x0C);

    // ── Step 4: System config (from Waveshare SDK) ──
    es8311_write_reg(0x0B, 0x00);    // System REG0B
    es8311_write_reg(0x0C, 0x00);    // System REG0C
    es8311_write_reg(0x10, 0x1F);    // System REG10: chip power up
    es8311_write_reg(0x11, 0x7F);    // System REG11: chip power up

    // REG0D: ADC/DAC reference power
    es8311_write_reg(0x0D, 0x01);    // Power on

    // REG0E: DAC power
    es8311_write_reg(0x0E, 0x02);    // DAC on

    // REG12: DAC volume = 0 (0dB, not muted)
    es8311_write_reg(0x12, 0x00);

    // REG13: Output driver
    es8311_write_reg(0x13, 0x10);

    // REG14: ADC power
    es8311_write_reg(0x14, 0x1A);

    // ── Step 5: ADC HPF config (from Waveshare SDK) ──
    es8311_write_reg(0x1B, 0x0A);
    es8311_write_reg(0x1C, 0x6A);

    // ── Step 6: GPIO/Reference ──
    es8311_write_reg(0x44, 0x08);    // GPIO config

    // ── Step 7: Start DAC ──
    // 从 Waveshare SDK es8311_start: 取消 DAC tristate
    uint8_t dac_iface = es8311_read_reg(0x09);
    dac_iface &= ~(1 << 6);    // bit6=0: DAC data normal (not tristate)
    es8311_write_reg(0x09, dac_iface);

    // Keep DAC muted until I2S has started and transmitted zero samples.
    es8311_write_reg(0x31, 0x60);    // DAC mute
    es8311_write_reg(0x32, 0xBF);    // DAC volume (high, ~-3dB)

    // ADC settings
    es8311_write_reg(0x17, 0xBF);    // ADC volume

    ESP_LOGI(TAG, "ES8311 initialized muted (MCLK=4.096MHz, Fs=16kHz, Slave)");
    return ESP_OK;
}

esp_err_t es8311_set_volume(int volume)
{
    if (volume < 0) volume = 0;
    if (volume > 100) volume = 100;
    uint8_t reg_val = (uint8_t)(volume * 255 / 100);
    return es8311_write_reg(0x32, reg_val);
}

esp_err_t es8311_mute(bool mute)
{
    if (mute) {
        /* Disable the PA first so codec/clock transitions cannot reach the speaker. */
        gpio_set_level(PA_CTRL_PIN, 0);
        return es8311_write_reg(0x31, 0x60);
    } else {
        /* Unmute the settled DAC first, then connect it to the speaker. */
        esp_err_t ret = es8311_write_reg(0x31, 0x00);
        if (ret != ESP_OK) {
            return ret;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
        gpio_set_level(PA_CTRL_PIN, 1);
    }
    return ESP_OK;
}
