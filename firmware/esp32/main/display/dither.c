/**
 * Floyd-Steinberg Dithering 实现
 * 对 ESP32-S3 优化: 使用 PSRAM 中间缓冲
 */
#include "dither.h"
#include "esp_heap_caps.h"
#include <string.h>
#include <stdlib.h>

void dither_floyd_steinberg(const uint8_t *gray_in, uint8_t *bit_out,
                            int width, int height)
{
    // 分配工作缓冲区在 PSRAM（避免溢出内部 SRAM）
    int16_t *err_buf = heap_caps_calloc(width * height, sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (!err_buf) {
        // 回退到阈值模式
        dither_threshold(gray_in, bit_out, width, height, 128);
        return;
    }

    // 拷贝灰度值到误差缓冲
    for (int i = 0; i < width * height; i++) {
        err_buf[i] = gray_in[i];
    }

    // 清零输出
    memset(bit_out, 0, (width * height + 7) / 8);

    for (int y = 0; y < height; y++) {
        for (int x = 0; x < width; x++) {
            int idx = y * width + x;
            int16_t old_val = err_buf[idx];

            // 阈值化
            uint8_t new_val = (old_val > 127) ? 255 : 0;
            int16_t error = old_val - new_val;

            // 写入 1-bit 输出 (0=黑, 1=白)
            if (new_val > 0) {
                int byte_idx = idx / 8;
                int bit_idx = 7 - (idx % 8);
                bit_out[byte_idx] |= (1 << bit_idx);
            }

            // 扩散误差 (Floyd-Steinberg 系数)
            if (x + 1 < width)
                err_buf[idx + 1] += error * 7 / 16;
            if (y + 1 < height) {
                if (x > 0)
                    err_buf[idx + width - 1] += error * 3 / 16;
                err_buf[idx + width] += error * 5 / 16;
                if (x + 1 < width)
                    err_buf[idx + width + 1] += error * 1 / 16;
            }
        }
    }

    heap_caps_free(err_buf);
}

void dither_threshold(const uint8_t *gray_in, uint8_t *bit_out,
                      int width, int height, uint8_t threshold)
{
    int total = width * height;
    memset(bit_out, 0, (total + 7) / 8);

    for (int i = 0; i < total; i++) {
        if (gray_in[i] > threshold) {
            bit_out[i / 8] |= (1 << (7 - (i % 8)));
        }
    }
}
