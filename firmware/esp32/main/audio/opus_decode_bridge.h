#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct aura_opus_decoder aura_opus_decoder_t;

aura_opus_decoder_t *aura_opus_decoder_create(int sample_rate, int channels, int duration_ms);
void aura_opus_decoder_destroy(aura_opus_decoder_t *decoder);
void aura_opus_decoder_reset(aura_opus_decoder_t *decoder);
bool aura_opus_decoder_decode(
    aura_opus_decoder_t *decoder,
    const uint8_t *opus,
    size_t opus_len,
    const int16_t **pcm_out,
    size_t *pcm_samples
);

#ifdef __cplusplus
}
#endif
