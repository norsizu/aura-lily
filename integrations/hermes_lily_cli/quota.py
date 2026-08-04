"""Best-effort provider quota queries for the private admin panel.

The browser only receives normalized, non-secret results. Providers that expose
no public usage endpoint are reported as unavailable instead of showing a
misleading zero. ``AURA_LILY_QUOTA_ENDPOINTS`` can add provider-specific
endpoints without putting provider credentials in the repository.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STEPFUN_PLAN_QUOTA_ENDPOINT = (
    os.environ.get(
        "AURA_STEPFUN_PLAN_QUOTA_ENDPOINT",
        "https://platform.stepfun.com/api/step.openapi.devcenter.Dashboard/QueryStepPlanRateLimit",
    ).strip()
    or "https://platform.stepfun.com/api/step.openapi.devcenter.Dashboard/QueryStepPlanRateLimit"
)


def query_quota(*, provider: str, model: str, base_url: str, api_key: str) -> dict[str, Any]:
    provider_text = str(provider or "").strip()
    if "stepfun" in f"{provider_text} {base_url}".lower() and "step_plan" in str(base_url or "").lower():
        return _query_stepfun_plan(provider_text, model)
    if not str(api_key or "").strip():
        return _unavailable(provider_text, model, "API Key 未配置")
    endpoint = _endpoint_for(provider_text, base_url)
    if not endpoint:
        return _unavailable(provider_text, model, "该供应商未提供可用额度查询接口")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
    try:
        request = Request(endpoint, headers=headers, method="GET")
        with urlopen(request, timeout=8) as response:
            raw = response.read(256 * 1024)
            status = int(getattr(response, "status", 200) or 200)
    except HTTPError as exc:
        return _unavailable(provider_text, model, f"额度查询失败（HTTP {exc.code}）")
    except (OSError, URLError, TimeoutError):
        return _unavailable(provider_text, model, "额度查询网络超时或不可达")
    if status < 200 or status >= 300:
        return _unavailable(provider_text, model, f"额度查询失败（HTTP {status}）")
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (TypeError, ValueError):
        return _unavailable(provider_text, model, "额度接口返回格式无法识别")
    result = _normalize(provider_text, model, payload)
    result["endpoint"] = endpoint.split("?", 1)[0]
    return result


def _query_stepfun_plan(provider: str, model: str) -> dict[str, Any]:
    """Query Step Plan's dashboard rate-limit view, never its cash balance.

    StepFun does not expose this subscription window through the API-key
    ``/v1/accounts`` endpoint. The dashboard RPC requires an ``Oasis-Token``
    (and, for some deployments, the browser session cookie), so report the
    missing session explicitly instead of presenting the unrelated account
    balance as a plan quota.
    """
    session_token = str(
        os.environ.get("AURA_STEPFUN_PLAN_SESSION_TOKEN")
        or os.environ.get("STEPFUN_PLAN_SESSION_TOKEN")
        or ""
    ).strip()
    session_cookie = str(
        os.environ.get("AURA_STEPFUN_PLAN_SESSION_COOKIE")
        or os.environ.get("STEPFUN_PLAN_SESSION_COOKIE")
        or ""
    ).strip()
    session_webid = str(
        os.environ.get("AURA_STEPFUN_PLAN_WEBID")
        or os.environ.get("STEPFUN_PLAN_WEBID")
        or ""
    ).strip()
    if not session_token and not session_cookie:
        return _unavailable(
            provider,
            model,
            "Step Plan 额度需要 StepFun 平台会话令牌；API Key 只能查询账户余额",
        )

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    # Match the platform web client. The dashboard RPC is authenticated by
    # the short-lived Oasis session, not by the model API key.
    headers["Oasis-Appid"] = (
        str(os.environ.get("AURA_STEPFUN_PLAN_APPID") or "10300").strip() or "10300"
    )
    headers["Oasis-Platform"] = (
        str(os.environ.get("AURA_STEPFUN_PLAN_PLATFORM") or "web").strip() or "web"
    )
    language = str(os.environ.get("AURA_STEPFUN_PLAN_LANGUAGE") or "").strip()
    if language:
        headers["Oasis-Language"] = language
    if session_token:
        headers["Oasis-Token"] = session_token
    if session_webid:
        headers["Oasis-Webid"] = session_webid
    if session_cookie:
        headers["Cookie"] = session_cookie
    try:
        request = Request(STEPFUN_PLAN_QUOTA_ENDPOINT, headers=headers, data=b"{}", method="POST")
        with urlopen(request, timeout=8) as response:
            raw = response.read(256 * 1024)
            status = int(getattr(response, "status", 200) or 200)
    except HTTPError as exc:
        return _unavailable(provider, model, f"Step Plan 额度查询失败（HTTP {exc.code}）")
    except (OSError, URLError, TimeoutError):
        return _unavailable(provider, model, "Step Plan 额度查询网络超时或不可达")
    if status < 200 or status >= 300:
        return _unavailable(provider, model, f"Step Plan 额度查询失败（HTTP {status}）")
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (TypeError, ValueError):
        return _unavailable(provider, model, "Step Plan 额度接口返回格式无法识别")
    result = _normalize_stepfun_plan(provider, model, payload)
    result["endpoint"] = STEPFUN_PLAN_QUOTA_ENDPOINT.split("?", 1)[0]
    return result


def _endpoint_for(provider: str, base_url: str) -> str:
    lower = f"{provider} {base_url}".lower()
    if "stepfun" in lower:
        if "step_plan" in str(base_url or "").lower():
            return STEPFUN_PLAN_QUOTA_ENDPOINT
        # The open-platform endpoint is an account cash balance, not a plan quota.
        return "https://api.stepfun.com/v1/accounts"
    if "kimi" in lower and ("coding" in lower or "kimi-for-coding" in lower):
        return "https://api.kimi.com/coding/v1/usages"
    if "minimax" in lower:
        domain = "api.minimaxi.com" if "minimaxi.com" in lower or "-cn" in lower else "api.minimax.io"
        return f"https://{domain}/v1/api/openplatform/coding_plan/remains"
    raw = str(os.environ.get("AURA_LILY_QUOTA_ENDPOINTS") or "").strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    item = payload.get(provider) or payload.get(str(base_url or ""))
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("url") or "").strip()
    return ""


def _normalize(provider: str, model: str, payload: Any) -> dict[str, Any]:
    provider_key = provider.lower()
    if "kimi" in provider_key and isinstance(payload, dict) and (
        isinstance(payload.get("limits"), list) or isinstance(payload.get("usage"), dict)
    ):
        windows = _normalize_kimi(payload)
    elif "minimax" in provider_key and isinstance(payload, dict) and isinstance(payload.get("model_remains"), list):
        windows = _normalize_minimax(payload)
    else:
        windows = []
    if not windows:
        _collect_windows(payload, windows)
    if not windows:
        balance = _number(payload, "balance")
        if balance is not None:
            windows.append({"name": "balance", "remaining": balance, "unit": "CNY"})
    priority = {"five_hour": 0, "5h": 0, "weekly_limit": 1, "weekly": 1, "monthly": 2, "balance": 3}
    windows.sort(key=lambda row: priority.get(str(row.get("name") or "").lower(), 9))
    for row in windows:
        row["display"] = _display(row)
    primary = windows[0] if windows else None
    return {
        "ok": bool(primary),
        "provider": provider,
        "model": model,
        "checked_at": int(time.time()),
        "windows": windows,
        "primary": primary,
        "display": _display(primary) if primary else "未返回可识别额度",
        "error": "" if primary else "额度接口未返回可识别窗口",
    }


def _normalize_stepfun_plan(provider: str, model: str, payload: Any) -> dict[str, Any]:
    """Normalize old rate windows and the current Credit-pool response."""
    source = payload if isinstance(payload, dict) else {}
    if isinstance(source.get("data"), dict):
        source = source["data"]
    windows: list[dict[str, Any]] = []
    _append_rate_window(
        windows,
        name="five_hour",
        value=_field(source, "five_hour_usage_left_rate", "fiveHourUsageLeftRate"),
        reset_at=_field(source, "five_hour_usage_reset_time", "fiveHourUsageResetTime"),
    )
    _append_rate_window(
        windows,
        name="weekly_limit",
        value=_field(source, "weekly_usage_left_rate", "weeklyUsageLeftRate"),
        reset_at=_field(source, "weekly_usage_reset_time", "weeklyUsageResetTime"),
    )
    # Current Step Plan responses expose a monthly subscription Credit pool.
    # Prefer it over legacy five-hour/week fields whenever it is present.
    credit = _field(source, "plan_credit_rate_limit", "planCreditRateLimit", "credit_rate_limit", "creditRateLimit")
    credit_rate = _field(credit, "subscription_credit_left_rate", "subscriptionCreditLeftRate") if isinstance(credit, dict) else None
    credit_buckets = _field(credit, "credit_buckets", "creditBuckets") if isinstance(credit, dict) else None
    if isinstance(credit, dict) and (credit_rate is not None or isinstance(credit_buckets, list)):
        windows = []
        _append_rate_window(
            windows,
            name="plan_credit",
            value=_field(credit, "subscription_credit_left_rate", "subscriptionCreditLeftRate"),
            reset_at=_field(credit, "subscription_credit_reset_time", "subscriptionCreditResetTime"),
        )
        buckets = credit_buckets
        if isinstance(buckets, list):
            for bucket in buckets:
                if not isinstance(bucket, dict):
                    continue
                total = _number_any(bucket, "credit_total", "creditTotal")
                remaining = _number_any(bucket, "credit_residual", "creditResidual")
                if total is None or total <= 0 or remaining is None:
                    continue
                windows.append({
                    "name": "topup",
                    "remaining": max(0.0, remaining),
                    "total": total,
                    "used": max(0.0, total - remaining),
                    "reset_at": _reset_value(_field(bucket, "expire_at", "expireAt", "next_reset_at", "nextResetAt")),
                    "unit": "Credit",
                })
    priority = {"five_hour": 0, "weekly_limit": 1, "plan_credit": 2}
    windows.sort(key=lambda row: priority.get(str(row.get("name")), 9))
    for row in windows:
        row["display"] = _display(row)
    primary = windows[0] if windows else None
    secondary = windows[1] if len(windows) > 1 else None
    return {
        "ok": bool(primary),
        "provider": provider,
        "model": model,
        "plan": "step_plan",
        "checked_at": int(time.time()),
        "windows": windows,
        "primary": primary,
        "secondary": secondary,
        "display": _display(primary) if primary else "未返回可识别 Step Plan 额度",
        "error": "" if primary else "Step Plan 额度接口未返回可识别窗口",
    }


def _append_rate_window(
    windows: list[dict[str, Any]], *, name: str, value: Any, reset_at: Any,
) -> None:
    rate = _number_value(value)
    if rate is None:
        return
    # Dashboard protobuf fields are rates (0..1), while some older responses
    # used percentage points (0..100).
    remaining = rate * 100.0 if 0.0 <= rate <= 1.0 else rate
    remaining = max(0.0, min(100.0, remaining))
    windows.append({
        "name": name,
        "remaining": remaining,
        "total": 100.0,
        "used": max(0.0, 100.0 - remaining),
        "reset_at": _reset_value(reset_at),
        "unit": "%",
    })


def _normalize_kimi(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Map Kimi Coding Plan's camelCase windows into the shared shape."""
    windows: list[dict[str, Any]] = []
    limits = payload.get("limits") if isinstance(payload.get("limits"), list) else []
    for item in limits:
        if not isinstance(item, dict) or not isinstance(item.get("detail"), dict):
            continue
        detail = item["detail"]
        total = _number(detail, "limit")
        remaining = _number(detail, "remaining")
        if total is None and remaining is None:
            continue
        windows.append({
            "name": "five_hour",
            "remaining": remaining,
            "total": total,
            "used": max(0.0, total - remaining) if total is not None and remaining is not None else None,
            "reset_at": _reset_value(detail.get("resetTime")),
            "unit": "tokens",
        })
    usage = payload.get("usage")
    if isinstance(usage, dict):
        total = _number(usage, "limit")
        remaining = _number(usage, "remaining")
        if total is not None or remaining is not None:
            windows.append({
                "name": "weekly_limit",
                "remaining": remaining,
                "total": total,
                "used": max(0.0, total - remaining) if total is not None and remaining is not None else None,
                "reset_at": _reset_value(usage.get("resetTime")),
                "unit": "tokens",
            })
    return windows


