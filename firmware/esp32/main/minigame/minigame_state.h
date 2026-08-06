/**
 * minigame_state.h — 白日梦大冒险 run-state types and public FSM API
 */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "minigame_data.h"

/* ── Screen IDs ─────────────────────────────────────────────────── */
typedef enum {
    MG_SCREEN_NONE        = 0,
    MG_SCREEN_DIFFICULTY,
    MG_SCREEN_STORY_EVENT,
    MG_SCREEN_CHOICE_RESULT,
    MG_SCREEN_ENCOUNTER,
    MG_SCREEN_BATTLE,
    MG_SCREEN_LEVEL_UP,         /* pick upgrade after combat win */
    MG_SCREEN_WIN_DAY,
    MG_SCREEN_WIN_GAME,
    MG_SCREEN_GAME_OVER,
} mg_screen_t;

/* ── Input tokens ────────────────────────────────────────────────── */
typedef enum {
    MG_INPUT_NONE    = 0,
    MG_INPUT_LEFT,              /* BOOT short → next option      */
    MG_INPUT_RIGHT,             /* BOOT long  → previous option  */
    MG_INPUT_CONFIRM,           /* KEY short  → confirm         */
    MG_INPUT_BACK,              /* KEY long   → back / cancel   */
} mg_input_t;

/* ── Difficulty ──────────────────────────────────────────────────── */
typedef enum {
    MG_DIFF_EASY   = 0,  /* 8 days  */
    MG_DIFF_NORMAL = 1,  /* 20 days */
    MG_DIFF_HARD   = 2,  /* 30 days */
} mg_difficulty_t;

/* ── Player stats ────────────────────────────────────────────────── */
typedef struct mg_player_t {
    int  hp,  hp_max;
    int  mp,  mp_max;
    int  en,  en_max;
    int  atk, def;
    int  qi;            /* 0-100 ultimate meter       */
    int  shard;         /* accumulated dream shards   */
    int  level;         /* combat wins (starts at 0)  */
    int  periodic_strike_interval; /* 0 = off; 3 = every 3 rounds bonus hit */
    bool moves[MG_MOVE_COUNT];
} mg_player_t;

/* ── Battle log ──────────────────────────────────────────────────── */
#define MG_BATTLE_LOG_LINES 3
#define MG_BATTLE_LOG_LEN   48

/* ── Battle state ────────────────────────────────────────────────── */
typedef struct {
    mg_enemy_t  enemy;          /* live copy — def grows via specials */
    int         enemy_hp;
    int         round;
    char        log[MG_BATTLE_LOG_LINES][MG_BATTLE_LOG_LEN];
    int         log_head;       /* ring-buffer write index */
    bool        player_won;
    bool        battle_over;
    int64_t     last_round_ms;
} mg_battle_t;

/* ── Run state ───────────────────────────────────────────────────── */
typedef struct {
    mg_difficulty_t difficulty;
    int             days_total;
    int             day_current;
    int             event_index;
    mg_player_t     player;
    mg_battle_t     battle;
    mg_screen_t     screen;
    int             sel;
    int             combat_wins;
    int             story_streak;
    int             combat_streak;
    int             last_choice;
    int             last_hp_delta;
    int             last_hpmax_delta;   /* hp_max increase from HPMAX_ADD */
    int             last_mp_delta;
    int             last_en_delta;
    int             last_qi_delta;
    int             last_shard_delta;
    int             last_unlocked_move;
    mg_upgrade_t    upgrade_choices[3]; /* shown in LEVEL_UP screen */
    int64_t         screen_enter_ms;
} mg_run_state_t;

/* ── Public API ──────────────────────────────────────────────────── */
void  mg_init(void);
void  mg_activate(void);            /* show difficulty screen */
void  mg_deactivate(void);          /* exit back to normal UI */
bool  mg_is_active(void);
void  mg_handle_input(mg_input_t input);
void  mg_tick(int64_t now_ms);      /* call from input_task ~50 ms cadence */
const mg_run_state_t *mg_get_run(void);
