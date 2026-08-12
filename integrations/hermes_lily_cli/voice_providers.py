"""Provider-specific ASR/TTS requests for Aura Lily's native voice gateway.

This module deliberately keeps provider transport separate from the ESP32
WebSocket protocol. It borrows protocol structure from the MIT-licensed
xinnan-tech/xiaozhi-esp32-server providers, then adapts it to Aura's one-turn
PCM/WAV pipeline and configuration model.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit

from websockets.sync.client import connect as sync_ws_connect


NLS_HOST = "nls-gateway-cn-shanghai.aliyuncs.com"
VOLCENGINE_ASR_URL = "wss://openspeech.bytedance.com/api/v2/asr"
VOLCENGINE_TTS_URL = "https://openspeech.bytedance.com/api/v1/tts"
BAIDU_OAUTH_URL = "https://aip.baidubce.com/oauth/2.0/token"

VOLC_CLIENT_FULL_REQUEST = 0b0001
VOLC_CLIENT_AUDIO_ONLY_REQUEST = 0b0010
VOLC_SERVER_FULL_RESPONSE = 0b1001
VOLC_SERVER_ACK = 0b1011
VOLC_SERVER_ERROR = 0b1111
VOLC_NEG_SEQUENCE = 0b0010
VOLC_GZIP = 0b0001
VOLC_JSON = 0b0001


class VoiceProviderError(RuntimeError):
    pass


def provider_options(runtime_config: Any, kind: str) -> dict[str, Any]:
    raw = getattr(runtime_config, f"{kind}_provider_options", {}) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def provider_text(options: dict[str, Any], key: str, fallback: Any = "") -> str:
    value = options.get(key, fallback)
    return str(value or "").strip()


def language_for_provider(value: Any) -> str:
    language = str(value or "zh").strip().lower()
    if language.startswith("ja"):
        return "ja-JP"
    if language.startswith("en"):
        return "en-US"
    return "zh-CN"


def pcm_from_wav(wav_bytes: bytes) -> bytes:
    """Return mono PCM frames from Aura's internally generated WAV container."""

    if len(wav_bytes) < 44 or wav_bytes[:4] != b"RIFF":
        return wav_bytes
    data_index = wav_bytes.find(b"data")
    if data_index < 0 or data_index + 8 > len(wav_bytes):
        return wav_bytes
    size = int.from_bytes(wav_bytes[data_index + 4:data_index + 8], "little", signed=False)
    return wav_bytes[data_index + 8:data_index + 8 + size]


def _urlopen(req: request.Request, timeout: float) -> bytes:
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        raise VoiceProviderError(f"HTTP {exc.code}") from exc
    except (OSError, URLError) as exc:
        raise VoiceProviderError(exc.__class__.__name__) from exc


