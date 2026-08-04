/**
 * SD 卡挂载 (SDMMC 1-bit 模式)
 */
#include "sd_card.h"
#include "aura_config.h"
#include "esp_vfs_fat.h"
#include "sdmmc_cmd.h"
#include "driver/sdmmc_host.h"
#include "driver/gpio.h"
#include "esp_log.h"

static const char *TAG = "sdcard";
static bool s_mounted = false;
static sdmmc_card_t *s_card = NULL;

esp_err_t sd_card_init(void)
{
    ESP_LOGI(TAG, "Mounting SD card at %s", SD_MOUNT_POINT);

    esp_vfs_fat_sdmmc_mount_config_t mount_cfg = {
        .format_if_mount_failed = false,
        .max_files = 5,
        .allocation_unit_size = 16 * 1024,
    };

    // TF 的 D3 与 SPI-CS 复用，1-bit 模式下也先强拉高，避免卡误入 SPI/悬空状态。
    gpio_config_t d3_cfg = {
        .pin_bit_mask = BIT64(SDMMC_D3_PIN),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&d3_cfg));
    gpio_set_level(SDMMC_D3_PIN, 1);

    sdmmc_host_t host = SDMMC_HOST_DEFAULT();

    sdmmc_slot_config_t slot = SDMMC_SLOT_CONFIG_DEFAULT();
    slot.width = 1;
    slot.clk = SDMMC_CLK_PIN;
    slot.cmd = SDMMC_CMD_PIN;
    slot.d0 = SDMMC_DATA_PIN;
    slot.d3 = SDMMC_D3_PIN;

    sdmmc_card_t *card;
    esp_err_t ret = esp_vfs_fat_sdmmc_mount(SD_MOUNT_POINT, &host, &slot, &mount_cfg, &card);

    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "SD card mount failed: %s", esp_err_to_name(ret));
        gpio_reset_pin(SDMMC_D3_PIN);
        return ret;
    }

    sdmmc_card_print_info(stdout, card);
    s_card = card;
    s_mounted = true;
    gpio_reset_pin(SDMMC_D3_PIN);
    ESP_LOGI(TAG, "SD card mounted successfully");
    return ESP_OK;
}

bool sd_card_is_mounted(void)
{
    return s_mounted;
}

sdmmc_card_t *sd_card_get_card(void)
{
    return s_card;
}

esp_err_t sd_card_unmount(void)
{
    if (!s_mounted || !s_card) {
        return ESP_ERR_INVALID_STATE;
    }
    esp_vfs_fat_sdcard_unmount(SD_MOUNT_POINT, s_card);
    s_mounted = false;
    ESP_LOGI(TAG, "SD card unmounted from VFS");
    return ESP_OK;
}
