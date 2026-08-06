/**
 * 顶部状态栏绘制
 */
#pragma once
#include <stdint.h>
#include "display/renderer.h"

void status_bar_draw(uint8_t *graybuf, int width, const aura_state_t *state);
