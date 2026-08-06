/**
 * Aura 状态机实现
 *
 * 转换表:
 *   IDLE       + WAKE_BUTTON   → LISTENING
 *   IDLE       + WAKE_WORD     → LISTENING
 *   IDLE       + RESPONSE_TEXT → SPEAKING   (接住迟到回复)
 *   LISTENING  + VOICE_STOP    → PROCESSING
 *   LISTENING  + ABORT         → IDLE
 *   PROCESSING + RESPONSE_TEXT → SPEAKING
 *   PROCESSING + ABORT         → IDLE
 *   SPEAKING   + TTS_DONE      → IDLE
 *   SPEAKING   + TTS_DONE_CONTINUE → LISTENING
 *   SPEAKING   + ABORT         → IDLE
 *   SPEAKING   + WAKE_BUTTON   → LISTENING  (打断重听)
 */
#include "state_machine.h"
#include "esp_log.h"
#include <stddef.h>

static const char *TAG = "fsm";

static aura_fsm_state_t  s_state = AURA_STATE_IDLE;
static fsm_transition_cb_t s_cb  = NULL;

static const char *STATE_NAMES[] = {
    "IDLE", "LISTENING", "PROCESSING", "SPEAKING",
};

void fsm_init(fsm_transition_cb_t cb)
{
    s_state = AURA_STATE_IDLE;
    s_cb    = cb;
    ESP_LOGI(TAG, "FSM initialized → IDLE");
}

aura_fsm_state_t fsm_get_state(void)
{
    return s_state;
}

const char *fsm_state_name(aura_fsm_state_t state)
{
    if (state >= 0 && state <= AURA_STATE_SPEAKING)
        return STATE_NAMES[state];
    return "UNKNOWN";
}

bool fsm_handle_event(aura_fsm_event_t event)
{
    aura_fsm_state_t old = s_state;
    aura_fsm_state_t next = old;           /* default: no change */

    switch (old) {
    /* ── IDLE ─────────────────────────── */
    case AURA_STATE_IDLE:
        if (event == AURA_EVT_WAKE_BUTTON || event == AURA_EVT_WAKE_WORD)
            next = AURA_STATE_LISTENING;
        else if (event == AURA_EVT_RESPONSE_TEXT)
            next = AURA_STATE_SPEAKING;
        break;

    /* ── LISTENING ────────────────────── */
    case AURA_STATE_LISTENING:
        if (event == AURA_EVT_VOICE_STOP)
            next = AURA_STATE_PROCESSING;
        else if (event == AURA_EVT_ABORT)
            next = AURA_STATE_IDLE;
        break;

    /* ── PROCESSING ───────────────────── */
    case AURA_STATE_PROCESSING:
        if (event == AURA_EVT_RESPONSE_TEXT)
            next = AURA_STATE_SPEAKING;
        else if (event == AURA_EVT_ABORT)
            next = AURA_STATE_IDLE;
        break;

    /* ── SPEAKING ─────────────────────── */
    case AURA_STATE_SPEAKING:
        if (event == AURA_EVT_TTS_DONE)
            next = AURA_STATE_IDLE;
        else if (event == AURA_EVT_TTS_DONE_CONTINUE)
            next = AURA_STATE_LISTENING;
        else if (event == AURA_EVT_ABORT)
            next = AURA_STATE_IDLE;
        else if (event == AURA_EVT_WAKE_BUTTON || event == AURA_EVT_WAKE_WORD)
            next = AURA_STATE_LISTENING;   /* 打断重听 */
        break;
    }

    if (next == old) {
        ESP_LOGD(TAG, "Event %d ignored in %s", event, STATE_NAMES[old]);
        return false;
    }

    s_state = next;
    ESP_LOGI(TAG, "%s → %s (evt %d)", STATE_NAMES[old], STATE_NAMES[next], event);

    if (s_cb)
        s_cb(old, next);

    return true;
}
