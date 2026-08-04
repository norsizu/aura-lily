#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

typedef struct {
    bool vad_speech;
    int fed_chunks;
    int fetched_chunks;
} audio_frontend_diag_t;

esp_err_t audio_frontend_init(void);
void audio_frontend_reset(void);
size_t audio_frontend_process(const int16_t *input, size_t input_samples,
                              int16_t *output, size_t output_capacity,
                              audio_frontend_diag_t *diag);