def _normalize_minimax(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Map MiniMax Coding Plan's remaining percentages into quota windows."""
    model_remains = payload.get("model_remains") if isinstance(payload.get("model_remains"), list) else []
    item = next((row for row in model_remains if isinstance(row, dict) and row.get("model_name") == "general"), None)
    if not item:
        return []
    windows: list[dict[str, Any]] = []
    remaining = _number(item, "current_interval_remaining_percent")
    if remaining is not None:
        windows.append({
            "name": "five_hour",
            "remaining": max(0.0, min(100.0, remaining)),
            "total": 100.0,
            "unit": "%",
            "reset_at": _reset_value(item.get("end_time")),
        })
    weekly_status = _number(item, "current_weekly_status")
    weekly = _number(item, "current_weekly_remaining_percent")
    if weekly_status == 1 and weekly is not None:
        windows.append({
            "name": "weekly_limit",
            "remaining": max(0.0, min(100.0, weekly)),
            "total": 100.0,
            "unit": "%",
            "reset_at": _reset_value(item.get("weekly_end_time")),
        })
    return windows


def _reset_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return ""
        # Provider timestamps are milliseconds; accept seconds defensively.
        return str(int(number * 1000 if number < 1_000_000_000_000 else number))
    return str(value).strip()


def _collect_windows(value: Any, out: list[dict[str, Any]], name_hint: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower().replace("-", "_")
            if key_text in {"five_hour", "5h", "fivehour", "weekly", "weekly_limit", "seven_day", "monthly"}:
                row = _window_from_value(key_text, item)
                if row:
                    out.append(row)
            elif key_text in {"remaining", "balance", "total_balance"} and isinstance(item, (int, float)):
                if not any(str(row.get("name")) == "balance" for row in out):
                    out.append({"name": "balance", "remaining": float(item), "unit": "CNY"})
            else:
                _collect_windows(item, out, key_text)
    elif isinstance(value, list):
        for item in value:
            _collect_windows(item, out, name_hint)


def _window_from_value(name: str, value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        remaining = _number(value, "remaining")
        if remaining is None:
            remaining = _number(value, "remain")
        total = _number(value, "total") or _number(value, "limit")
        used = _number(value, "used")
        reset = str(
            value.get("reset_at")
            or value.get("reset_time")
            or value.get("resetAt")
            or value.get("resetTime")
            or ""
        ).strip()
    else:
        remaining = float(value) if isinstance(value, (int, float)) else None
        total = used = None
        reset = ""
    if remaining is None and total is None and used is None:
        return None
    if remaining is None and total is not None and used is not None:
        remaining = max(0.0, total - used)
    return {"name": "weekly_limit" if name in {"weekly", "seven_day"} else "five_hour" if name in {"5h", "fivehour"} else name, "remaining": remaining, "total": total, "used": used, "reset_at": reset}


def _number(value: Any, key: str) -> float | None:
    if not isinstance(value, dict):
        return None
    raw = value.get(key)
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _field(value: Any, *keys: str) -> Any:
    if not isinstance(value, dict):
        return None
    for key in keys:
        if key in value and value[key] is not None:
            return value[key]
    return None


def _number_any(value: Any, *keys: str) -> float | None:
    return _number_value(_field(value, *keys))


def _number_value(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _display(row: dict[str, Any] | None) -> str:
    if not row:
        return "未返回可识别额度"
    name = {"five_hour": "5 小时", "weekly_limit": "周额度", "monthly": "月额度", "plan_credit": "Credit", "topup": "加油包", "balance": "账户余额"}.get(str(row.get("name")), str(row.get("name")))
    remaining = row.get("remaining")
    unit = str(row.get("unit") or "").strip()
    if remaining is None:
        return name
    value = f"{remaining:.2f}".rstrip("0").rstrip(".")
    total = row.get("total")
    if total is not None:
        total_text = f"{float(total):.2f}".rstrip("0").rstrip(".")
        suffix = f" {unit}" if unit else ""
        return f"{name} {value}/{total_text}{suffix}"
    suffix = f" {unit}" if unit else ""
    return f"{name} {value}{suffix}"


def _unavailable(provider: str, model: str, error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "provider": provider,
        "model": model,
        "checked_at": int(time.time()),
        "windows": [],
        "primary": None,
        "secondary": None,
        "display": "不可用",
        "error": error,
    }
