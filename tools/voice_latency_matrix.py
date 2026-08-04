#!/usr/bin/env python3
"""Run a small latency matrix over the existing voice benchmark.

This wrapper deliberately shells out to ``voice_latency_benchmark.py`` for each
profile so gateway module constants are reloaded from that profile's environment.
It is meant for manual provider runs, not CI.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "tools" / "voice_latency_benchmark.py"


@dataclass(frozen=True)
class Profile:
    name: str
    env: dict[str, str]
    note: str


PROFILES: dict[str, Profile] = {
    "baseline": Profile(
        name="baseline",
        env={
            "AURA_TTS_STEPFUN_WS_WARM_ENABLED": "1",
            "AURA_BRIDGE_STREAM_FIRST_SEGMENT_CHARS": "6",
            "AURA_TTS_WS_CHUNK_BYTES": "2048",
            "AURA_TTS_AUDIO_SEND_PACING_ENABLED": "1",
        },
        note="current tuned baseline: warm WS, 6-char first segment, 2048-byte audio chunks",
    ),
    "first4": Profile(
        name="first4",
        env={
            "AURA_TTS_STEPFUN_WS_WARM_ENABLED": "1",
            "AURA_BRIDGE_STREAM_FIRST_SEGMENT_CHARS": "4",
            "AURA_TTS_WS_CHUNK_BYTES": "2048",
            "AURA_TTS_AUDIO_SEND_PACING_ENABLED": "1",
        },
        note="more aggressive first TTS text boundary",
    ),
    "first8": Profile(
        name="first8",
        env={
            "AURA_TTS_STEPFUN_WS_WARM_ENABLED": "1",
            "AURA_BRIDGE_STREAM_FIRST_SEGMENT_CHARS": "8",
            "AURA_TTS_WS_CHUNK_BYTES": "2048",
            "AURA_TTS_AUDIO_SEND_PACING_ENABLED": "1",
        },
        note="slightly more complete first phrase before TTS",
    ),
    "chunk1024": Profile(
        name="chunk1024",
        env={
            "AURA_TTS_STEPFUN_WS_WARM_ENABLED": "1",
            "AURA_BRIDGE_STREAM_FIRST_SEGMENT_CHARS": "6",
            "AURA_TTS_WS_CHUNK_BYTES": "1024",
            "AURA_TTS_AUDIO_SEND_PACING_ENABLED": "1",
        },
        note="smaller device audio frames; can reduce burst size but may raise frame count",
    ),
    "chunk4096": Profile(
        name="chunk4096",
        env={
            "AURA_TTS_STEPFUN_WS_WARM_ENABLED": "1",
            "AURA_BRIDGE_STREAM_FIRST_SEGMENT_CHARS": "6",
            "AURA_TTS_WS_CHUNK_BYTES": "4096",
            "AURA_TTS_AUDIO_SEND_PACING_ENABLED": "1",
        },
        note="larger device audio frames; useful as a control for gap/stall behavior",
    ),
    "pacing-off": Profile(
        name="pacing-off",
        env={
            "AURA_TTS_STEPFUN_WS_WARM_ENABLED": "1",
            "AURA_BRIDGE_STREAM_FIRST_SEGMENT_CHARS": "6",
            "AURA_TTS_WS_CHUNK_BYTES": "2048",
            "AURA_TTS_AUDIO_SEND_PACING_ENABLED": "0",
        },
        note="control run; pacing often has zero sleep in current logs",
    ),
    "warm-off": Profile(
        name="warm-off",
        env={
            "AURA_TTS_STEPFUN_WS_WARM_ENABLED": "0",
            "AURA_BRIDGE_STREAM_FIRST_SEGMENT_CHARS": "6",
            "AURA_TTS_WS_CHUNK_BYTES": "2048",
            "AURA_TTS_AUDIO_SEND_PACING_ENABLED": "1",
        },
        note="control run to prove warm session value",
    ),
}

DEFAULT_PROFILES = ("baseline", "first4", "first8")
SUMMARY_METRICS = (
    "first_audio_sent_ms",
    "first_audio_after_stop_ms",
    "turn_to_tts_first_audio_ms",
    "tts_first_text_ms",
    "tts_first_audio_since_bridge_ms",
    "tts_first_text_to_audio_ms",
    "tts_audio_chunk_gap_p95_ms",
    "tts_audio_chunk_gap_max_ms",
    "tts_audio_chunk_stall_count",
    "tts_audio_send_pacing_sleep_ms",
    "total_ms",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run repeated Aura Lily voice latency benchmark profiles.")
    parser.add_argument("--profiles", default=",".join(DEFAULT_PROFILES), help="Comma separated profile names, or 'all'.")
    parser.add_argument("--iterations", type=int, default=3, help="Runs per profile. Manual provider runs may incur cost.")
    parser.add_argument("--mode", choices=["voice-sim", "voice-ws"], default="voice-sim")
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8765/turn")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8787/ws")
    parser.add_argument("--text", default="测试一下，简单回应我一句。")
    parser.add_argument("--audio-ms", type=int, default=1200)
    parser.add_argument("--frame-ms", type=int, default=40)
    parser.add_argument("--audio-format", choices=["pcm", "opus"], default="pcm")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--user-city", default="")
    parser.add_argument("--user-timezone", default="")
    parser.add_argument("--user-latitude", default="")
    parser.add_argument("--user-longitude", default="")
    parser.add_argument("--fake-streaming-asr-speech-stop", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only.")
    parser.add_argument("--keep-going", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args(argv)


def selected_profiles(value: str) -> list[Profile]:
    names = list(PROFILES) if str(value or "").strip().lower() == "all" else [
        item.strip() for item in str(value or "").split(",") if item.strip()
    ]
    if not names:
        names = list(DEFAULT_PROFILES)
    unknown = [name for name in names if name not in PROFILES]
    if unknown:
        raise SystemExit(f"unknown profile(s): {', '.join(unknown)}")
    return [PROFILES[name] for name in names]


def benchmark_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(BENCHMARK),
        "--mode",
        args.mode,
        "--text",
        args.text,
        "--audio-ms",
        str(args.audio_ms),
        "--timeout",
        str(args.timeout),
        "--json",
    ]
    if args.mode == "voice-sim":
        command.extend(["--bridge-url", args.bridge_url])
        if args.fake_streaming_asr_speech_stop:
            command.append("--fake-streaming-asr-speech-stop")
        else:
            command.append("--no-fake-streaming-asr-speech-stop")
    else:
        command.extend([
            "--ws-url",
            args.ws_url,
            "--frame-ms",
            str(args.frame_ms),
            "--audio-format",
            args.audio_format,
        ])
    for arg_name, cli_name in (
        ("user_city", "--user-city"),
        ("user_timezone", "--user-timezone"),
        ("user_latitude", "--user-latitude"),
        ("user_longitude", "--user-longitude"),
    ):
        value = str(getattr(args, arg_name, "") or "").strip()
        if value:
            command.extend([cli_name, value])
    return command


def run_profile_once(profile: Profile, args: argparse.Namespace, iteration: int) -> dict[str, Any]:
    env = os.environ.copy()
    env.update(profile.env)
    env.setdefault("PYTHONPATH", ".")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env.setdefault("AURA_TTS_PREFACE_ENABLED", "0")
    started = time.monotonic()
    result = subprocess.run(
        benchmark_command(args),
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=max(5.0, float(args.timeout) + 15.0),
    )
    elapsed = int((time.monotonic() - started) * 1000)
    payload = parse_benchmark_payload(result.stdout)
    payload.update({
        "profile": profile.name,
        "profile_note": profile.note,
        "profile_env": dict(profile.env),
        "profile_env_applies_to": profile_env_applies_to(args.mode),
        "iteration": iteration,
        "process_ms": elapsed,
        "exit_code": result.returncode,
    })
    if result.returncode != 0:
        payload["ok"] = False
        payload["error"] = "benchmark_failed"
        payload["stderr_tail"] = tail_text(result.stderr)
    return payload


def parse_benchmark_payload(stdout: str) -> dict[str, Any]:
    payloads: list[dict[str, Any]] = []
    for line in str(stdout or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            payloads.append(item)
    results = [item for item in payloads if item.get("type") != "summary"]
    if results:
        return dict(results[-1])
    if payloads:
        return dict(payloads[-1])
    return {"ok": False, "error": "no_json_payload"}


def profile_env_applies_to(mode: str) -> str:
    if mode == "voice-ws":
        return "benchmark_client_only_existing_gateway_container_keeps_its_startup_env"
    return "benchmark_process"


def tail_text(value: str, *, limit: int = 1200) -> str:
    text = str(value or "").strip()
    return text[-limit:] if len(text) > limit else text


def metric_value(result: dict[str, Any], key: str) -> int:
    if key in result:
        return max(0, int(result.get(key) or 0))
    timing = result.get("timing") if isinstance(result.get("timing"), dict) else {}
    return max(0, int(timing.get(key) or 0))


def percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(max(0, int(value)) for value in values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * pct + 0.999999) - 1))
    return ordered[index]


def summarize_profile(profile: Profile, runs: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "profile": profile.name,
        "note": profile.note,
        "env": dict(profile.env),
        "runs": len(runs),
        "ok": sum(1 for run in runs if run.get("ok")),
        "metrics": {},
    }
    for metric in SUMMARY_METRICS:
        values = [metric_value(run, metric) for run in runs if metric_value(run, metric)]
        if not values:
            continue
        summary["metrics"][metric] = {
            "p50": percentile(values, 0.50),
            "p95": percentile(values, 0.95),
            "max": max(values),
        }
    return summary


def summarize_matrix(profiles: list[Profile], runs: list[dict[str, Any]]) -> dict[str, Any]:
    by_profile = {
        profile.name: [run for run in runs if run.get("profile") == profile.name]
        for profile in profiles
    }
    return {
        "type": "matrix_summary",
        "runs": len(runs),
        "ok": sum(1 for run in runs if run.get("ok")),
        "profiles": [summarize_profile(profile, by_profile.get(profile.name, [])) for profile in profiles],
    }


def print_human_summary(summary: dict[str, Any]) -> None:
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("profile summary:")
    for profile in summary.get("profiles", []):
        metrics = profile.get("metrics") if isinstance(profile.get("metrics"), dict) else {}
        first_audio = metrics.get("first_audio_sent_ms") or {}
        text_to_audio = metrics.get("tts_first_text_to_audio_ms") or {}
        gap = metrics.get("tts_audio_chunk_gap_max_ms") or {}
        stalls = metrics.get("tts_audio_chunk_stall_count") or {}
        print(
            "- {name}: ok={ok}/{runs} first_audio_p50={fa50}ms first_audio_p95={fa95}ms "
            "tts_text_to_audio_p50={tta50}ms gap_max_p95={gap95}ms stalls_p95={stalls95}".format(
                name=profile.get("profile"),
                ok=profile.get("ok"),
                runs=profile.get("runs"),
                fa50=first_audio.get("p50", 0),
                fa95=first_audio.get("p95", 0),
                tta50=text_to_audio.get("p50", 0),
                gap95=gap.get("p95", 0),
                stalls95=stalls.get("p95", 0),
            )
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    profiles = selected_profiles(args.profiles)
    all_runs: list[dict[str, Any]] = []
    for profile in profiles:
        for iteration in range(1, max(1, int(args.iterations or 1)) + 1):
            try:
                result = run_profile_once(profile, args, iteration)
            except subprocess.TimeoutExpired as exc:
                result = {
                    "ok": False,
                    "profile": profile.name,
                    "profile_note": profile.note,
                    "profile_env": dict(profile.env),
                    "iteration": iteration,
                    "error": "timeout",
                    "stdout_tail": tail_text(exc.stdout or ""),
                    "stderr_tail": tail_text(exc.stderr or ""),
                }
            all_runs.append(result)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            elif not result.get("ok"):
                print(f"{profile.name}#{iteration}: failed {result.get('error') or result.get('status')}")
            else:
                print(
                    "{profile}#{iteration}: first_audio={first_audio}ms text_to_audio={text_to_audio}ms "
                    "gap_max={gap_max}ms".format(
                        profile=profile.name,
                        iteration=iteration,
                        first_audio=metric_value(result, "first_audio_sent_ms"),
                        text_to_audio=metric_value(result, "tts_first_text_to_audio_ms"),
                        gap_max=metric_value(result, "tts_audio_chunk_gap_max_ms"),
                    )
                )
            if not result.get("ok") and not args.keep_going:
                break
    summary = summarize_matrix(profiles, all_runs)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    else:
        print_human_summary(summary)
    return 0 if summary["ok"] == summary["runs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
