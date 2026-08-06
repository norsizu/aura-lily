/**
 * minigame_render.h — 白日梦大冒险 screen drawing API
 */
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

esp_err_t mg_render_init(void);
void mg_render_frame(void);

/** 主菜单弹窗；volume_mode=true 时绘制音量二级菜单。 */
void mg_draw_main_menu(uint8_t *graybuf, int sel, bool volume_mode,
                       const char *header, const char *const *opts,
                       int opt_count);

/** 服装店信息面板（叠加在已绘制的 graybuf 上）
 *  @param sel    当前浏览的商品 0-5
 *  @param coins  玩家当前豆豆数
 *  @param owned  当前商品是否已购买
 *  @param name   商品名
 *  @param price  商品价格
 *  @param tag    商品风格标签
 */
void mg_draw_shop_panel(uint8_t *graybuf, int sel, int coins, bool owned,
                        const char *name, int price, const char *tag,
                        const char *title, const char *currency,
                        const char *owned_label, const char *new_label,
                        const char *price_label, const char *style_label,
                        const char *status_label, const char *owned_status,
                        const char *not_owned_status, const char *balance_label,
                        const char *after_buy_label, const char *short_label,
                        const char *not_enough_label, const char *wear_label,
                        const char *buy_label, const char *footer);

void mg_draw_dessert_panel(uint8_t *graybuf, int sel, int item_count, int coins,
                           const char *name, int price, const char *effect,
                           const char *title, const char *currency,
                           const char *price_label, const char *effect_label,
                           const char *balance_label, const char *buy_label,
                           const char *not_enough_label, const char *pending_label,
                           bool pending, const char *footer);

/** 衣柜详情面板（只展示已拥有服装）
 *  @param outfit_idx 当前高亮的服装序号
 *  @param page       已拥有列表中的页码（0-based）
 *  @param page_count 已拥有服装数量
 *  @param name       服装名
 */
void mg_draw_wardrobe_panel(uint8_t *graybuf, int outfit_idx,
                            int page, int page_count, const char *name,
                            const char *title, const char *owned_label,
                            const char *id_label, const char *status_label,
                            const char *wearable_label, const char *source_label,
                            const char *wardrobe_label, const char *wear_label,
                            const char *footer);
