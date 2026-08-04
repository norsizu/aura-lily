from __future__ import annotations

import json
import os
import time
import uuid
from io import BytesIO
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPSConnection, HTTPResponse
from threading import Lock
from typing import Any, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from integrations.hermes_lily_cli.bridge import HermesLilyResult, scrub_json_value, scrub_text
from integrations.hermes_lily_cli.runtime_config import load_hermes_provider_catalog
from integrations.aura_persona_gateway.response_contract import DIRECT_LLM_SYSTEM_PROMPT

LOW_LATENCY_STREAM_INSTRUCTION = (
    "当前是实时语音对话。优先快速给出自然口语答复；"
    "闲聊、寒暄和简单问答优先一到两句完整短答、约四十八个汉字以内；"
    "用户在问知识、原因、步骤、对比，或明确要求详细讲解时，可以分几句把内容讲全，不受四十八字限制，"
    "但仍要先给结论、口语表达、讲完即止。"
    "第一句先直接回答问题，第二句只补充最关键依据或下一步。"
    "第一句必须包含用户本轮话题里的一个具体词或同义改写，例如状态、工作、天气、速度、位置、情绪、计划；不能只表示你在听。"
    "不要用“嗯”“我想一下”“稍等”这类前导占位，一开口就是答案。"
    "不要用“行呀”“好呀”“那咱们”“直奔主题”“进入正题”“话不多说”这类无信息量过渡语开头。"
    "不要用损人、训人、油腻网络腔或过度吐槽的说法，例如“过家家”“烂事儿”“魂不在”“手痒”。"
    "闲聊时不要用“你说”“我在听”“那我听着”“你想从哪儿开始讲”“还有什么想聊的”这类空洞开头；第一句必须包含对用户话题的具体承接、感受或追问点。"
    "如果用户说想聊最近状态，不要只问从哪儿开始；要先承接“最近状态”，再给一个很短的切入点。"
    "用户说“我最近状态/聊聊最近状态/复盘工作状态”时，说的是用户状态，不是你的状态；不要编你自己在后台、没睡、忙完或喘口气。"
    "没有上下文证据时，不要替用户诊断紧绷、焦虑、疲惫、效率低、项目压力、熬夜、加班或最近具体经历；改用开放式问题承接。"
    "示例：用户说“我想复盘最近的状态”，你可以答“从工作节奏说起：是事情太满，还是提不起劲？”；禁止只复读成“最近状态啊？”"
    "用户说语音链路、首字、首包或首音频时，指本设备 ASR、模型首句、TTS 首音频和播放链路，不是手机基站。"
)
# ── 前端快模型的意图判断（替代正则路由）───────────────────────
# 快模型自己判断"这轮我答不答得准"：答不准就第一个字符输出 [后台] 标记行，
# 网关截住这行（不进 TTS），转后台 agent 联网/干活。正则只做兜底。
AGENT_TASK_MARKER = "[后台]"
AGENT_TASK_STREAM_INSTRUCTION = (
    "遇到你自己答不准的问题——需要联网查实时信息（新闻、股价、币价、比分、汇率、未来天气预报）、"
    "需要查资料核实具体事实，或者要产出文档、报告、代码、表格这类交付物——不要编造，也不要解释你做不到；"
    "改为从第一个字符起就输出标记行：[后台]加一句不超过三十个字的任务描述，然后立刻停止，不要再输出任何别的字。"
    "例如用户问“比特币现在多少钱”，你只输出“[后台]查比特币当前价格”。"
    "常识问答、闲聊、寒暄、情感陪伴、观点和你知识范围内能稳答的内容照常直接回答，禁止输出[后台]。"
)
NO_REASONING_EFFORT_VALUES = {"", "none", "off", "disabled", "false", "0"}
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
DIRECT_LLM_HTTP_KEEPALIVE_ENV = "AURA_DIRECT_LLM_HTTP_KEEPALIVE_ENABLED"
DIRECT_LLM_HTTP_KEEPALIVE_RETRY_ENV = "AURA_DIRECT_LLM_HTTP_KEEPALIVE_RETRY_ONCE"
DIRECT_LLM_HTTP_WARM_ENV = "AURA_DIRECT_LLM_HTTP_WARM_ENABLED"
DIRECT_LLM_EMPTY_RESPONSE_FALLBACK = "刚才没有生成出有效回复，你再说一遍？"


