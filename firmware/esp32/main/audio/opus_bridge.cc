#include "opus_bridge.h"

#include <vector>

#include "opus_encoder.h"

struct aura_opus_encoder {
    OpusEncoderWrapper *impl;
    std::vector<int16_t> pcm_scratch;
    std::vector<uint8_t> opus_scratch;
};

aura_opus_encoder_t *aura_opus_encoder_create(int sample_rate, int channels, int duration_ms)
{
    auto *encoder = new aura_opus_encoder_t;
    encoder->impl = new OpusEncoderWrapper(sample_rate, channels, duration_ms);
    encoder->pcm_scratch.reserve(sample_rate / 1000 * channels * duration_ms);
    encoder->opus_scratch.reserve(MAX_OPUS_PACKET_SIZE);
    return encoder;
}

void aura_opus_encoder_destroy(aura_opus_encoder_t *encoder)
{
    if (!encoder) return;
    delete encoder->impl;
    delete encoder;
}

void aura_opus_encoder_reset(aura_opus_encoder_t *encoder)
{
    if (!encoder || !encoder->impl) return;
    encoder->impl->ResetState();
}

void aura_opus_encoder_set_dtx(aura_opus_encoder_t *encoder, bool enable)
{
    if (!encoder || !encoder->impl) return;
    encoder->impl->SetDtx(enable);
}

void aura_opus_encoder_set_complexity(aura_opus_encoder_t *encoder, int complexity)
{
    if (!encoder || !encoder->impl) return;
    encoder->impl->SetComplexity(complexity);
}

void aura_opus_encoder_set_bitrate(aura_opus_encoder_t *encoder, int bitrate)
{
    (void)encoder;
    (void)bitrate;
}

bool aura_opus_encoder_encode(
    aura_opus_encoder_t *encoder,
    const int16_t *pcm,
    size_t samples,
    uint8_t *opus_out,
    size_t opus_capacity,
    size_t *opus_len
)
{
    if (!encoder || !encoder->impl || !pcm || !opus_out || !opus_len) {
        return false;
    }

    encoder->pcm_scratch.assign(pcm, pcm + samples);
    encoder->opus_scratch.clear();
    if (!encoder->impl->Encode(std::move(encoder->pcm_scratch), encoder->opus_scratch)) {
        return false;
    }
    if (encoder->opus_scratch.size() > opus_capacity) {
        return false;
    }

    for (size_t i = 0; i < encoder->opus_scratch.size(); ++i) {
        opus_out[i] = encoder->opus_scratch[i];
    }
    *opus_len = encoder->opus_scratch.size();
    return true;
}
