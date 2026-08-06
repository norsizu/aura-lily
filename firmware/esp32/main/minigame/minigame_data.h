/**
 * minigame_data.h — Hardcoded event / enemy data types for 白日梦大冒险
 * Vertical Slice event pool: 3 story events + 1 combat encounter.
 */
#pragma once
#include <stdbool.h>
#include <stdint.h>

/* ── Move IDs ──────────────────────────────────────────────────── */
typedef enum {
    MG_MOVE_DOUBLE_STRIKE = 0,  /* extra attack hit at 50% dmg */
    MG_MOVE_COUNTER       = 1,  /* reflect 30% of incoming dmg  */
    MG_MOVE_FIRST_BURST   = 2,  /* round 1: bonus +20 damage    */
    MG_MOVE_QI_SURGE      = 3,  /* qi gain +30%                 */
    MG_MOVE_COUNT         = 4,
} mg_move_t;

/* ── Effect types ───────────────────────────────────────────────── */
typedef enum {
    MG_EFFECT_NONE         = 0,
    MG_EFFECT_HP_ADD,
    MG_EFFECT_MP_ADD,
    MG_EFFECT_EN_ADD,
    MG_EFFECT_QI_ADD,
    MG_EFFECT_SHARD_ADD,
    MG_EFFECT_MOVE_UNLOCK,
    MG_EFFECT_HPMAX_ADD,   /* raise hp_max; current hp scales proportionally */
} mg_effect_type_t;

/* ── Combat-win upgrade choices ─────────────────────────────────── */
typedef enum {
    MG_UPGRADE_ATK_PLUS   = 0,  /* 强化拳法: ATK +5            */
    MG_UPGRADE_HPMAX_PLUS = 1,  /* 体魄强化: hp_max +30        */
    MG_UPGRADE_HEAL_FULL  = 2,  /* 梦境续命: hp = hp_max       */
    MG_UPGRADE_PERIODIC   = 3,  /* 连击本能: extra hit /3 rds  */
    MG_UPGRADE_DEF_PLUS   = 4,  /* 铠甲强化: DEF +3            */
    MG_UPGRADE_COUNT      = 5,
} mg_upgrade_t;

typedef struct {
    mg_effect_type_t type;
    int              value;
    int              move_id;   /* used only when type == MG_EFFECT_MOVE_UNLOCK */
} mg_effect_t;

typedef struct {
    const char  *tag;           /* flavour tag shown in choice box, e.g. "新奇旅途" */
    const char  *text;          /* choice description text */
    mg_effect_t  effects[2];    /* up to 2 stat effects */
} mg_choice_t;

/* ── Event types ────────────────────────────────────────────────── */
typedef enum {
    MG_EVENT_STORY  = 0,
    MG_EVENT_COMBAT = 1,
} mg_event_type_t;

typedef struct {
    const char       *id;
    mg_event_type_t   type;
    const char       *title;
    const char       *lines[4];   /* narrative lines (NULL = unused) */
    mg_choice_t       choices[2]; /* for STORY only */
    int               enemy_index;/* for COMBAT only */
    bool              is_final_boss; /* true = always triggered on last day */
} mg_event_t;

/* ── Enemy ──────────────────────────────────────────────────────── */
typedef struct {
    const char *name;
    int         hp, atk, def;
    int         special_interval; /* every N rounds: def += special_def_add */
    int         special_def_add;
    bool        is_boss;
} mg_enemy_t;

/* ── Data tables ────────────────────────────────────────────────── */
#define MG_EVENT_COUNT  16   /* 12 story + 4 combat */
#define MG_ENEMY_COUNT  4

extern const mg_event_t  mg_events[MG_EVENT_COUNT];
extern const mg_enemy_t  mg_enemies[MG_ENEMY_COUNT];
