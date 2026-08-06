/**
 * minigame_state.c — 白日梦大冒险 finite-state machine
 *
 * Screen flow:
 *   DIFFICULTY → roll daily event → STORY_EVENT / ENCOUNTER → ...
 *   day_current reaches days_total → WIN_GAME
 */
#include "minigame_state.h"
#include "minigame_battle.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include <string.h>

static const char *TAG = "mg_state";

static bool           s_active = false;
static mg_run_state_t s_run    = {0};

/* cached so helper functions can use it without passing around */
static int64_t s_now_ms = 0;

static int clamp_add(int *target, int value, int max_value)
{
    int before = *target;
    *target += value;
    if (*target > max_value) *target = max_value;
    if (*target < 0) *target = 0;
    return *target - before;
}

static void clear_choice_result(void)
{
    s_run.last_choice         = -1;
    s_run.last_hp_delta       = 0;
    s_run.last_hpmax_delta    = 0;
    s_run.last_mp_delta       = 0;
    s_run.last_en_delta       = 0;
    s_run.last_qi_delta       = 0;
    s_run.last_shard_delta    = 0;
    s_run.last_unlocked_move  = -1;
}

static void apply_choice_with_result(const mg_choice_t *ch)
{
    mg_player_t *p = &s_run.player;

    for (int i = 0; i < 2; i++) {
        const mg_effect_t *e = &ch->effects[i];
        switch (e->type) {
        case MG_EFFECT_HP_ADD:
            s_run.last_hp_delta += clamp_add(&p->hp, e->value, p->hp_max);
            break;
        case MG_EFFECT_MP_ADD:
            s_run.last_mp_delta += clamp_add(&p->mp, e->value, p->mp_max);
            break;
        case MG_EFFECT_EN_ADD:
            s_run.last_en_delta += clamp_add(&p->en, e->value, p->en_max);
            break;
        case MG_EFFECT_QI_ADD:
            s_run.last_qi_delta += clamp_add(&p->qi, e->value, 100);
            break;
        case MG_EFFECT_SHARD_ADD:
            p->shard += e->value;
            s_run.last_shard_delta += e->value;
            break;
        case MG_EFFECT_HPMAX_ADD: {
            int old_max = p->hp_max;
            int old_hp  = p->hp;
            p->hp_max += e->value;
            if (old_max > 0 && p->hp_max > old_max) {
                /* scale current HP proportionally */
                p->hp = (int)((long)old_hp * p->hp_max / old_max);
                if (p->hp > p->hp_max) p->hp = p->hp_max;
            }
            s_run.last_hpmax_delta += e->value;
            s_run.last_hp_delta    += (p->hp - old_hp);
            break;
        }
        case MG_EFFECT_MOVE_UNLOCK:
            if (e->move_id >= 0 && e->move_id < MG_MOVE_COUNT && !p->moves[e->move_id]) {
                p->moves[e->move_id] = true;
                s_run.last_unlocked_move = e->move_id;
                ESP_LOGI(TAG, "Move unlocked: %d", e->move_id);
            }
            break;
        default:
            break;
        }
    }
}

/* ── Player init ─────────────────────────────────────────────────── */
static void init_player(mg_difficulty_t diff)
{
    mg_player_t *p = &s_run.player;
    memset(p, 0, sizeof(*p));
    int d = (int)diff;
    p->hp_max = 200 - d * 20;  /* 200 / 180 / 160 */
    p->hp     = p->hp_max;
    p->mp_max = 60 - d * 10;   /* 60 / 50 / 40  */
    p->mp     = p->mp_max;
    p->en_max = 16 - d * 2;    /* 16 / 14 / 12  */
    p->en     = p->en_max;
    p->atk    = 30 + d * 2;    /* 30 / 32 / 34  */
    p->def    = 10 - d;        /* 10 / 9 / 8    */
}

/* ── Screen transition ───────────────────────────────────────────── */
static void enter_screen(mg_screen_t sc)
{
    s_run.screen          = sc;
    s_run.screen_enter_ms = s_now_ms;
    s_run.sel             = 0;
    ESP_LOGI(TAG, "Enter screen %d", (int)sc);
}

