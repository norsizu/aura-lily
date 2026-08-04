/**
 * Aura 状态机 — 语音交互状态管理
 *
 * States: IDLE → LISTENING → PROCESSING → SPEAKING → IDLE
 */
#pragma once

#include <stdbool.h>

typedef enum {
    AURA_STATE_IDLE,
    AURA_STATE_LISTENING,
    AURA_STATE_PROCESSING,
    AURA_STATE_SPEAKING,
} aura_fsm_state_t;

typedef enum {
    AURA_EVT_WAKE_BUTTON,    // 用户按下唤醒键
    AURA_EVT_WAKE_WORD,      // 唤醒词 "豆豆" 检测到
    AURA_EVT_VOICE_STOP,     // 用户释放按键 / 录音结束
    AURA_EVT_RESPONSE_TEXT,   // 后端返回 AI 文本
    AURA_EVT_TTS_DONE,       // TTS 播放完成 / 超时
    AURA_EVT_TTS_DONE_CONTINUE, // TTS 播完后继续听下一句
    AURA_EVT_ABORT,          // 中止 (错误、断线等)
} aura_fsm_event_t;

/**
 * 状态转换回调 (新状态进入时调用)
 * @param old_state  离开的状态
 * @param new_state  进入的状态
 */
typedef void (*fsm_transition_cb_t)(aura_fsm_state_t old_state,
                                     aura_fsm_state_t new_state);

/**
 * 初始化状态机，注册转换回调
 */
void fsm_init(fsm_transition_cb_t cb);

/**
 * 输入事件，驱动状态转换
 * @return true 如果发生了转换
 */
bool fsm_handle_event(aura_fsm_event_t event);

/**
 * 获取当前状态
 */
aura_fsm_state_t fsm_get_state(void);

/**
 * 状态名称 (调试用)
 */
const char *fsm_state_name(aura_fsm_state_t state);
