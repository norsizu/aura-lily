/**
 * minigame_data.c — 白日梦大冒险 素材池 v2.0
 * 12 剧情事件 + 4 战斗事件 + 4 敌人
 */
#include "minigame_data.h"

/* ─── 敌人池 ─────────────────────────────────────────────────────── */
const mg_enemy_t mg_enemies[MG_ENEMY_COUNT] = {
    /* 0 街区暴徒 — 入场热身 */
    { .name="街区暴徒",   .hp=80,  .atk=18, .def=6,
      .special_interval=5, .special_def_add=5,  .is_boss=false },
    /* 1 霓虹骑士 — 中期标准 */
    { .name="霓虹骑士",   .hp=120, .atk=25, .def=15,
      .special_interval=3, .special_def_add=10, .is_boss=false },
    /* 2 数字怪 — 后期激进，特技间隔短 */
    { .name="数字怪",     .hp=100, .atk=30, .def=10,
      .special_interval=2, .special_def_add=8,  .is_boss=false },
    /* 3 虚空守门人 — 最终 Boss */
    { .name="虚空守门人", .hp=160, .atk=28, .def=16,
      .special_interval=4, .special_def_add=8,  .is_boss=true  },
};

/* ─── 事件池 ─────────────────────────────────────────────────────── */
const mg_event_t mg_events[MG_EVENT_COUNT] = {

/* ══ STORY 01 深夜奶茶店 ══════════════════════════════════════════ */
{
    .id="EVT_STORY_01", .type=MG_EVENT_STORY, .title="深夜奶茶店",
    .is_final_boss=false,
    .lines={"深夜，路边奶茶店","亮着灯，店员向你","递出冒泡的星云","特调，招手微笑。"},
    .choices={
        { .tag="新奇旅途", .text="干了！做梦就放肆",
          .effects={{MG_EFFECT_MP_ADD,18,0},{MG_EFFECT_SHARD_ADD,5,0}} },
        { .tag="诸事如常", .text="谢谢，我不渴",
          .effects={{MG_EFFECT_EN_ADD,3,0},{MG_EFFECT_NONE}} },
    }, .enemy_index=-1,
},

/* ══ STORY 02 幽灵快递员 ══════════════════════════════════════════ */
{
    .id="EVT_STORY_02", .type=MG_EVENT_STORY, .title="幽灵快递员",
    .is_final_boss=false,
    .lines={"幽灵快递员拦住你，","递来一个包裹：","\"有你的件，签","收一下~\" 是啥？"},
    .choices={
        { .tag="幸运日", .text="签收！拆了再说",
          .effects={{MG_EFFECT_HP_ADD,25,0},{MG_EFFECT_SHARD_ADD,8,0}} },
        { .tag="乌云缠绕", .text="可疑，拒绝签收",
          .effects={{MG_EFFECT_EN_ADD,5,0},{MG_EFFECT_QI_ADD,15,0}} },
    }, .enemy_index=-1,
},

/* ══ STORY 03 便利店妖怪 ══════════════════════════════════════════ */
{
    .id="EVT_STORY_03", .type=MG_EVENT_STORY, .title="便利店妖怪",
    .is_final_boss=false,
    .lines={"收银台后站着只","小狸猫妖，认真","扫码说：","\"欢迎光临！\""},
    .choices={
        { .tag="诸事如常", .text="买水，顺便聊聊",
          .effects={{MG_EFFECT_HP_ADD,15,0},{MG_EFFECT_MP_ADD,10,0}} },
        { .tag="新奇旅途", .text="问：在梦里打工苦吗",
          .effects={{MG_EFFECT_MP_ADD,25,0},
                    {MG_EFFECT_MOVE_UNLOCK,0,MG_MOVE_DOUBLE_STRIKE}} },
    }, .enemy_index=-1,
},

/* ══ STORY 04 末班地铁 ════════════════════════════════════════════ */
{
    .id="EVT_STORY_04", .type=MG_EVENT_STORY, .title="末班地铁",
    .is_final_boss=false,
    .lines={"最后一班地铁进站，","车厢里坐满了","没有脸的乘客，","有个空位在等你。"},
    .choices={
        { .tag="梦境深潜", .text="坐下去，看哪里",
          .effects={{MG_EFFECT_HP_ADD,20,0},{MG_EFFECT_MP_ADD,-10,0}} },
        { .tag="谨慎是福", .text="站在车门边",
          .effects={{MG_EFFECT_EN_ADD,4,0},{MG_EFFECT_QI_ADD,10,0}} },
    }, .enemy_index=-1,
},

/* ══ STORY 05 午夜图书馆 ══════════════════════════════════════════ */
{
    .id="EVT_STORY_05", .type=MG_EVENT_STORY, .title="午夜图书馆",
    .is_final_boss=false,
    .lines={"24小时图书馆，","馆员是位老狐狸，","她指着一排","发光的禁书。"},
    .choices={
        { .tag="求知若渴", .text="借一本读读",
          .effects={{MG_EFFECT_MP_ADD,20,0},
                    {MG_EFFECT_MOVE_UNLOCK,0,MG_MOVE_QI_SURGE}} },
        { .tag="稳扎稳打", .text="问有没有实战手册",
          .effects={{MG_EFFECT_HP_ADD,10,0},{MG_EFFECT_EN_ADD,3,0}} },
    }, .enemy_index=-1,
},

/* ══ STORY 06 屋顶神鸦 ════════════════════════════════════════════ */
{
    .id="EVT_STORY_06", .type=MG_EVENT_STORY, .title="屋顶神鸦",
    .is_final_boss=false,
    .lines={"屋顶的乌鸦盯着你，","开口说：","\"我见过你白天，","又累又窝囊。\""},
    .choices={
        { .tag="鸡汤入魂", .text="但我还在努力",
          .effects={{MG_EFFECT_HPMAX_ADD,20,0},{MG_EFFECT_HP_ADD,10,0}} },
        { .tag="互怼见长", .text="你个鸟懂啥",
          .effects={{MG_EFFECT_EN_ADD,5,0},{MG_EFFECT_HP_ADD,-10,0}} },
    }, .enemy_index=-1,
},

/* ══ STORY 07 霓虹占卜师 ══════════════════════════════════════════ */
{
    .id="EVT_STORY_07", .type=MG_EVENT_STORY, .title="霓虹占卜师",
    .is_final_boss=false,
    .lines={"小巷占卜摊，","猫灵摆出塔罗，","她说：\"命运的","牌面已定，看吗？\""},
    .choices={
        { .tag="命运一击", .text="算战斗运",
          .effects={{MG_EFFECT_QI_ADD,30,0},{MG_EFFECT_HP_ADD,-5,0}} },
        { .tag="财运高照", .text="算前程",
          .effects={{MG_EFFECT_SHARD_ADD,15,0},{MG_EFFECT_NONE}} },
    }, .enemy_index=-1,
},

/* ══ STORY 08 梦境咖啡师 ══════════════════════════════════════════ */
{
    .id="EVT_STORY_08", .type=MG_EVENT_STORY, .title="梦境咖啡师",
    .is_final_boss=false,
    .lines={"只营业三分钟的","咖啡馆，咖啡师是","一片会说话的云，","正在调\"回忆拿铁\"。"},
    .choices={
        { .tag="云端放飞", .text="来杯你推荐的",
          .effects={{MG_EFFECT_HP_ADD,40,0},{MG_EFFECT_MP_ADD,-20,0}} },
        { .tag="现实主义", .text="普通的一杯就好",
          .effects={{MG_EFFECT_HP_ADD,15,0},{MG_EFFECT_MP_ADD,10,0}} },
    }, .enemy_index=-1,
},

/* ══ STORY 09 迷路的星星 ══════════════════════════════════════════ */
{
    .id="EVT_STORY_09", .type=MG_EVENT_STORY, .title="迷路的星星",
    .is_final_boss=false,
    .lines={"一颗小星星蹲在","路边哭泣，它说","找不到回家","的路了。"},
    .choices={
        { .tag="好心有好报", .text="陪它找找看",
          .effects={{MG_EFFECT_SHARD_ADD,20,0},{MG_EFFECT_EN_ADD,-2,0}} },
        { .tag="许愿时刻", .text="帮它许个愿",
          .effects={{MG_EFFECT_HPMAX_ADD,15,0},{MG_EFFECT_QI_ADD,20,0}} },
    }, .enemy_index=-1,
},

/* ══ STORY 10 雨中猫灵 ════════════════════════════════════════════ */
{
    .id="EVT_STORY_10", .type=MG_EVENT_STORY, .title="雨中猫灵",
    .is_final_boss=false,
    .lines={"大雨中雪白的猫","拦住你，脖子上","挂着一块","梦境碎片。"},
    .choices={
        { .tag="热心市民", .text="帮它找主人",
          .effects={{MG_EFFECT_SHARD_ADD,12,0},
                    {MG_EFFECT_MOVE_UNLOCK,0,MG_MOVE_COUNTER}} },
        { .tag="温柔避雨", .text="送它一把伞",
          .effects={{MG_EFFECT_HPMAX_ADD,20,0},{MG_EFFECT_HP_ADD,10,0}} },
    }, .enemy_index=-1,
},

/* ══ STORY 11 地下赌局 ════════════════════════════════════════════ */
{
    .id="EVT_STORY_11", .type=MG_EVENT_STORY, .title="地下赌局",
    .is_final_boss=false,
    .lines={"地下停车场，一群","梦境生物在玩牌。","鬼头庄家招手：","\"来一局不？\""},
    .choices={
        { .tag="赌徒本能", .text="All in！",
          .effects={{MG_EFFECT_SHARD_ADD,30,0},{MG_EFFECT_HP_ADD,-20,0}} },
        { .tag="冷眼旁观", .text="看破套路再走",
          .effects={{MG_EFFECT_QI_ADD,20,0},
                    {MG_EFFECT_MOVE_UNLOCK,0,MG_MOVE_FIRST_BURST}} },
    }, .enemy_index=-1,
},

/* ══ STORY 12 梦境商店 ════════════════════════════════════════════ */
{
    .id="EVT_STORY_12", .type=MG_EVENT_STORY, .title="梦境商店",
    .is_final_boss=false,
    .lines={"不知哪来的移动","推车，贩卖","\"梦中珍品\"，","看起来有点东西。"},
    .choices={
        { .tag="实用主义", .text="战斗补给包",
          .effects={{MG_EFFECT_HP_ADD,25,0},{MG_EFFECT_EN_ADD,-2,0}} },
        { .tag="赌神附体", .text="神秘道具",
          .effects={{MG_EFFECT_MOVE_UNLOCK,0,MG_MOVE_DOUBLE_STRIKE},
                    {MG_EFFECT_HP_ADD,-5,0}} },
    }, .enemy_index=-1,
},

/* ══ COMBAT 01 街区暴徒 ══════════════════════════════════════════ */
{
    .id="EVT_COMBAT_01", .type=MG_EVENT_COMBAT, .title="街区暴徒",
    .is_final_boss=false,
    .lines={"穿发光夹克的地痞","堵在路中间，","\"这片是我地盘，","买路钱！\""},
    .choices={}, .enemy_index=0,
},

/* ══ COMBAT 02 霓虹骑士 ══════════════════════════════════════════ */
{
    .id="EVT_COMBAT_02", .type=MG_EVENT_COMBAT, .title="霓虹骑士",
    .is_final_boss=false,
    .lines={"霓虹骑士骑着","发光摩托拦路。","\"我是街区守卫，","你得先过我这关！\""},
    .choices={}, .enemy_index=1,
},

/* ══ COMBAT 03 数字怪 ════════════════════════════════════════════ */
{
    .id="EVT_COMBAT_03", .type=MG_EVENT_COMBAT, .title="数字怪",
    .is_final_boss=false,
    .lines={"广告牌里钻出来","的像素怪物，","像素点重组成","威胁的形态。"},
    .choices={}, .enemy_index=2,
},

/* ══ COMBAT 04 虚空守门人（Boss）════════════════════════════════ */
{
    .id="EVT_COMBAT_04", .type=MG_EVENT_COMBAT, .title="虚空守门人",
    .is_final_boss=true,
    .lines={"梦境尽头，巨人","穿公务员制服挡路：","\"最终关卡，","盖章才能离开。\""},
    .choices={}, .enemy_index=3,
},

}; /* end mg_events[] */
