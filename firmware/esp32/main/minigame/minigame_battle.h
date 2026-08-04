/**
 * minigame_battle.h — Battle round logic for 白日梦大冒险
 */
#pragma once
#include "minigame_state.h"

/**
 * Initialise a fresh battle.
 * @param b              battle state to fill
 * @param enemy_index    index into mg_enemies[]
 * @param stat_multiplier applied to enemy hp/atk/def for day scaling and difficulty.
 */
void mg_battle_init(mg_battle_t *b, int enemy_index, float stat_multiplier);

/**
 * Advance one combat round.
 * Mutates both b (enemy HP, log, round counter) and p (player HP, qi, moves).
 * Sets b->battle_over + b->player_won when the fight ends.
 */
void mg_battle_advance(mg_battle_t *b, mg_player_t *p);