static void show_event(int idx)
{
    s_run.event_index = idx;
    const mg_event_t *ev = &mg_events[idx];
    if (ev->type == MG_EVENT_STORY) {
        enter_screen(MG_SCREEN_STORY_EVENT);
    } else {
        enter_screen(MG_SCREEN_ENCOUNTER);
    }
}

static bool is_final_boss_day(void)
{
    return s_run.days_total > 0 && s_run.day_current >= s_run.days_total;
}

static int select_non_repeating(const int *pool, int pool_count)
{
    if (pool_count <= 0) return 0;

    int idx = pool[esp_random() % pool_count];
    if (pool_count > 1 && idx == s_run.event_index) {
        int start = (int)(esp_random() % pool_count);
        for (int i = 0; i < pool_count; i++) {
            int candidate = pool[(start + i) % pool_count];
            if (candidate != s_run.event_index) {
                idx = candidate;
                break;
            }
        }
    }
    return idx;
}

static int roll_event_index(void)
{
    /* 最终天：强制选 is_final_boss 事件 */
    if (is_final_boss_day()) {
        for (int i = 0; i < MG_EVENT_COUNT; i++) {
            if (mg_events[i].is_final_boss) return i;
        }
        /* fallback: 继续走常规战斗 pool */
    }

    int story_count = 0;
    int combat_count = 0;
    int story_indices[MG_EVENT_COUNT];
    int combat_indices[MG_EVENT_COUNT];
    int progress = (s_run.days_total > 0)
        ? (s_run.day_current * 100) / s_run.days_total
        : 0;

    for (int i = 0; i < MG_EVENT_COUNT; i++) {
        if (mg_events[i].type == MG_EVENT_STORY) {
            story_indices[story_count++] = i;
        } else if (mg_events[i].type == MG_EVENT_COMBAT && !mg_events[i].is_final_boss) {
            int enemy = mg_events[i].enemy_index;
            if (enemy == 0 ||
                (enemy == 1 && progress >= 35) ||
                (enemy == 2 && progress >= 55)) {
                combat_indices[combat_count++] = i;
            }
        }
    }

    if (story_count == 0 && combat_count == 0) {
        return 0;
    }

    /*
     * Roguelike-lite: daily event is rolled, not walked linearly.
     * - First three days are story-only, so the run can build resources.
     * - Final day is handled above and always becomes the boss.
     * - Early days are mostly story, later days see more encounters.
     * - Avoid repeating the exact same event when the pool allows it.
     */
    bool prefer_combat = false;
    if (combat_count > 0 && s_run.day_current > 3) {
        uint32_t roll = esp_random() % 100;
        /*
         * 8-day demo balance:
         *   - first three days are still story-only
         *   - target one normal fight before the final boss
         *   - extra fights remain possible, but are no longer forced
         *
         * Longer chapters keep the more combat-heavy tuning.
         */
        int target_wins = s_run.days_total <= 8 ? 1 : (s_run.days_total <= 20 ? 5 : 8);
        int combat_chance = (s_run.days_total <= 8)
            ? (8 + (progress * 22) / 100)
            : (12 + (progress * 35) / 100);
        int days_left_before_boss = s_run.days_total - s_run.day_current;
        int wins_needed = target_wins - s_run.combat_wins;
        bool must_combat = (wins_needed > 0 && wins_needed >= days_left_before_boss);

        int chance_cap = (s_run.days_total <= 8) ? 32 : 48;
        if (combat_chance > chance_cap) combat_chance = chance_cap;
        if (s_run.days_total <= 8) {
            if (s_run.story_streak >= 4) combat_chance += 12;
        } else if (s_run.story_streak >= 3) {
            combat_chance += 18;
        }
        if (s_run.combat_streak >= 2) combat_chance = 0;
        if (must_combat) {
            combat_chance = 100;
        }
        int hard_cap = (s_run.days_total <= 8) ? 42 : 75;
        if (!must_combat && combat_chance > hard_cap) combat_chance = hard_cap;
        prefer_combat = (roll < (uint32_t)combat_chance);
    }

    const int *pool = prefer_combat ? combat_indices : story_indices;
    int pool_count = prefer_combat ? combat_count : story_count;
    if (pool_count == 0) {
        pool = prefer_combat ? story_indices : combat_indices;
        pool_count = prefer_combat ? story_count : combat_count;
    }
    if (pool_count <= 0) return 0;

    return select_non_repeating(pool, pool_count);
}

