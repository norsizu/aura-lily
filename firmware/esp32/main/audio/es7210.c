/**
 * ES7210 四通道 ADC 驱动 — 严格按照 Espressif esp_codec_dev 官方实现
 * Waveshare S3 RLCD 4.2: 使用 MIC1 + MIC3
 */
#include "es7210.h"
#include "aura_config.h"
#include "driver/i2c.h"
#include "esp_log.h"

static const char *TAG = "es7210";

/* ── 寄存器定义 ───────────────────────── */
#define REG00_RESET          0x00
#define REG01_CLK_OFF        0x01
#define REG02_MAINCLK        0x02
#define REG03_MASTER_CLK     0x03
#define REG06_POWER_DOWN     0x06
#define REG07_OSR            0x07
#define REG08_MODE           0x08
#define REG09_TIMING0        0x09
#define REG0A_TIMING1        0x0A
#define REG11_SDP1           0x11
#define REG12_SDP2           0x12
#define REG14_ADC12_MUTE     0x14
#define REG15_ADC34_MUTE     0x15
#define REG20_ADC34_HPF2     0x20
#define REG21_ADC34_HPF1     0x21
#define REG22_ADC12_HPF1     0x22
#define REG23_ADC12_HPF2     0x23
#define REG40_ANALOG         0x40
#define REG41_MIC12_BIAS     0x41
#define REG42_MIC34_BIAS     0x42
#define REG43_MIC1_GAIN      0x43
#define REG44_MIC2_GAIN      0x44
#define REG45_MIC3_GAIN      0x45
#define REG46_MIC4_GAIN      0x46
#define REG47_MIC1_PWR       0x47
#define REG48_MIC2_PWR       0x48
#define REG49_MIC3_PWR       0x49
#define REG4A_MIC4_PWR       0x4A
#define REG4B_MIC12_PWR      0x4B
#define REG4C_MIC34_PWR      0x4C

/* ── I2C 底层 ──────────────────────────── */
static esp_err_t wr(uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = {reg, val};
    esp_err_t ret = i2c_master_write_to_device(I2C_PORT, ES7210_ADDR, buf, 2, pdMS_TO_TICKS(100));
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Write 0x%02X=0x%02X FAIL (0x%x)", reg, val, ret);
    }
    return ret;
}

static esp_err_t rd(uint8_t reg, uint8_t *val)
{
    return i2c_master_write_read_device(I2C_PORT, ES7210_ADDR, &reg, 1, val, 1, pdMS_TO_TICKS(100));
}

static esp_err_t update_bit(uint8_t reg, uint8_t mask, uint8_t val)
{
    uint8_t old = 0;
    esp_err_t ret = rd(reg, &old);
    if (ret != ESP_OK) return ret;
    return wr(reg, (old & ~mask) | (val & mask));
}

/* ── MIC 选择 (照搬官方) ────────────────── */
#define MIC1  (1 << 0)
#define MIC3  (1 << 2)

/* 增益: 37.5 dB = gain value 0x0E (最大增益，因为 MEMS 麦克风信号弱) */
#define DEFAULT_GAIN  0x0E  /* 37.5 dB */

static int mic_select(void)
{
    int ret = 0;

    /* 1. 所有 MIC gain bit4 清零 (关闭) */
    for (int i = 0; i < 4; i++) {
        ret |= update_bit(REG43_MIC1_GAIN + i, 0x10, 0x00);
    }
    /* 2. MIC pair 先断电 */
    ret |= wr(REG4B_MIC12_PWR, 0xFF);
    ret |= wr(REG4C_MIC34_PWR, 0xFF);

    /* 3. 启用所有 4 个 MIC (和小智 TDM 模式一致) */
    ESP_LOGI(TAG, "Enable MIC1+MIC2+MIC3+MIC4 (all 4 channels)");

    /* ADC1/2 + ADC3/4 时钟全开 */
    ret |= update_bit(REG01_CLK_OFF, 0x1F, 0x00);

    /* MIC12 + MIC34 供电 */
    ret |= wr(REG4B_MIC12_PWR, 0x00);
    ret |= wr(REG4C_MIC34_PWR, 0x00);

    /* 所有 4 个 MIC gain enable + 设置增益 */
    for (int i = 0; i < 4; i++) {
        ret |= update_bit(REG43_MIC1_GAIN + i, 0x10, 0x10); /* gain enable */
        ret |= update_bit(REG43_MIC1_GAIN + i, 0x0F, DEFAULT_GAIN);
    }

    /* 5. TDM 模式 */
    ret |= wr(REG12_SDP2, 0x02);  /* TDM enabled */

    return ret;
}

