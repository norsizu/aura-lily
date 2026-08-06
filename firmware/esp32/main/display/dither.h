/**
 * Floyd-Steinberg Dithering — 灰度 → 1-bit 转换
 */
#pragma once
#include <stdint.h>
#include <stddef.h>

/**
 * Floyd-Steinberg dithering
 * @param gray_in   输入灰度图 (width * height bytes, 0=黑 255=白)
 * @param bit_out   输出 1-bit packed (width * height / 8 bytes)
 * @param width     图像宽度
 * @param height    图像高度
 */
void dither_floyd_steinberg(const uint8_t *gray_in, uint8_t *bit_out,
                            int width, int height);

/**
 * 简单阈值二值化（快速模式）
 */
void dither_threshold(const uint8_t *gray_in, uint8_t *bit_out,
                      int width, int height, uint8_t threshold);
