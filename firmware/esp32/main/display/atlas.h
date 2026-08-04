/**
 * Atlas 加载器 — 9 姿势九宫格解析
 */
#pragma once
#include <stdint.h>
#include "esp_err.h"

typedef struct {
    uint8_t *data;          // 完整 atlas 灰度数据 (PSRAM)
    int atlas_width;        // 总宽度
    int atlas_height;       // 总高度
    int cell_width;         // 单格宽度 = atlas_width / 3
    int cell_height;        // 单格高度 = atlas_height / 3
} atlas_t;

/**
 * 从 SD 卡或 SPIFFS 加载 atlas (需要先解码为灰度)
 * @param path  文件路径 (如 "/sdcard/outfits/school_uniform.bin")
 * @param atlas 输出结构
 */
esp_err_t atlas_load(const char *path, atlas_t *atlas);

/**
 * 提取单个姿势到缓冲区
 * @param atlas  已加载的 atlas
 * @param index  姿势索引 (0-8)
 * @param out    输出灰度缓冲 (cell_width * cell_height bytes)
 */
void atlas_extract_pose(const atlas_t *atlas, int index, uint8_t *out);

/**
 * 释放 atlas 内存
 */
void atlas_free(atlas_t *atlas);