static void start_next_day_event(void)
{
    int idx = roll_event_index();
    show_event(idx);
    if (mg_events[idx].type == MG_EVENT_COMBAT) {
        s_run.combat_streak++;
        s_run.story_streak = 0;
    } else {
        s_run.story_streak++;
        s_run.combat_streak = 0;
    }
}

static void advance_day(void)
{
    if (s_run.day_current >= s_run.days_total) {
        enter_screen(MG_SCREEN_WIN_GAME);
        return;
    }
    s_run.day_current++;
    start_next_day_event();
}

static void generate_upgrade_choices(void)
{
    /* Build pool of valid upgrades; skip PERIODIC if already active */
    int pool[MG_UPGRADE_COUNT];
    int pool_sz = 0;
    for (int i = 0; i < MG_UPGRADE_COUNT; i++) {
        if (i == MG_UPGRADE_PERIODIC && s_run.player.periodic_strike_interval > 0)
            continue;
        pool[pool_sz++] = i;
    }
    /* Fisher-Yates shuffle, pick first 3 */
    for (int i = 0; i < pool_sz - 1; i++) {
        int j = i + (int)(esp_random() % (pool_sz - i));
        int tmp = pool[i]; pool[i] = pool[j]; pool[j] = tmp;
    }
    for (int i = 0; i < 3; i++)
        s_run.upgrade_choices[i] = (i < pool_sz) ? (mg_upgrade_t)pool[i]
                                                  : MG_UPGRADE_ATK_PLUS;
}

static void apply_upgrade(mg_upgrade_t upg)
{
    mg_player_t *p = &s_run.player;
    p->level++;
    switch (upg) {
    case MG_UPGRADE_ATK_PLUS:
        p->atk += 5;
        ESP_LOGI(TAG, "Upgrade: ATK -> %d", p->atk);
        break;
    case MG_UPGRADE_HPMAX_PLUS: {
        int old_max = p->hp_max;
        p->hp_max  += 30;
        if (old_max > 0) {
            p->hp = (int)((long)p->hp * p->hp_max / old_max);
            if (p->hp > p->hp_max) p->hp = p->hp_max;
        }
        ESP_LOGI(TAG, "Upgrade: hp_max -> %d  hp -> %d", p->hp_max, p->hp);
        break;
    }
    case MG_UPGRADE_HEAL_FULL:
        p->hp = p->hp_max;
        ESP_LOGI(TAG, "Upgrade: full heal (%d)", p->hp);
        break;
    case MG_UPGRADE_PERIODIC:
        p->periodic_strike_interval = 3;
        ESP_LOGI(TAG, "Upgrade: periodic strike every 3 rounds");
        break;
    case MG_UPGRADE_DEF_PLUS:
        p->def += 3;
        ESP_LOGI(TAG, "Upgrade: DEF -> %d", p->def);
        break;
    default:
        break;
    }
}

/* ── Public API ──────────────────────────────────────────────────── */

void mg_init(void)
{
    s_active = false;
    memset(&s_run, 0, sizeof(s_run));
}

void mg_activate(void)
{
    s_active = true;
    memset(&s_run, 0, sizeof(s_run));
    s_now_ms  = esp_timer_get_time() / 1000;
    s_run.sel = (int)MG_DIFF_EASY;
    clear_choice_result();
    enter_screen(MG_SCREEN_DIFFICULTY);
    ESP_LOGI(TAG, "Minigame activated");
}

void mg_deactivate(void)
{
    s_active        = false;
    s_run.screen    = MG_SCREEN_NONE;
    ESP_LOGI(TAG, "Minigame deactivated");
}

bool mg_is_active(void)
{
    return s_active;
}

const mg_run_state_t *mg_get_run(void)
{
    return &s_run;
}

