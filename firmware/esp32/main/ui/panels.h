/**
 * 面板系统 — 左侧状态 / 右侧Agent / 底部对话
 */
#pragma once
#include <stdint.h>
#include "renderer.h"

void panels_draw_left(uint8_t *graybuf, int width, int height, const aura_state_t *state);
void panels_draw_right(uint8_t *graybuf, int width, int height, const aura_state_t *state);
void panels_draw_dialogue(uint8_t *graybuf, int width, int height, const aura_state_t *state);