@dataclass(frozen=True)
class DirectLlmConfig:
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout_seconds: float = 90.0
    max_tokens: int = 96
    temperature: float = 0.4
    reasoning_effort: str = "low"
    system_prompt: str = ""
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    # 流式语音时让模型自己做意图判断：答不准就输出 [后台] 标记转后台 agent。
    agent_marker_enabled: bool = False


class DirectLlmClient:
    """Small OpenAI-compatible chat client for Aura's non-agent reply path."""

    def __init__(self, config: DirectLlmConfig) -> None:
        self.config = config

    def run(self, prompt: str, *, metadata: Mapping[str, Any] | None = None) -> HermesLilyResult:
        request_id = f"aura-llm-{uuid.uuid4().hex[:12]}"
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            return HermesLilyResult(
                ok=False,
                status="failed",
                response="goal is required",
                request_id=request_id,
                latency_ms=0,
                evidence={"error": "empty_goal", "route": "direct_llm"},
            )
        if not self.config.model.strip():
            return HermesLilyResult(
                ok=False,
                status="failed",
                response="Aura direct LLM model is not configured.",
                request_id=request_id,
                latency_ms=0,
                evidence={"error": "missing_model", "route": "direct_llm"},
            )
        chat_url = chat_completions_url(self.config.base_url, provider=self.config.provider)
        if not chat_url:
            return HermesLilyResult(
                ok=False,
                status="failed",
                response="Aura direct LLM Base URL is not configured.",
                request_id=request_id,
                latency_ms=0,
                evidence={"error": "missing_base_url", "route": "direct_llm"},
            )

        chat_body = self._chat_body(clean_prompt, stream=False)
        trace = self._chat_trace(clean_prompt, chat_body)
        body = json.dumps(chat_body, ensure_ascii=False).encode("utf-8")
        headers = self._chat_headers()
        req = Request(chat_url, data=body, method="POST", headers=headers)
        started = time.monotonic()
        try:
            with urlopen(req, timeout=max(1.0, float(self.config.timeout_seconds or 90.0))) as res:
                raw = res.read()
                payload = json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            detail = _http_error_detail(exc)
            return HermesLilyResult(
                ok=False,
                status="failed",
                response=f"Aura direct LLM HTTP {exc.code}.",
                request_id=request_id,
                latency_ms=_latency_ms(started),
                evidence={
                    "stop_reason": "http_error",
                    "route": "direct_llm",
                    "provider": self.config.provider,
                    "model": self.config.model,
                    "status": exc.code,
                    **trace,
                    "detail": scrub_text(detail, 500),
                    "metadata": scrub_json_value(dict(metadata or {})),
                },
            )
        except (OSError, URLError, json.JSONDecodeError) as exc:
            return HermesLilyResult(
                ok=False,
                status="failed",
                response=f"Aura direct LLM failed: {exc.__class__.__name__}.",
                request_id=request_id,
                latency_ms=_latency_ms(started),
                evidence={
                    "stop_reason": "network_or_parse_error",
                    "route": "direct_llm",
                    "provider": self.config.provider,
                    "model": self.config.model,
                    "error_type": exc.__class__.__name__,
                    **trace,
                    "metadata": scrub_json_value(dict(metadata or {})),
                },
            )

        response = extract_chat_text(payload).strip()
        ok = bool(response)
        return HermesLilyResult(
            ok=ok,
            status="completed" if ok else "failed",
            response=response or DIRECT_LLM_EMPTY_RESPONSE_FALLBACK,
            request_id=request_id,
            latency_ms=_latency_ms(started),
            evidence={
                "stop_reason": "finished" if ok else "empty_response",
                "route": "direct_llm",
                "provider": self.config.provider,
                "model": self.config.model,
                **trace,
                "metadata": scrub_json_value(dict(metadata or {})),
            },
        )

    def stream(self, prompt: str, *, metadata: Mapping[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        request_id = f"aura-llm-{uuid.uuid4().hex[:12]}"
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            yield _stream_final(
                ok=False,
                status="failed",
                response="goal is required",
                request_id=request_id,
                latency_ms=0,
                evidence={"error": "empty_goal", "route": "direct_llm"},
            )
            return
        if not self.config.model.strip():
            yield _stream_final(
                ok=False,
                status="failed",
                response="Aura direct LLM model is not configured.",
                request_id=request_id,
                latency_ms=0,
                evidence={"error": "missing_model", "route": "direct_llm"},
            )
            return
        chat_url = chat_completions_url(self.config.base_url, provider=self.config.provider)
        if not chat_url:
            yield _stream_final(
                ok=False,
                status="failed",
                response="Aura direct LLM Base URL is not configured.",
                request_id=request_id,
                latency_ms=0,
                evidence={"error": "missing_base_url", "route": "direct_llm"},
            )
            return

        chat_body = self._chat_body(clean_prompt, stream=True)
        trace = self._chat_trace(clean_prompt, chat_body)
        body = json.dumps(chat_body, ensure_ascii=False).encode("utf-8")
        headers = self._chat_headers()
        req = Request(chat_url, data=body, method="POST", headers=headers)
        started = time.monotonic()
        first_delta_ms = 0
        response_open_ms = 0
        keepalive_reused = False
        keepalive_retried = False
        parts: list[str] = []
        try:
            with _open_chat_request(
                req,
                timeout=max(1.0, float(self.config.timeout_seconds or 90.0)),
                reusable=_direct_llm_http_keepalive_enabled(),
            ) as res:
                keepalive_reused = bool(getattr(res, "reused_connection", False))
                keepalive_retried = bool(getattr(res, "retried_connection", False))
                response_open_ms = _latency_ms(started)
                for payload in _iter_stream_payloads(res):
                    delta = extract_chat_delta_text(payload)
                    if delta:
                        if not first_delta_ms:
                            first_delta_ms = _latency_ms(started)
                        parts.append(delta)
                        yield {
                            "type": "delta",
                            "text": delta,
                            "timing": {
                                **trace,
                                "aura_llm_response_open_ms": response_open_ms,
                                "aura_llm_first_delta_ms": first_delta_ms,
                                "aura_llm_response_to_first_delta_ms": max(0, first_delta_ms - response_open_ms),
                                "aura_llm_http_keepalive": keepalive_reused,
                                "aura_llm_http_keepalive_retry": keepalive_retried,
                            },
                        }
                        continue
                    if not parts:
                        full = extract_chat_text(payload)
                        if full:
                            if not first_delta_ms:
                                first_delta_ms = _latency_ms(started)
                            parts.append(full)
                            yield {
                                "type": "delta",
                                "text": full,
                                "timing": {
                                    **trace,
                                    "aura_llm_response_open_ms": response_open_ms,
                                    "aura_llm_first_delta_ms": first_delta_ms,
                                    "aura_llm_response_to_first_delta_ms": max(0, first_delta_ms - response_open_ms),
                                    "aura_llm_http_keepalive": keepalive_reused,
                                    "aura_llm_http_keepalive_retry": keepalive_retried,
                                },
                            }
        except HTTPError as exc:
            detail = _http_error_detail(exc)
            yield _stream_final(
                ok=False,
                status="failed",
                response=f"Aura direct LLM HTTP {exc.code}.",
                request_id=request_id,
                latency_ms=_latency_ms(started),
                evidence={
                    "stop_reason": "http_error",
                    "route": "direct_llm",
                    "provider": self.config.provider,
                    "model": self.config.model,
                    "status": exc.code,
                    **trace,
                    "detail": scrub_text(detail, 500),
                    "metadata": scrub_json_value(dict(metadata or {})),
                },
            )
            return
        except (OSError, URLError, json.JSONDecodeError) as exc:
            yield _stream_final(
                ok=False,
                status="failed",
                response=f"Aura direct LLM failed: {exc.__class__.__name__}.",
                request_id=request_id,
                latency_ms=_latency_ms(started),
                evidence={
                    "stop_reason": "network_or_parse_error",
                    "route": "direct_llm",
                    "provider": self.config.provider,
                    "model": self.config.model,
                    "error_type": exc.__class__.__name__,
                    **trace,
                    "metadata": scrub_json_value(dict(metadata or {})),
                },
            )
            return

        response = "".join(parts).strip()
        ok = bool(response)
        complete_ms = _latency_ms(started)
        yield _stream_final(
            ok=ok,
            status="completed" if ok else "failed",
            response=response or DIRECT_LLM_EMPTY_RESPONSE_FALLBACK,
            request_id=request_id,
            latency_ms=complete_ms,
            evidence={
                "stop_reason": "finished" if ok else "empty_response",
                "route": "direct_llm",
                "provider": self.config.provider,
                "model": self.config.model,
                **trace,
                "aura_llm_response_open_ms": response_open_ms,
                "aura_llm_first_delta_ms": first_delta_ms,
                "aura_llm_response_to_first_delta_ms": max(0, first_delta_ms - response_open_ms) if first_delta_ms else 0,
                "aura_llm_complete_ms": complete_ms,
                "aura_llm_http_keepalive": keepalive_reused,
                "aura_llm_http_keepalive_retry": keepalive_retried,
                "metadata": scrub_json_value(dict(metadata or {})),
                "streamed": True,
            },
        )

    def _chat_body(self, prompt: str, *, stream: bool) -> dict[str, Any]:
        custom_system_prompt = str(self.config.system_prompt or "").strip()
        if custom_system_prompt:
            # Caller-provided prompt (e.g. KB Q&A) replaces the persona
            # contract entirely; no low-latency persona instruction either.
            system_prompt = custom_system_prompt
        else:
            system_prompt = DIRECT_LLM_SYSTEM_PROMPT
            if stream:
                system_prompt = system_prompt + LOW_LATENCY_STREAM_INSTRUCTION
                if self.config.agent_marker_enabled:
                    system_prompt = system_prompt + AGENT_TASK_STREAM_INSTRUCTION
        body: dict[str, Any] = {
            "model": self.config.model.strip(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": str(prompt or "").strip()},
            ],
            "temperature": _float_range(self.config.temperature, default=0.4, minimum=0.0, maximum=2.0),
        }
        max_tokens = _int_range(self.config.max_tokens, default=96, minimum=16, maximum=4096)
        if max_tokens > 0:
            body["max_tokens"] = max_tokens
        if _model_needs_text_modalities(self.config.model):
            body["modalities"] = ["text"]
        effort = str(self.config.reasoning_effort or "").strip().lower()
        if effort not in NO_REASONING_EFFORT_VALUES:
            body["reasoning_effort"] = effort
        if stream:
            body["stream"] = True
        return body

    def _chat_headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.config.api_key.strip():
            headers["authorization"] = f"Bearer {self.config.api_key.strip()}"
        headers.update({str(k): str(v) for k, v in dict(self.config.extra_headers or {}).items() if v})
        return headers

    def _chat_trace(self, prompt: str, body: Mapping[str, Any]) -> dict[str, Any]:
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        system_prompt = ""
        if messages and isinstance(messages[0], Mapping):
            system_prompt = str(messages[0].get("content") or "")
        trace: dict[str, Any] = {
            "aura_llm_prompt_chars": len(str(prompt or "")),
            "aura_llm_user_prompt_chars": len(str(prompt or "")),
            "aura_llm_system_prompt_chars": len(system_prompt),
            "aura_llm_max_tokens": int(body.get("max_tokens") or 0),
        }
        effort = str(body.get("reasoning_effort") or "").strip()
        if effort:
            trace["aura_llm_reasoning_effort"] = effort
        modalities = body.get("modalities")
        if isinstance(modalities, list) and modalities:
            trace["aura_llm_modalities"] = ",".join(str(item) for item in modalities)
        return trace


def _model_needs_text_modalities(model: str) -> bool:
    name = str(model or "").strip().lower()
    return name.startswith("stepaudio-") and "chat" in name


def extract_chat_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], Mapping) else {}
        message = first.get("message") if isinstance(first.get("message"), Mapping) else {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)
        if isinstance(first.get("text"), str):
            return str(first["text"])
    for key in ("output_text", "text", "response"):
        if isinstance(payload.get(key), str):
            return str(payload[key])
    return ""