void mg_handle_input(mg_input_t input)
{
    if (!s_active) return;

    bool next_input = (input == MG_INPUT_LEFT);
    bool prev_input = (input == MG_INPUT_RIGHT);

    switch (s_run.screen) {

    case MG_SCREEN_DIFFICULTY:
        if (next_input) {
            s_run.sel = (s_run.sel + 1) % 3;
        } else if (prev_input) {
            s_run.sel = (s_run.sel + 2) % 3;
        } else if (input == MG_INPUT_CONFIRM) {
            s_run.difficulty  = (mg_difficulty_t)s_run.sel;
            static const int days[] = {8, 20, 30};
            s_run.days_total  = days[s_run.sel];
            s_run.day_current = 1;
            s_run.event_index  = -1;
            init_player(s_run.difficulty);
            start_next_day_event();
        } else if (input == MG_INPUT_BACK) {
            mg_deactivate();
        }
        break;

    case MG_SCREEN_STORY_EVENT:
        if (next_input || prev_input) {
            s_run.sel = (s_run.sel + 1) % 2;
        } else if (input == MG_INPUT_CONFIRM) {
            const mg_event_t *ev = &mg_events[s_run.event_index];
            clear_choice_result();
            s_run.last_choice = s_run.sel;
            apply_choice_with_result(&ev->choices[s_run.sel]);
            enter_screen(MG_SCREEN_CHOICE_RESULT);
        }
        break;

    case MG_SCREEN_CHOICE_RESULT:
        if (input == MG_INPUT_CONFIRM || next_input) {
            advance_day();
        }
        break;

    case MG_SCREEN_WIN_DAY:
        if (input == MG_INPUT_CONFIRM || next_input) {
            advance_day();
        }
        break;

    case MG_SCREEN_WIN_GAME:
    case MG_SCREEN_GAME_OVER:
        if (input == MG_INPUT_CONFIRM) {
            mg_deactivate();
        }
        break;

    case MG_SCREEN_BATTLE:
        if (s_run.battle.battle_over && input == MG_INPUT_CONFIRM) {
            if (s_run.battle.player_won) {
                const mg_event_t *ev = &mg_events[s_run.event_index];
                s_run.combat_wins++;
                if (ev->is_final_boss || is_final_boss_day()) {
                    enter_screen(MG_SCREEN_WIN_GAME);
                } else {
                    generate_upgrade_choices();
                    enter_screen(MG_SCREEN_LEVEL_UP);
                }
            } else {
                enter_screen(MG_SCREEN_GAME_OVER);
            }
        }
        break;

    case MG_SCREEN_LEVEL_UP:
        if (next_input) {
            s_run.sel = (s_run.sel + 1) % 3;
        } else if (prev_input) {
            s_run.sel = (s_run.sel + 2) % 3;
        } else if (input == MG_INPUT_CONFIRM) {
            apply_upgrade(s_run.upgrade_choices[s_run.sel]);
            advance_day();
        }
        break;

    case MG_SCREEN_ENCOUNTER:
        /* no player input — auto-advances via mg_tick() */
        break;

    default:
        break;
    }
}

void mg_tick(int64_t now_ms)
{
    if (!s_active) return;
    s_now_ms = now_ms;

    /* ENCOUNTER → BATTLE after a short dramatic pause */
    if (s_run.screen == MG_SCREEN_ENCOUNTER) {
        if (now_ms - s_run.screen_enter_ms >= 2200) {
            const mg_event_t *ev = &mg_events[s_run.event_index];
            float diff_mult = 1.0f + (int)s_run.difficulty * 0.2f;
            float day_progress = (s_run.days_total > 1)
                ? (float)(s_run.day_current - 1) / (float)(s_run.days_total - 1)
                : 1.0f;
            float day_mult = 0.78f + day_progress * 0.52f;
            if (is_final_boss_day()) {
                day_mult += (s_run.days_total <= 8) ? 0.40f : 0.18f;
            }
            mg_battle_init(&s_run.battle, ev->enemy_index, diff_mult * day_mult);
            s_run.battle.last_round_ms = now_ms;
            enter_screen(MG_SCREEN_BATTLE);
        }
        return;
    }

    /* BATTLE: advance slowly enough to read; result waits for KEY confirm. */
    if (s_run.screen == MG_SCREEN_BATTLE) {
        if (s_run.battle.battle_over) {
            return;
        }
        if (now_ms - s_run.battle.last_round_ms >= 1800) {
            s_run.battle.last_round_ms = now_ms;
            mg_battle_advance(&s_run.battle, &s_run.player);
        }
    }
}
