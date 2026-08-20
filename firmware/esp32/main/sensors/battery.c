/** Waveshare ESP32-S3-RLCD-4.2 battery monitor. */
#include "battery.h"
#include "aura_config.h"
#include "esp_log.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define BATTERY_WARMUP_COUNT 16
#define BATTERY_SAMPLE_COUNT 32
#define BATTERY_TRIM_COUNT 8
#define BATTERY_MIN_MV 3000
#define BATTERY_MAX_MV 4350

static const char *TAG = "battery";
static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali;
static bool s_ready;
static bool s_cali_ready;
static int s_filtered_mv;

static void sort_samples(int *samples, int count)
{
    for (int i = 1; i < count; ++i) {
        int value = samples[i];
        int j = i - 1;
        while (j >= 0 && samples[j] > value) {
            samples[j + 1] = samples[j];
            --j;
        }
        samples[j + 1] = value;
    }
}

int battery_percent_from_mv(int mv)
{
    if (mv <= 3300) return 0;
    if (mv <= 3500) return (mv - 3300) * 5 / 200;
    if (mv <= 3700) return 5 + (mv - 3500) * 15 / 200;
    if (mv <= 3850) return 20 + (mv - 3700) * 30 / 150;
    if (mv <= 4000) return 50 + (mv - 3850) * 30 / 150;
    if (mv <= 4200) return 80 + (mv - 4000) * 20 / 200;
    return 100;
}

esp_err_t battery_init(void)
{
    adc_oneshot_unit_init_cfg_t unit_cfg = {
        .unit_id = RLCD_BATTERY_ADC_UNIT,
        .ulp_mode = ADC_ULP_MODE_DISABLE,
    };
    esp_err_t err = adc_oneshot_new_unit(&unit_cfg, &s_adc);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "ADC unit init failed: %s", esp_err_to_name(err));
        return err;
    }
    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    err = adc_oneshot_config_channel(s_adc, RLCD_BATTERY_ADC_CHANNEL, &chan_cfg);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "BAT_ADC channel init failed: %s", esp_err_to_name(err));
        return err;
    }
#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id = RLCD_BATTERY_ADC_UNIT,
        .chan = RLCD_BATTERY_ADC_CHANNEL,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    if (adc_cali_create_scheme_curve_fitting(&cali_cfg, &s_cali) == ESP_OK)
        s_cali_ready = true;
#elif ADC_CALI_SCHEME_LINE_FITTING_SUPPORTED
    adc_cali_line_fitting_config_t cali_cfg = {
        .unit_id = RLCD_BATTERY_ADC_UNIT,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    if (adc_cali_create_scheme_line_fitting(&cali_cfg, &s_cali) == ESP_OK)
        s_cali_ready = true;
#endif
    s_ready = true;
    ESP_LOGI(TAG, "BAT_ADC ready on GPIO%d%s", RLCD_BATTERY_ADC_GPIO,
             s_cali_ready ? " (calibrated)" : " (uncalibrated)");
    return ESP_OK;
}

esp_err_t battery_read(int *percent, bool *valid)
{
    if (!percent || !valid) return ESP_ERR_INVALID_ARG;
    *valid = false;
    *percent = 0;
    if (!s_ready) return ESP_ERR_INVALID_STATE;

    for (int i = 0; i < BATTERY_WARMUP_COUNT; ++i) {
        int discarded = 0;
        esp_err_t err = adc_oneshot_read(s_adc, RLCD_BATTERY_ADC_CHANNEL, &discarded);
        if (err != ESP_OK) return err;
        vTaskDelay(pdMS_TO_TICKS(1));
    }

    int samples[BATTERY_SAMPLE_COUNT];
    for (int i = 0; i < BATTERY_SAMPLE_COUNT; ++i) {
        esp_err_t err = adc_oneshot_read(s_adc, RLCD_BATTERY_ADC_CHANNEL, &samples[i]);
        if (err != ESP_OK) return err;
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    sort_samples(samples, BATTERY_SAMPLE_COUNT);
    int raw_sum = 0;
    for (int i = BATTERY_TRIM_COUNT; i < BATTERY_SAMPLE_COUNT - BATTERY_TRIM_COUNT; ++i)
        raw_sum += samples[i];
    int raw = raw_sum / (BATTERY_SAMPLE_COUNT - 2 * BATTERY_TRIM_COUNT);
    int adc_mv = 0;
    if (s_cali_ready && adc_cali_raw_to_voltage(s_cali, raw, &adc_mv) != ESP_OK)
        s_cali_ready = false;
    if (!s_cali_ready) adc_mv = raw * 3100 / 4095;
    int battery_mv = (int)((float)adc_mv *
                           (RLCD_BATTERY_DIVIDER_TOP_OHM + RLCD_BATTERY_DIVIDER_BOTTOM_OHM) /
                           RLCD_BATTERY_DIVIDER_BOTTOM_OHM);
    if (battery_mv < BATTERY_MIN_MV || battery_mv > BATTERY_MAX_MV) return ESP_OK;
    if (s_filtered_mv == 0) s_filtered_mv = battery_mv;
    else s_filtered_mv = (s_filtered_mv * 3 + battery_mv + 2) / 4;
    *percent = battery_percent_from_mv(s_filtered_mv);
    *valid = true;
    ESP_LOGI(TAG, "raw=%d adc=%dmV battery=%dmV filtered=%dmV level=%d%%",
             raw, adc_mv, battery_mv, s_filtered_mv, *percent);
    return ESP_OK;
}
