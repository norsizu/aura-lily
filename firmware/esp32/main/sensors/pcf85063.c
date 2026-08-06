/**
 * PCF85063 RTC I2C 驱动
 */
#include "pcf85063.h"
#include "aura_config.h"
#include "driver/i2c.h"
#include "esp_log.h"

static const char *TAG = "pcf85063";

#define BCD_TO_DEC(x) (((x) >> 4) * 10 + ((x) & 0x0F))
#define DEC_TO_BCD(x) ((((x) / 10) << 4) | ((x) % 10))

esp_err_t pcf85063_init(void)
{
    ESP_LOGI(TAG, "Initializing PCF85063 RTC");
    // 控制寄存器 1: 正常模式
    uint8_t buf[2] = {0x00, 0x00};
    return i2c_master_write_to_device(I2C_PORT, PCF85063_ADDR, buf, 2, pdMS_TO_TICKS(100));
}

esp_err_t pcf85063_get_time(pcf85063_time_t *t)
{
    uint8_t reg = 0x04;  // Seconds register
    uint8_t data[7];
    esp_err_t ret = i2c_master_write_read_device(
        I2C_PORT, PCF85063_ADDR, &reg, 1, data, 7, pdMS_TO_TICKS(100));
    if (ret != ESP_OK) return ret;

    t->second  = BCD_TO_DEC(data[0] & 0x7F);
    t->minute  = BCD_TO_DEC(data[1] & 0x7F);
    t->hour    = BCD_TO_DEC(data[2] & 0x3F);
    t->day     = BCD_TO_DEC(data[3] & 0x3F);
    t->weekday = data[4] & 0x07;
    t->month   = BCD_TO_DEC(data[5] & 0x1F);
    t->year    = 2000 + BCD_TO_DEC(data[6]);

    return ESP_OK;
}

esp_err_t pcf85063_set_time(const pcf85063_time_t *t)
{
    uint8_t buf[8];
    buf[0] = 0x04;  // Start register
    buf[1] = DEC_TO_BCD(t->second);
    buf[2] = DEC_TO_BCD(t->minute);
    buf[3] = DEC_TO_BCD(t->hour);
    buf[4] = DEC_TO_BCD(t->day);
    buf[5] = t->weekday;
    buf[6] = DEC_TO_BCD(t->month);
    buf[7] = DEC_TO_BCD(t->year - 2000);

    return i2c_master_write_to_device(I2C_PORT, PCF85063_ADDR, buf, 8, pdMS_TO_TICKS(100));
}