def extract_chat_delta_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], Mapping) else {}
        delta = first.get("delta") if isinstance(first.get("delta"), Mapping) else {}
        content = delta.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)
    for key in ("delta", "content_delta"):
        if isinstance(payload.get(key), str):
            return str(payload[key])
    return ""


def _iter_stream_payloads(response: Any) -> Iterator[Mapping[str, Any]]:
    for raw_line in response:
        line = bytes(raw_line or b"").strip()
        if not line:
            continue
        if line.startswith(b"data:"):
            line = line[5:].strip()
        if line == b"[DONE]":
            break
        payload = json.loads(line.decode("utf-8"))
        if isinstance(payload, Mapping):
            yield payload


class _ReusableChatResponse:
    def __init__(
        self,
        *,
        response: HTTPResponse,
        pool: "_ReusableChatConnectionPool",
        key: tuple[str, str, int],
        connection: HTTPConnection | HTTPSConnection,
        reused_connection: bool,
        retried_connection: bool,
    ) -> None:
        self._response = response
        self._pool = pool
        self._key = key
        self._connection = connection
        self.reused_connection = bool(reused_connection)
        self.retried_connection = bool(retried_connection)
        self._healthy = True
        self._closed = False
        self.status = getattr(response, "status", 0)
        self.reason = getattr(response, "reason", "")

    def __enter__(self) -> "_ReusableChatResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self._healthy = False
        self.close()
        return False

    def __iter__(self) -> Iterator[bytes]:
        while True:
            line = self._response.readline()
            if not line:
                break
            yield line

    def read(self) -> bytes:
        return self._response.read()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._healthy:
            self._pool.release(self._key, self._connection)
        else:
            self._pool.discard(self._connection)


