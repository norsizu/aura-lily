/**
 * Atlas 九宫格加载与姿势提取
 *
 * Atlas 布局:
 *  ┌─────┬─────┬─────┐
 *  │  0  │  1  │  2  │  neutral, proud, thinking
 *  ├─────┼─────┼─────┤
 *  │  3  │  4  │  5  │  surprised, apologetic, assertive
 *  ├─────┼─────┼─────┤
 *  │  6  │  7  │  8  │  shy, excited, relaxed
 *  └─────┴─────┴─────┘
 *
 * Supports two on-disk formats (auto-detected by file size):
 *   8-bit raw:    header(8) + w*h bytes          ~527 KB per outfit
 *   2-bit packed: header(8) + ceil(w*h/4) bytes  ~132 KB per outfit
 *   Pixel codes (2-bit): 0=transparent(0xFF), 1=black(0x00), 2=white(0xFE), 3=gray(0x80)
 */
#include "atlas.h"
#include "aura_config.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include <stdio.h>
#include <string.h>

static const char *TAG = "atlas";

/* Lookup table for 2-bit pixel codes → 8-bit pixel values */
static const uint8_t s_2bit_lut[4] = {0xFF, 0x00, 0xFE, 0x80};

static void unpack_2bit(const uint8_t *packed, size_t packed_size,
                         uint8_t *out, size_t out_size)
{
    size_t out_idx = 0;
    for (size_t i = 0; i < packed_size && out_idx < out_size; i++) {
        uint8_t byte = packed[i];
        for (int shift = 6; shift >= 0 && out_idx < out_size; shift -= 2) {
            out[out_idx++] = s_2bit_lut[(byte >> shift) & 0x03];
        }
    }
}

esp_err_t atlas_load(const char *path, atlas_t *atlas)
{
    ESP_LOGI(TAG, "Loading atlas: %s", path);

    FILE *f = fopen(path, "rb");
    if (!f) {
        ESP_LOGE(TAG, "Failed to open: %s", path);
        return ESP_ERR_NOT_FOUND;
    }

    uint32_t w, h;
    fread(&w, 4, 1, f);
    fread(&h, 4, 1, f);

    atlas->atlas_width = (int)w;
    atlas->atlas_height = (int)h;
    atlas->cell_width = w / ATLAS_COLS;
    atlas->cell_height = h / ATLAS_ROWS;

    size_t pixel_count = (size_t)w * h;
    size_t packed_size = (pixel_count + 3) / 4;

    /* Determine format by remaining bytes in file */
    long pos = ftell(f);
    fseek(f, 0, SEEK_END);
    long file_size = ftell(f);
    fseek(f, pos, SEEK_SET);
    size_t data_bytes = (size_t)(file_size - pos);

    atlas->data = heap_caps_malloc(pixel_count, MALLOC_CAP_SPIRAM);
    if (!atlas->data) {
        ESP_LOGE(TAG, "PSRAM alloc failed for %zu bytes", pixel_count);
        fclose(f);
        return ESP_ERR_NO_MEM;
    }

    if (data_bytes == pixel_count) {
        /* Legacy 8-bit raw format */
        size_t rd = fread(atlas->data, 1, pixel_count, f);
        if (rd != pixel_count) ESP_LOGW(TAG, "Short read (8-bit): %zu/%zu", rd, pixel_count);
        ESP_LOGI(TAG, "Format: 8-bit raw (%zu KB)", data_bytes / 1024);
    } else if (data_bytes == packed_size) {
        /* 2-bit packed format – unpack into PSRAM buffer */
        uint8_t *packed = malloc(packed_size);
        if (!packed) {
            ESP_LOGE(TAG, "Temp alloc failed (%zu bytes)", packed_size);
            heap_caps_free(atlas->data);
            atlas->data = NULL;
            fclose(f);
            return ESP_ERR_NO_MEM;
        }
        size_t rd = fread(packed, 1, packed_size, f);
        if (rd != packed_size) ESP_LOGW(TAG, "Short read (2-bit): %zu/%zu", rd, packed_size);
        unpack_2bit(packed, packed_size, atlas->data, pixel_count);
        free(packed);
        ESP_LOGI(TAG, "Format: 2-bit packed (%zu KB -> %zu KB unpacked)",
                 data_bytes / 1024, pixel_count / 1024);
    } else {
        ESP_LOGE(TAG, "Unknown format: file data=%zu, expected %zu (8-bit) or %zu (2-bit)",
                 data_bytes, pixel_count, packed_size);
        heap_caps_free(atlas->data);
        atlas->data = NULL;
        fclose(f);
        return ESP_ERR_INVALID_SIZE;
    }

    fclose(f);
    ESP_LOGI(TAG, "Atlas loaded: %dx%d, cell: %dx%d",
             atlas->atlas_width, atlas->atlas_height,
             atlas->cell_width, atlas->cell_height);
    return ESP_OK;
}

void atlas_extract_pose(const atlas_t *atlas, int index, uint8_t *out)
{
    if (!atlas || !atlas->data || index < 0 || index >= POSE_COUNT) {
        return;
    }

    int col = index % ATLAS_COLS;
    int row = index / ATLAS_COLS;
    int x_off = col * atlas->cell_width;
    int y_off = row * atlas->cell_height;

    for (int y = 0; y < atlas->cell_height; y++) {
        int src_offset = (y_off + y) * atlas->atlas_width + x_off;
        int dst_offset = y * atlas->cell_width;
        memcpy(out + dst_offset, atlas->data + src_offset, atlas->cell_width);
    }
}

void atlas_free(atlas_t *atlas)
{
    if (atlas && atlas->data) {
        heap_caps_free(atlas->data);
        atlas->data = NULL;
    }
}
