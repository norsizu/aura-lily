/**
 * WebSocket 消息协议辅助函数
 */
#pragma once
#include <stdint.h>

// 姿势名 → 索引
int msg_pose_to_index(const char *pose_name);

// 场景名 → 索引
int msg_scene_to_index(const char *scene_name);

// 索引 → 姿势名
const char *msg_index_to_pose(int index);