class _ReusableChatConnectionPool:
    def __init__(self) -> None:
        self._lock = Lock()
        self._idle: dict[tuple[str, str, int], HTTPConnection | HTTPSConnection] = {}

    def open(self, request: Request, *, timeout: float, retry_once: bool) -> _ReusableChatResponse:
        url = urlsplit(request.full_url)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise URLError("unsupported reusable chat URL")
        key = (url.scheme, url.hostname, int(url.port or (443 if url.scheme == "https" else 80)))
        body = bytes(request.data or b"")
        headers = {name: value for name, value in request.header_items()}
        parsed_path = urlunsplit(("", "", url.path or "/", url.query, ""))
        connection, reused = self._acquire(key, timeout=timeout)
        retried = False
        try:
            response = self._request(connection, method=request.get_method(), path=parsed_path, body=body, headers=headers)
        except OSError:
            self.discard(connection)
            if not retry_once:
                raise
            retried = True
            connection, reused = self._new_connection(key, timeout=timeout), False
            response = self._request(connection, method=request.get_method(), path=parsed_path, body=body, headers=headers)
        if response.status >= 400:
            raw_error = response.read()
            error = HTTPError(request.full_url, response.status, response.reason, response.headers, BytesIO(raw_error))
            self.discard(connection)
            raise error
        return _ReusableChatResponse(
            response=response,
            pool=self,
            key=key,
            connection=connection,
            reused_connection=reused,
            retried_connection=retried,
        )

    def warm_url(self, url_text: str, *, timeout: float) -> dict[str, Any]:
        url = urlsplit(str(url_text or ""))
        if url.scheme not in {"http", "https"} or not url.hostname:
            return {"ok": False, "status": "invalid_url"}
        key = (url.scheme, url.hostname, int(url.port or (443 if url.scheme == "https" else 80)))
        with self._lock:
            if key in self._idle:
                return {"ok": True, "status": "already_warm", "reused": True}
        connection = self._new_connection(key, timeout=timeout)
        try:
            connection.connect()
        except OSError as exc:
            self.discard(connection)
            return {"ok": False, "status": "failed", "error": exc.__class__.__name__}
        self.release(key, connection)
        return {"ok": True, "status": "warmed", "reused": False}

    def _acquire(
        self,
        key: tuple[str, str, int],
        *,
        timeout: float,
    ) -> tuple[HTTPConnection | HTTPSConnection, bool]:
        with self._lock:
            connection = self._idle.pop(key, None)
        if connection is not None:
            connection.timeout = timeout
            return connection, True
        return self._new_connection(key, timeout=timeout), False

    def _new_connection(self, key: tuple[str, str, int], *, timeout: float) -> HTTPConnection | HTTPSConnection:
        scheme, host, port = key
        if scheme == "https":
            return HTTPSConnection(host, port=port, timeout=timeout)
        return HTTPConnection(host, port=port, timeout=timeout)

    def _request(
        self,
        connection: HTTPConnection | HTTPSConnection,
        *,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
    ) -> HTTPResponse:
        request_headers = dict(headers)
        request_headers.setdefault("Connection", "keep-alive")
        connection.request(method, path, body=body, headers=request_headers)
        return connection.getresponse()

    def release(self, key: tuple[str, str, int], connection: HTTPConnection | HTTPSConnection) -> None:
        with self._lock:
            old = self._idle.pop(key, None)
            self._idle[key] = connection
        if old is not None and old is not connection:
            self.discard(old)

    def discard(self, connection: HTTPConnection | HTTPSConnection) -> None:
        try:
            connection.close()
        except OSError:
            pass

    def close_all(self) -> None:
        with self._lock:
            rows = list(self._idle.values())
            self._idle.clear()
        for connection in rows:
            self.discard(connection)


