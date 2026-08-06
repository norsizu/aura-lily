/**
 * minigame_battle.c — Auto-battle round resolution
 *
 * Round sequence:
 *   1. Player attacks enemy  (may double-strike, may first-burst)
 *   2. Enemy special ability (every special_interval rounds: DEF +N)
 *   3. Enemy attacks player  (player may counter-reflect)
 *   4. Qi accumulates from both attack and taking-hit
 *   5. Ultimate fires if qi >= 100  → qi resets to 0
 *   6. Check win/lose
 */
#include "minigame_battle.h"
#include "esp_log.h"
#include <string.h>
#include <stdio.h>

static const char *TAG = "mg_battle";

static void battle_log(mg_battle_t *b, const char *msg)
{
    int idx = b->log_head % MG_BATTLE_LOG_LINES;
    strncpy(b->log[idx], msg, MG_BATTLE_LOG_LEN - 1);
    b->log[idx][MG_BATTLE_LOG_LEN - 1] = '\0';
    b->log_head++;
    ESP_LOGD(TAG, "log: %s", msg);
}

void mg_battle_init(mg_battle_t *b, int enemy_index, float stat_multiplier)
{
    memset(b, 0, sizeof(*b));
    b->enemy    = mg_enemies[enemy_index];
    if (stat_multiplier < 0.5f) stat_multiplier = 0.5f;
    b->enemy.hp  = (int)(b->enemy.hp  * stat_multiplier + 0.5f);
    b->enemy.atk = (int)(b->enemy.atk * stat_multiplier + 0.5f);
    b->enemy.def = (int)(b->enemy.def * (0.85f + (stat_multiplier - 1.0f) * 0.45f) + 0.5f);
    if (b->enemy.def < 1) b->enemy.def = 1;
    b->enemy_hp  = b->enemy.hp;
    ESP_LOGI(TAG, "Battle: %s HP=%d ATK=%d DEF=%d",
             b->enemy.name, b->enemy_hp, b->enemy.atk, b->enemy.def);
}

void mg_battle_advance(mg_battle_t *b, mg_player_t *p)
{
    char buf[MG_BATTLE_LOG_LEN];
    b->round++;

    /* ── 1. Player attacks ──────────────────────────────────── */
    int dmg = p->atk - b->enemy.def;
    if (dmg < 1) dmg = 1;

    if (b->round == 1 && p->moves[MG_MOVE_FIRST_BURST]) {
        dmg += 20;
        battle_log(b, "首击爆发！+20");
    }

    int extra = 0;
    if (p->moves[MG_MOVE_DOUBLE_STRIKE]) {
        extra = dmg / 2;
        if (extra < 1) extra = 1;
    }

    b->enemy_hp -= (dmg + extra);
    if (extra > 0) {
        snprintf(buf, sizeof(buf), "豆豆连击 -%d(+%d)", dmg, extra);
    } else {
        snprintf(buf, sizeof(buf), "豆豆攻击 -%d", dmg);
    }
    battle_log(b, buf);

    /* Qi gain from attacking */
    int qi_atk = p->moves[MG_MOVE_QI_SURGE] ? 18 * 13 / 10 : 18;
    p->qi += qi_atk;
    if (p->qi > 100) p->qi = 100;

    if (b->enemy_hp <= 0) {
        b->enemy_hp   = 0;
        p->shard += 5;
        b->player_won = true;
        b->battle_over = true;
        battle_log(b, "豆豆获胜！");
        return;
    }

    /* Periodic bonus strike (every N rounds) */
    if (p->periodic_strike_interval > 0 &&
        b->round % p->periodic_strike_interval == 0) {
        int bonus = p->atk / 2;
        if (bonus < 1) bonus = 1;
        b->enemy_hp -= bonus;
        snprintf(buf, sizeof(buf), "本能连击 -%d", bonus);
        battle_log(b, buf);
        if (b->enemy_hp <= 0) {
            b->enemy_hp    = 0;
            p->shard      += 5;
            b->player_won  = true;
            b->battle_over = true;
            battle_log(b, "豆豆获胜！");
            return;
        }
    }

    /* ── 2. Enemy special ───────────────────────────────────── */
    if (b->enemy.special_interval > 0 &&
        b->round % b->enemy.special_interval == 0) {
        b->enemy.def += b->enemy.special_def_add;
        snprintf(buf, sizeof(buf), "%s防御+%d", b->enemy.name, b->enemy.special_def_add);
        battle_log(b, buf);
    }

    /* ── 3. Enemy attacks ───────────────────────────────────── */
    int e_dmg = b->enemy.atk - p->def;
    if (e_dmg < 1) e_dmg = 1;
    p->hp -= e_dmg;
    snprintf(buf, sizeof(buf), "%s攻击 -%d", b->enemy.name, e_dmg);
    battle_log(b, buf);

    /* Counter: reflect 30 % back */
    if (p->moves[MG_MOVE_COUNTER]) {
        int reflect = e_dmg * 3 / 10;
        if (reflect < 1) reflect = 1;
        b->enemy_hp -= reflect;
        snprintf(buf, sizeof(buf), "豆豆反弹 -%d", reflect);
        battle_log(b, buf);
        if (b->enemy_hp <= 0) {
            b->enemy_hp    = 0;
            p->hp += e_dmg;                    /* negate the killing blow */
            if (p->hp > p->hp_max) p->hp = p->hp_max;
            p->shard += 5;
            b->player_won  = true;
            b->battle_over = true;
            battle_log(b, "豆豆获胜！");
            return;
        }
    }

    /* Qi gain from taking a hit */
    int qi_hit = p->moves[MG_MOVE_QI_SURGE] ? 22 * 13 / 10 : 22;
    p->qi += qi_hit;

    /* ── 4. Check ultimate ──────────────────────────────────── */
    if (p->qi >= 100) {
        p->qi = 0;
        int ult = p->atk * 32 / 10 + 80 - b->enemy.def;
        if (ult < 80) ult = 80;
        b->enemy_hp -= ult;
        snprintf(buf, sizeof(buf), "白日梦大招！-%d", ult);
        battle_log(b, buf);
        if (b->enemy_hp <= 0) {
            b->enemy_hp    = 0;
            if (p->hp <= 0) p->hp = 1;          /* dramatic last-stand finish */
            p->shard += 5;
            b->player_won  = true;
            b->battle_over = true;
            battle_log(b, "豆豆获胜！");
            return;
        }
    }

    /* ── 5. Check player dead ───────────────────────────────── */
    if (p->hp <= 0) {
        p->hp          = 0;
        b->player_won  = false;
        b->battle_over = true;
        battle_log(b, "豆豆倒下了...");
    }
}
