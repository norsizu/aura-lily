/**
 * 消息协议辅助
 */
#include "messages.h"
#include <string.h>

static const char *POSE_NAMES[] = {
    "idle_a", "idle_b", "listen_a",
    "listen_b", "thinking", "speak_a",
    "speak_b", "happy", "proud",
};

static const char *SCENE_NAMES[] = {
    "home.living_room", "home.study", "home.bedroom", "home.kitchen",
    "home.balcony", "outside.neighborhood", "outside.cafe", "outside.shop",
    "outside.park", "outside.mall",
};

int msg_pose_to_index(const char *name)
{
    for (int i = 0; i < 9; i++) {
        if (strcmp(name, POSE_NAMES[i]) == 0) return i;
    }
    /* 未知名称返回 -1：不覆盖设备本地随机姿势 */
    return -1;
}

int msg_scene_to_index(const char *name)
{
    if (!name) return -1;
    /* Compatibility with the original three-scene protocol. */
    if (strcmp(name, "home") == 0 || strcmp(name, "living_room") == 0) return 0;
    if (strcmp(name, "desk") == 0 || strcmp(name, "study") == 0) return 1;
    if (strcmp(name, "bedroom") == 0) return 2;
    if (strcmp(name, "neighborhood_food") == 0) return 5;
    if (strcmp(name, "quiet_stop") == 0) return 6;
    if (strcmp(name, "daily_shop") == 0) return 7;
    if (strcmp(name, "nearby_walk") == 0 || strcmp(name, "park") == 0) return 8;
    if (strcmp(name, "mall") == 0) return 9;
    for (int i = 0; i < 10; i++) {
        if (strcmp(name, SCENE_NAMES[i]) == 0) return i;
    }
    return -1;
}

const char *msg_index_to_pose(int index)
{
    if (index >= 0 && index < 9) return POSE_NAMES[index];
    return "idle_a";
}
