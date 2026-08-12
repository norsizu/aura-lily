#pragma once

#include <stdint.h>
#include "renderer.h"

void panels_draw_info_board(uint8_t *graybuf, int width, int height,
                            const aura_state_t *state);
