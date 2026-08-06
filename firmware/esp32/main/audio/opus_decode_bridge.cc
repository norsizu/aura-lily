#include "opus_decode_bridge.h"

#include <vector>

#include "opus_decoder.h"

struct aura_opus_decoder {
    OpusDecoderWrapper *impl;
    std::vector<uint8_t> opus_scratch;
    std::vector<int16_t> pcm_scratch;
};

aura_opus_decoder_t *aura_opus_decoder_create(int sample_rate, int channels, int duration_ms)
{
    auto *decoder = new aura_opus_decoder_t;
    decoder->impl = new OpusDecoderWrapper(sample_rate, channels, duration_ms);
    decoder->opus_scratch.reserve(512);
    decoder->pcm_scratch.reserve((sample_rate / 1000) * channels * duration_ms);
    return decoder;
}

void aura_opus_decoder_destroy(aura_opus_decoder_t *decoder)
{
    if (!decoder) return;
    delete decoder->impl;
    delete decoder;
}

void aura_opus_decoder_reset(aura_opus_decoder_t *decoder)
{
    if (!decoder || !decoder->impl) return;
    decoder->impl->ResetState();
}

bool aura_opus_decoder_decode(
    aura_opus_decoder_t *decoder,
    const uint8_t *opus,
    size_t opus_len,
    const int16_t **pcm_out,
    size_t *pcm_samples
)
{
    if (!decoder || !decoder->impl || !opus || opus_len == 0 || !pcm_out || !pcm_samples) {
        return false;
    }

    decoder->opus_scratch.assign(opus, opus + opus_len);
    decoder->pcm_scratch.clear();
    if (!decoder->impl->Decode(std::move(decoder->opus_scratch), decoder->pcm_scratch)) {
        return false;
    }
    *pcm_out = decoder->pcm_scratch.empty() ? nullptr : decoder->pcm_scratch.data();
    *pcm_samples = decoder->pcm_scratch.size();
    return true;
}
