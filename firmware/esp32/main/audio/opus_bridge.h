#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct aura_opus_encoder aura_opus_encoder_t;

aura_opus_encoder_t *aura_opus_encoder_create(int sample_rate, int channels, int duration_ms);
void aura_opus_encoder_destroy(aura_opus_encoder_t *encoder);
void aura_opus_encoder_reset(aura_opus_encoder_t *encoder);
void aura_opus_encoder_set_dtx(aura_opus_encoder_t *encoder, bool enable);
void aura_opus_encoder_set_complexity(aura_opus_encoder_t *encoder, int complexity);
void aura_opus_encoder_set_bitrate(aura_opus_encoder_t *encoder, int bitrate);
bool aura_opus_encoder_encode(
    aura_opus_encoder_t *encoder,
    const int16_t *pcm,
    size_t samples,
    uint8_t *opus_out,
    size_t opus_capacity,
    size_t *opus_len
);

#ifdef __cplusplus
}
#endif