_REUSABLE_CHAT_POOL = _ReusableChatConnectionPool()


def close_direct_llm_http_pool() -> None:
    _REUSABLE_CHAT_POOL.close_all()


def warm_direct_llm_http_pool(config: DirectLlmConfig, *, timeout_seconds: float = 1.5) -> dict[str, Any]:
    if not _direct_llm_http_keepalive_enabled():
        return {"ok": False, "status": "keepalive_disabled"}
    if not _direct_llm_http_warm_enabled():
        return {"ok": False, "status": "warm_disabled"}
    if not str(getattr(config, "api_key", "") or "").strip():
        return {"ok": False, "status": "missing_api_key"}
    chat_url = chat_completions_url(str(getattr(config, "base_url", "") or ""), provider=str(getattr(config, "provider", "") or ""))
    if not chat_url:
        return {"ok": False, "status": "missing_base_url"}
    started = time.monotonic()
    result = _REUSABLE_CHAT_POOL.warm_url(chat_url, timeout=max(0.1, float(timeout_seconds or 1.5)))
    result["latency_ms"] = _latency_ms(started)
    result["endpoint_host"] = urlsplit(chat_url).hostname or ""
    return result


def _open_chat_request(request: Request, *, timeout: float, reusable: bool) -> Any:
    if reusable:
        return _REUSABLE_CHAT_POOL.open(
            request,
            timeout=timeout,
            retry_once=_direct_llm_http_keepalive_retry_enabled(),
        )
    return urlopen(request, timeout=timeout)