/* ── 初始化 (照搬 es7210_open + es7210_start) ─── */
esp_err_t es7210_init(void)
{
    ESP_LOGI(TAG, "Initializing ES7210 (addr=0x%02X)", ES7210_ADDR);
    int ret = 0;

    /* ── Phase 1: Reset & Clock ────── */
    ret |= wr(REG00_RESET, 0xFF);               /* 软复位 */
    vTaskDelay(pdMS_TO_TICKS(20));
    ret |= wr(REG00_RESET, 0x41);               /* 复位释放 */
    ret |= wr(REG01_CLK_OFF, 0x3F);             /* 时钟初始化 */

    /* ── Phase 2: Timing ──────────── */
    ret |= wr(REG09_TIMING0, 0x30);             /* 状态周期 */
    ret |= wr(REG0A_TIMING1, 0x30);             /* 上电周期 */

    /* ── Phase 3: HPF (去 DC offset) ── */
    ret |= wr(REG23_ADC12_HPF2, 0x2A);
    ret |= wr(REG22_ADC12_HPF1, 0x0A);
    ret |= wr(REG20_ADC34_HPF2, 0x0A);
    ret |= wr(REG21_ADC34_HPF1, 0x2A);

    /* ── Phase 4: Slave 模式 ───────── */
    ret |= update_bit(REG08_MODE, 0x01, 0x00);  /* slave */

    /* ── Phase 5: 模拟配置 ─────────── */
    ret |= wr(REG40_ANALOG, 0x43);              /* VDDA=3.3V, VMID 5K */
    ret |= wr(REG41_MIC12_BIAS, 0x70);          /* MIC bias 2.87V */
    ret |= wr(REG42_MIC34_BIAS, 0x70);

    /* ── Phase 6: 采样率 ──────────── */
    ret |= wr(REG07_OSR, 0x20);                 /* OSR */
    ret |= wr(REG02_MAINCLK, 0xC1);             /* DLL */

    /* ── Phase 7: I2S 格式 ─────────── */
    /* TDM mode, 16-bit, I2S normal format
     * REG12: 0x02 = TDM mode enabled
     * REG11: bits[7:5]=011 (16-bit)=0x60, bits[1:0]=00 (I2S normal) */
    ret |= wr(REG12_SDP2, 0x02);                /* TDM enable */
    ret |= update_bit(REG11_SDP1, 0xE3, 0x60);  /* 16-bit + I2S normal */
    ret |= mic_select();

    if (ret != 0) {
        ESP_LOGE(TAG, "Configuration had I2C errors!");
    }

    /* ── Phase 9: 启动序列 (es7210_start) ── */
    /* 读回当前 CLK_OFF 值作为 clock_reg_value */
    uint8_t clk_reg = 0;
    rd(REG01_CLK_OFF, &clk_reg);

    ret = 0;
    ret |= wr(REG01_CLK_OFF, clk_reg);          /* 恢复时钟 */
    ret |= wr(REG06_POWER_DOWN, 0x00);           /* 解除 power down */
    ret |= wr(REG40_ANALOG, 0x43);               /* 模拟再确认 */
    ret |= wr(REG47_MIC1_PWR, 0x08);             /* MIC 通道上电 */
    ret |= wr(REG48_MIC2_PWR, 0x08);
    ret |= wr(REG49_MIC3_PWR, 0x08);
    ret |= wr(REG4A_MIC4_PWR, 0x08);
    /* mic_select 再次调用 (官方 start 序列) */
    ret |= mic_select();
    ret |= wr(REG40_ANALOG, 0x43);
    ret |= wr(REG00_RESET, 0x71);                /* 启动 ADC */
    ret |= wr(REG00_RESET, 0x41);

    /* ── Phase 10: Unmute ──────────── */
    ret |= update_bit(REG14_ADC12_MUTE, 0x03, 0x00);
    ret |= update_bit(REG15_ADC34_MUTE, 0x03, 0x00);

    if (ret != 0) {
        ESP_LOGE(TAG, "Start sequence had errors!");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "ES7210 initialized OK (MIC1+MIC3, 37.5dB, 16-bit I2S TDM, Slave)");
    return ESP_OK;
}

esp_err_t es7210_set_gain(int gain_db)
{
    uint8_t val;
    if (gain_db < 3) val = 0x00;
    else if (gain_db < 37) val = (uint8_t)(gain_db / 3);
    else val = 0x0E;

    update_bit(REG43_MIC1_GAIN, 0x0F, val);
    update_bit(REG45_MIC3_GAIN, 0x0F, val);
    return ESP_OK;
}
