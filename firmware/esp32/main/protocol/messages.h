/**
 * WebSocket 消息协议辅助函数
 */
#pragma once
#include <stdint.h>

// 姿势名 → 索引
int msg_pose_to_index(const char *pose_name);

// 场景名 → 索引
int msg_scene_to_index(const char *scene_name);

/* ── E1 情绪集合（与服务端协议对齐）────────────────────────────
 * dialogue.payload.emotion 与 {"type":"emotion"} 都只允许这 8 个值。
 * 顺序即 emotion_115.bin 里情绪脸的资源索引，改动会同时破坏资源布局。
 */
#define E1_EMOTION_COUNT 8

// E1 索引 → 情绪名；越界一律回 "neutral"
const char *msg_emotion_name(int index);

// 情绪名 → E1 索引；NULL/空串/非 E1 值返回 -1
int msg_emotion_to_index(const char *emotion);

// 索引 → 姿势名
const char *msg_index_to_pose(int index);