def _json_request(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    raw = _urlopen(request.Request(url, data=body, headers=headers, method="POST"), timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VoiceProviderError("invalid JSON response") from exc


def _bearer_headers(api_key: str, *, content_type: str = "application/json") -> dict[str, str]:
    headers = {"content-type": content_type}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    return headers


def _nls_url(base_url: str, path: str) -> str:
    parsed = urlsplit(str(base_url or "").strip() or f"https://{NLS_HOST}")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise VoiceProviderError("NLS base URL is invalid")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def transcribe_aliyun_nls(runtime_config: Any, wav_bytes: bytes, *, timeout: float) -> str:
    options = provider_options(runtime_config, "asr")
    token = provider_text(options, "token", getattr(runtime_config, "asr_api_key", ""))
    appkey = provider_text(options, "appkey")
    if not token or not appkey:
        raise VoiceProviderError("阿里云 NLS ASR 需要 appkey 和 token")
    query = urlencode({
        "appkey": appkey,
        "format": "pcm",
        "sample_rate": 16000,
        "enable_punctuation_prediction": "true",
        "enable_inverse_text_normalization": "true",
        "enable_voice_detection": "false",
    })
    url = f"{_nls_url(getattr(runtime_config, 'asr_base_url', ''), '/stream/v1/asr')}?{query}"
    req = request.Request(
        url,
        data=pcm_from_wav(wav_bytes),
        method="POST",
        headers={"X-NLS-Token": token, "Content-Type": "application/octet-stream"},
    )
    raw = _urlopen(req, timeout)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VoiceProviderError("NLS ASR returned invalid JSON") from exc
    if int(payload.get("status") or 0) != 20000000:
        raise VoiceProviderError(str(payload.get("message") or payload.get("status") or "NLS ASR failed"))
    return str(payload.get("result") or "").strip()


def _volc_header(message_type: int, flags: int = 0) -> bytearray:
    return bytearray([(0b0001 << 4) | 1, (message_type << 4) | flags, (VOLC_JSON << 4) | VOLC_GZIP, 0])


def _volc_response(packet: bytes) -> dict[str, Any]:
    if len(packet) < 8:
        raise VoiceProviderError("火山引擎 ASR 响应过短")
    header_size = packet[0] & 0x0F
    message_type = packet[1] >> 4
    compression = packet[2] & 0x0F
    payload = packet[header_size * 4:]
    if message_type == VOLC_SERVER_ERROR:
        code = int.from_bytes(payload[:4], "big", signed=False)
        return {"code": code, "error": payload[8:].decode("utf-8", errors="replace")}
    if message_type not in {VOLC_SERVER_FULL_RESPONSE, VOLC_SERVER_ACK}:
        return {}
    offset = 4 if message_type == VOLC_SERVER_FULL_RESPONSE else 8
    if len(payload) < offset:
        return {}
    content = payload[offset:]
    if compression == VOLC_GZIP and content:
        content = gzip.decompress(content)
    try:
        return json.loads(content.decode("utf-8")) if content else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VoiceProviderError("火山引擎 ASR 响应无效") from exc


def transcribe_volcengine(runtime_config: Any, wav_bytes: bytes, *, timeout: float) -> str:
    options = provider_options(runtime_config, "asr")
    appid = provider_text(options, "appid")
    cluster = provider_text(options, "cluster")
    token = provider_text(options, "access_token", getattr(runtime_config, "asr_api_key", ""))
    if not appid or not cluster or not token:
        raise VoiceProviderError("火山引擎 ASR 需要 appid、cluster 和 access_token")
    url = str(getattr(runtime_config, "asr_base_url", "") or VOLCENGINE_ASR_URL)
    if not url.startswith(("ws://", "wss://")):
        url = VOLCENGINE_ASR_URL
    config = {
        "app": {"appid": appid, "cluster": cluster, "token": token},
        "user": {"uid": str(uuid.uuid4())},
        "request": {"reqid": str(uuid.uuid4()), "show_utterances": False, "sequence": 1},
        "audio": {
            "format": "raw", "rate": 16000, "language": language_for_provider(getattr(runtime_config, "asr_language", "zh")),
            "bits": 16, "channel": 1, "codec": "raw",
        },
    }
    raw_config = gzip.compress(json.dumps(config).encode("utf-8"))
    first = _volc_header(VOLC_CLIENT_FULL_REQUEST)
    first.extend(len(raw_config).to_bytes(4, "big"))
    first.extend(raw_config)
    pcm = pcm_from_wav(wav_bytes)
    last = _volc_header(VOLC_CLIENT_AUDIO_ONLY_REQUEST, VOLC_NEG_SEQUENCE)
    compressed_audio = gzip.compress(pcm)
    last.extend(len(compressed_audio).to_bytes(4, "big"))
    last.extend(compressed_audio)
    try:
        with sync_ws_connect(url, additional_headers={"Authorization": f"Bearer; {token}"}, open_timeout=timeout, close_timeout=timeout) as ws:
            ws.send(first)
            _volc_response(ws.recv())
            ws.send(last)
            payload = _volc_response(ws.recv())
    except Exception as exc:
        raise VoiceProviderError(f"火山引擎 ASR: {exc.__class__.__name__}") from exc
    if int(payload.get("code") or 0) not in {0, 1000, 1013}:
        raise VoiceProviderError(str(payload.get("message") or payload.get("code") or "火山引擎 ASR 失败"))
    results = payload.get("result") if isinstance(payload.get("result"), list) else []
    return str((results[0] or {}).get("text") or "").strip() if results else ""


def transcribe_baidu(runtime_config: Any, wav_bytes: bytes, *, timeout: float) -> str:
    options = provider_options(runtime_config, "asr")
    token = baidu_access_token(options, getattr(runtime_config, "asr_api_key", ""), timeout=timeout)
    payload = {
        "format": "pcm", "rate": 16000, "channel": 1, "token": token,
        "cuid": provider_text(options, "cuid", "aura-lily"), "len": len(pcm_from_wav(wav_bytes)),
        "speech": base64.b64encode(pcm_from_wav(wav_bytes)).decode("ascii"),
        "dev_pid": int(provider_text(options, "dev_pid", "1537") or 1537),
    }
    url = str(getattr(runtime_config, "asr_base_url", "") or "https://vop.baidu.com/server_api")
    response = _json_request(url, payload, {"content-type": "application/json"}, timeout)
    if int(response.get("err_no") or 0) != 0:
        raise VoiceProviderError(str(response.get("err_msg") or response.get("err_no") or "百度 ASR 失败"))
    result = response.get("result") if isinstance(response.get("result"), list) else []
    return str(result[0] or "").strip() if result else ""


def transcribe_qwen3_asr(runtime_config: Any, wav_bytes: bytes, *, timeout: float) -> str:
    try:
        import dashscope  # type: ignore[import-not-found]
    except ImportError as exc:
        raise VoiceProviderError("Qwen3-ASR 需要安装 dashscope") from exc
    import tempfile
    from pathlib import Path

    api_key = provider_text(provider_options(runtime_config, "asr"), "api_key", getattr(runtime_config, "asr_api_key", ""))
    if not api_key:
        raise VoiceProviderError("Qwen3-ASR 需要 DashScope API Key")
    with tempfile.TemporaryDirectory(prefix="aura-qwen-asr-") as tmp:
        path = Path(tmp) / "turn.wav"
        path.write_bytes(wav_bytes)
        dashscope.api_key = api_key
        response = dashscope.MultiModalConversation.call(
            model=str(getattr(runtime_config, "asr_model", "") or "qwen3-asr-flash"),
            messages=[{"role": "user", "content": [{"audio": str(path)}]}],
            result_format="message",
            asr_options={"enable_lid": True, "enable_itn": True, "language": language_for_provider(getattr(runtime_config, "asr_language", "zh"))},
            stream=True,
        )
        text = ""
        for chunk in response:
            try:
                output = chunk["output"] if isinstance(chunk, dict) else chunk.output
                choice = output["choices"][0] if isinstance(output, dict) else output.choices[0]
                message = choice["message"] if isinstance(choice, dict) else choice.message
                content = message["content"] if isinstance(message, dict) else message.content
                item = content[0]
                text = str(item["text"] if isinstance(item, dict) else item.text).strip()
            except (AttributeError, KeyError, IndexError, TypeError):
                continue
    return text


def baidu_access_token(options: dict[str, Any], fallback: str, *, timeout: float) -> str:
    token = provider_text(options, "access_token", fallback)
    if token:
        return token
    client_id = provider_text(options, "client_id")
    client_secret = provider_text(options, "client_secret")
    if not client_id or not client_secret:
        raise VoiceProviderError("百度语音需要 access_token 或 client_id/client_secret")
    url = f"{BAIDU_OAUTH_URL}?{urlencode({'grant_type': 'client_credentials', 'client_id': client_id, 'client_secret': client_secret})}"
    raw = _urlopen(request.Request(url, method="POST"), timeout)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VoiceProviderError("百度 token 响应无效") from exc
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise VoiceProviderError(str(payload.get("error_description") or "百度 token 获取失败"))
    return token


def synthesize_aliyun_nls(runtime_config: Any, text: str, *, sample_rate: int, timeout: float) -> bytes:
    options = provider_options(runtime_config, "tts")
    token = provider_text(options, "token", getattr(runtime_config, "tts_api_key", ""))
    appkey = provider_text(options, "appkey")
    if not token or not appkey:
        raise VoiceProviderError("阿里云 NLS TTS 需要 appkey 和 token")
    payload = {
        "appkey": appkey, "token": token, "text": text, "format": "pcm", "sample_rate": sample_rate,
        "voice": str(getattr(runtime_config, "tts_voice", "") or "xiaoyun"),
        "volume": int(provider_text(options, "volume", "50") or 50),
        "speech_rate": int(provider_text(options, "speech_rate", "0") or 0),
        "pitch_rate": int(provider_text(options, "pitch_rate", "0") or 0),
    }
    req = request.Request(
        _nls_url(getattr(runtime_config, "tts_base_url", ""), "/stream/v1/tts"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST", headers={"Content-Type": "application/json"},
    )
    raw = _urlopen(req, timeout)
    if raw.lstrip().startswith((b"{", b"[")):
        raise VoiceProviderError(_error_text(raw, "阿里云 NLS TTS 失败"))
    return raw


def synthesize_volcengine(runtime_config: Any, text: str, *, sample_rate: int, timeout: float) -> bytes:
    options = provider_options(runtime_config, "tts")
    appid = provider_text(options, "appid")
    cluster = provider_text(options, "cluster")
    token = provider_text(options, "access_token", getattr(runtime_config, "tts_api_key", ""))
    voice = str(getattr(runtime_config, "tts_voice", "") or provider_text(options, "voice_type"))
    if not appid or not cluster or not token or not voice:
        raise VoiceProviderError("火山引擎 TTS 需要 appid、cluster、access_token 和 voice")
    payload = {
        "app": {"appid": appid, "token": token, "cluster": cluster},
        "user": {"uid": provider_text(options, "uid", "aura-lily")},
        "audio": {"voice_type": voice, "encoding": "pcm", "speed_ratio": float(provider_text(options, "speed_ratio", "1")), "volume_ratio": float(provider_text(options, "volume_ratio", "1")), "pitch_ratio": float(provider_text(options, "pitch_ratio", "1")), "rate": sample_rate},
        "request": {"reqid": str(uuid.uuid4()), "text": text, "text_type": "plain", "operation": "query", "with_frontend": 1, "frontend_type": "unitTson"},
    }
    url = str(getattr(runtime_config, "tts_base_url", "") or VOLCENGINE_TTS_URL)
    result = _json_request(url, payload, {"Content-Type": "application/json", "Authorization": f"Bearer; {token}"}, timeout)
    data = str(result.get("data") or "")
    if not data:
        raise VoiceProviderError(str((result.get("message") or result.get("error")) or "火山引擎 TTS 失败"))
    try:
        return base64.b64decode(data)
    except ValueError as exc:
        raise VoiceProviderError("火山引擎 TTS 音频无效") from exc


def synthesize_baidu(runtime_config: Any, text: str, *, sample_rate: int, timeout: float) -> bytes:
    options = provider_options(runtime_config, "tts")
    token = baidu_access_token(options, getattr(runtime_config, "tts_api_key", ""), timeout=timeout)
    payload = urlencode({
        "tex": text, "tok": token, "cuid": provider_text(options, "cuid", "aura-lily"), "ctp": 1,
        "lan": language_for_provider(getattr(runtime_config, "asr_language", "zh")).split("-", 1)[0],
        "spd": provider_text(options, "speed", "5"), "pit": provider_text(options, "pitch", "5"),
        # aue=4 returns raw 16 kHz PCM. The gateway resamples PCM only; WAV
        # would otherwise be treated as audible samples and corrupt playback.
        "vol": provider_text(options, "volume", "5"), "per": getattr(runtime_config, "tts_voice", "") or provider_text(options, "voice", "0"), "aue": 4,
    }).encode("utf-8")
    url = str(getattr(runtime_config, "tts_base_url", "") or "https://tsn.baidu.com/text2audio")
    raw = _urlopen(request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout)
    if raw.lstrip().startswith((b"{", b"[")):
        raise VoiceProviderError(_error_text(raw, "百度 TTS 失败"))
    return raw


def synthesize_minimax(runtime_config: Any, text: str, *, sample_rate: int, timeout: float) -> bytes:
    options = provider_options(runtime_config, "tts")
    api_key = provider_text(options, "api_key", getattr(runtime_config, "tts_api_key", ""))
    group_id = provider_text(options, "group_id")
    if not api_key or not group_id:
        raise VoiceProviderError("MiniMax TTS 需要 group_id 和 API Key")
    url = str(getattr(runtime_config, "tts_base_url", "") or "https://api.minimaxi.com/v1/t2a_v2")
    sep = "&" if "?" in url else "?"
    payload = {
        "model": str(getattr(runtime_config, "tts_model", "") or "speech-2.6-hd"), "text": text, "stream": False,
        "voice_setting": {"voice_id": str(getattr(runtime_config, "tts_voice", "") or provider_text(options, "voice_id", "female-shaonv")), "speed": float(provider_text(options, "speed", "1")), "vol": float(provider_text(options, "volume", "1")), "pitch": int(provider_text(options, "pitch", "0"))},
        "audio_setting": {"sample_rate": sample_rate, "bitrate": 128000, "format": "pcm", "channel": 1},
    }
    result = _json_request(f"{url}{sep}GroupId={group_id}", payload, _bearer_headers(api_key), timeout)
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    audio = data.get("audio") or data.get("audio_hex") or ""
    if not audio:
        raise VoiceProviderError(str((result.get("base_resp") or {}).get("status_msg") or "MiniMax TTS 失败"))
    try:
        return bytes.fromhex(audio)
    except ValueError:
        try:
            return base64.b64decode(audio)
        except ValueError as exc:
            raise VoiceProviderError("MiniMax TTS 音频无效") from exc


def _error_text(raw: bytes, fallback: str) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback
    return str(payload.get("message") or payload.get("error") or payload.get("msg") or fallback)


# Adapted from xinnan-tech/xiaozhi-esp32-server's Tencent TC3 signing helpers.
def tencent_headers(*, service: str, action: str, version: str, secret_id: str, secret_key: str, payload: dict[str, Any], region: str = "ap-shanghai") -> tuple[dict[str, str], bytes]:
    if not secret_id or not secret_key:
        raise VoiceProviderError("腾讯云需要 secret_id 和 secret_key")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    timestamp = int(time.time())
    day = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
    host = f"{service}.tencentcloudapi.com"
    canonical_headers = f"content-type:application/json\nhost:{host}\n"
    signed_headers = "content-type;host"
    canonical = "\n".join(["POST", "/", "", canonical_headers, signed_headers, hashlib.sha256(body).hexdigest()])
    scope = f"{day}/{service}/tc3_request"
    string_to_sign = "\n".join(["TC3-HMAC-SHA256", str(timestamp), scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    date_key = hmac.new(f"TC3{secret_key}".encode("utf-8"), day.encode("utf-8"), hashlib.sha256).digest()
    service_key = hmac.new(date_key, service.encode("utf-8"), hashlib.sha256).digest()
    signing_key = hmac.new(service_key, b"tc3_request", hashlib.sha256).digest()
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    return ({"Content-Type": "application/json", "Host": host, "Authorization": authorization, "X-TC-Action": action, "X-TC-Version": version, "X-TC-Timestamp": str(timestamp), "X-TC-Region": region}, body)


def transcribe_tencent(runtime_config: Any, wav_bytes: bytes, *, timeout: float) -> str:
    options = provider_options(runtime_config, "asr")
    secret_id = provider_text(options, "secret_id")
    secret_key = provider_text(options, "secret_key", getattr(runtime_config, "asr_api_key", ""))
    service = "asr"
    pcm = pcm_from_wav(wav_bytes)
    encoded_pcm = base64.b64encode(pcm).decode("ascii")
    payload = {
        "ProjectId": 0,
        "SubServiceType": 2,
        "EngSerViceType": provider_text(options, "engine_type", "16k_zh"),
        "SourceType": 1,
        "VoiceFormat": "pcm",
        "Data": encoded_pcm,
        # Tencent expects the byte length of Base64 audio, not decoded PCM.
        "DataLen": len(encoded_pcm),
    }
    headers, body = tencent_headers(
        service=service,
        action="SentenceRecognition",
        version="2019-06-14",
        secret_id=secret_id,
        secret_key=secret_key,
        payload=payload,
        region=provider_text(options, "region", "ap-shanghai"),
    )
    raw = _urlopen(request.Request(f"https://{service}.tencentcloudapi.com", data=body, headers=headers, method="POST"), timeout)
    try:
        response = json.loads(raw.decode("utf-8")).get("Response") or {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VoiceProviderError("腾讯云 ASR 响应无效") from exc
    if response.get("Error"):
        error = response["Error"]
        raise VoiceProviderError(f"{error.get('Code')}: {error.get('Message')}")
    return str(response.get("Result") or "").strip()


def synthesize_tencent(runtime_config: Any, text: str, *, sample_rate: int, timeout: float) -> bytes:
    options = provider_options(runtime_config, "tts")
    secret_id = provider_text(options, "secret_id")
    secret_key = provider_text(options, "secret_key", getattr(runtime_config, "tts_api_key", ""))
    voice = str(getattr(runtime_config, "tts_voice", "") or provider_text(options, "voice_type", "101001"))
    try:
        voice_type = int(voice)
    except ValueError as exc:
        raise VoiceProviderError("腾讯云 TTS 的 Voice 必须为数字音色 ID") from exc
    service = "tts"
    payload = {
        "Text": text,
        "SessionId": str(uuid.uuid4()),
        "VoiceType": voice_type,
        "Codec": "pcm",
        "Volume": int(provider_text(options, "volume", "0") or 0),
        "Speed": int(provider_text(options, "speed", "0") or 0),
        "SampleRate": int(sample_rate),
    }
    headers, body = tencent_headers(
        service=service,
        action="TextToVoice",
        version="2019-08-23",
        secret_id=secret_id,
        secret_key=secret_key,
        payload=payload,
        region=provider_text(options, "region", "ap-shanghai"),
    )
    raw = _urlopen(request.Request(f"https://{service}.tencentcloudapi.com", data=body, headers=headers, method="POST"), timeout)
    try:
        response = json.loads(raw.decode("utf-8")).get("Response") or {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VoiceProviderError("腾讯云 TTS 响应无效") from exc
    if response.get("Error"):
        error = response["Error"]
        raise VoiceProviderError(f"{error.get('Code')}: {error.get('Message')}")
    try:
        return base64.b64decode(str(response.get("Audio") or ""), validate=True)
    except ValueError as exc:
        raise VoiceProviderError("腾讯云 TTS 音频无效") from exc
