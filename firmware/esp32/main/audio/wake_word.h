#pragma once
#include "esp_err.h"
#include <stdint.h>
#include <stdbool.h>

/**
 * 初始化唤醒词检测 (MultiNet 命令词识别)
 * 使用 "豆豆" (pinyin: dou dou) 作为唤醒词
 */
esp_err_t wake_word_init(void);

/**
 * 喂入 16-bit mono 16kHz PCM 数据
 * 同时将数据存入环形缓冲区 (最近 ~2 秒)
 * @param pcm  PCM 数据指针
 * @param samples 采样数 (应为 get_feed_size 的返回值)
 */
void wake_word_feed(const int16_t *pcm, int samples);

/** 检查是否检测到唤醒词 (读后自动清除) */
bool wake_word_detected(void);

/** 启用检测 */
void wake_word_start(void);

/** 停止检测 */
void wake_word_stop(void);

/** 获取每次 feed 需要的采样数 */
int wake_word_get_feed_size(void);

/**
 * 获取环形缓冲区中的残留音频 (唤醒词之后的部分)
 * 检测到唤醒词后调用，将缓冲区中最近的音频拷贝到 out_buf
 * @param out_buf   输出缓冲区
 * @param max_samples  缓冲区最大采样数
 * @return 实际拷贝的采样数
 */
int wake_word_get_trailing_audio(int16_t *out_buf, int max_samples);

/** 清空环形缓冲区 */
void wake_word_clear_ring_buffer(void);
