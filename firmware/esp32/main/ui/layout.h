/**
 * UI 布局管理
 */
#pragma once
#include <stdint.h>

typedef struct {
    int x, y, w, h;
} rect_t;

void layout_init(void);
rect_t layout_get_status_bar(void);
rect_t layout_get_character_area(void);
rect_t layout_get_text_area(void);