def open_pooled_http_request(request: Request, *, timeout: float, retry_once: bool = True) -> Any:
    """Open a request on the shared keepalive pool (reused by StepFun ASR to skip TLS setup)."""
    return _REUSABLE_CHAT_POOL.open(request, timeout=timeout, retry_once=retry_once)


def warm_pooled_http_url(url_text: str, *, timeout_seconds: float = 1.5) -> dict[str, Any]:
    """Pre-open a keepalive connection for the given URL's host (e.g. ASR endpoint)."""
    started = time.monotonic()
    result = _REUSABLE_CHAT_POOL.warm_url(str(url_text or ""), timeout=max(0.1, float(timeout_seconds or 1.5)))
    result["latency_ms"] = _latency_ms(started)
    result["endpoint_host"] = urlsplit(str(url_text or "")).hostname or ""
    return result


def _direct_llm_http_keepalive_enabled() -> bool:
    return str(os.environ.get(DIRECT_LLM_HTTP_KEEPALIVE_ENV, "")).strip().lower() in TRUE_VALUES


def _direct_llm_http_keepalive_retry_enabled() -> bool:
    raw = str(os.environ.get(DIRECT_LLM_HTTP_KEEPALIVE_RETRY_ENV, "1")).strip().lower()
    if not raw:
        return True
    return raw in TRUE_VALUES


def _direct_llm_http_warm_enabled() -> bool:
    raw = str(os.environ.get(DIRECT_LLM_HTTP_WARM_ENV, "1")).strip().lower()
    if not raw:
        return True
    return raw in TRUE_VALUES


def _stream_final(
    *,
    ok: bool,
    status: str,
    response: str,
    request_id: str,
    latency_ms: int,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "type": "final",
        "ok": bool(ok),
        "status": str(status),
        "response": str(response),
        "request_id": str(request_id),
        "latency_ms": int(latency_ms),
        "evidence": dict(evidence),
    }


def chat_completions_url(base_url: str, *, provider: str = "") -> str:
    text = (base_url or _provider_default_base_url(provider)).strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/chat/completions"):
        return text
    if not path:
        path = "/v1/chat/completions"
    elif path.endswith("/v1"):
        path = f"{path}/chat/completions"
    else:
        path = f"{path}/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _provider_default_base_url(provider: str) -> str:
    provider_key = str(provider or "").strip().lower()
    if not provider_key:
        return ""
    try:
        catalog = load_hermes_provider_catalog()
    except (FileNotFoundError, ValueError):
        catalog = ()
    for item in catalog:
        item_id = str(item.get("id") or "").strip().lower()
        aliases = {str(alias).strip().lower() for alias in item.get("aliases") or []}
        if provider_key == item_id or provider_key in aliases:
            return str(item.get("base_url") or "")
    return ""


def _http_error_detail(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _latency_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _int_range(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _float_range(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))
