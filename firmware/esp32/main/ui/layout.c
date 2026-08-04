/**
 * UI 布局管理
 */
#include "layout.h"
#include "aura_config.h"

void layout_init(void) {}

rect_t layout_get_status_bar(void)
{
    return (rect_t){0, 0, RLCD_WIDTH, STATUS_BAR_HEIGHT};
}

rect_t layout_get_character_area(void)
{
    return (rect_t){
        40, STATUS_BAR_HEIGHT + 5,
        RLCD_WIDTH - 80, RLCD_HEIGHT - STATUS_BAR_HEIGHT - 50
    };
}

rect_t layout_get_text_area(void)
{
    return (rect_t){10, RLCD_HEIGHT - 45, RLCD_WIDTH - 20, 40};
}
