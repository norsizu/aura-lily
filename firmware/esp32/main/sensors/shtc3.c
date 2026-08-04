/**
 * SHTC3 高精度温湿度传感器 I2C 驱动
 */
#include "shtc3.h"
#include "aura_config.h"
#include "driver/i2c.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "shtc3";

static esp_err_t shtc3_cmd(uint16_t cmd)
{
    uint8_t buf[2] = {cmd >> 8, cmd & 0xFF};
    return i2c_master_write_to_device(I2C_PORT, SHTC3_ADDR, buf, 2, pdMS_TO_TICKS(100));
}

esp_err_t shtc3_init(void)
{
    ESP_LOGI(TAG, "Initializing SHTC3");
    // 唤醒
    shtc3_cmd(0x3517);
    vTaskDelay(pdMS_TO_TICKS(1));
    // 软复位
    shtc3_cmd(0x805D);
    vTaskDelay(pdMS_TO_TICKS(1));
    ESP_LOGI(TAG, "SHTC3 initialized");
    return ESP_OK;
}

esp_err_t shtc3_read(float *temperature, float *humidity)
{
    // 唤醒
    shtc3_cmd(0x3517);
    vTaskDelay(pdMS_TO_TICKS(1));

    // 触发测量 (Clock Stretching, T first)
    shtc3_cmd(0x7CA2);
    vTaskDelay(pdMS_TO_TICKS(15));

    // 读取 6 bytes: T_MSB, T_LSB, T_CRC, H_MSB, H_LSB, H_CRC
    uint8_t data[6];
    esp_err_t ret = i2c_master_read_from_device(I2C_PORT, SHTC3_ADDR, data, 6, pdMS_TO_TICKS(100));
    if (ret != ESP_OK) return ret;

    uint16_t raw_t = (data[0] << 8) | data[1];
    uint16_t raw_h = (data[3] << 8) | data[4];

    *temperature = -45.0f + 175.0f * (float)raw_t / 65535.0f;
    *humidity = 100.0f * (float)raw_h / 65535.0f;

    // 休眠
    shtc3_cmd(0xB098);

    return ESP_OK;
}
