#!/usr/bin/env python3
"""Batch-generate StepFun Step Plan TTS files.

The script intentionally reads API keys from environment variables only.
It never prints the key or writes it to output files.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request


DEFAULT_BASE_URL = "https://api.stepfun.com/step_plan/v1"
DEFAULT_MODEL = "stepaudio-2.5-tts"
DEFAULT_VOICE = ""
DEFAULT_FORMAT = "pcm"
DEFAULT_SAMPLE_RATE = 24000
MAX_INPUT_CHARS = 1000


@dataclass
class TtsJob:
    id: str
    text: str
    instruction: str = ""
    voice: str = ""
    model: str = ""


def speech_url(base_url: str) -> str:
    text = (base_url or "").strip()
    parsed = parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid base URL: {base_url!r}")
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/audio/speech"):
        return text
    if not path:
        path = "/v1/audio/speech"
    elif path.endswith("/v1"):
        path = f"{path}/audio/speech"
    else:
        path = f"{path}/audio/speech"
    return parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def env_value(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def read_jobs(path: Path) -> list[TtsJob]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return read_jsonl_jobs(path)
    if suffix == ".csv":
        return read_csv_jobs(path)
    return read_text_jobs(path)


def read_jsonl_jobs(path: Path) -> list[TtsJob]:
    jobs: list[TtsJob] = []
    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            text = str(item.get("text") or item.get("input") or "").strip()
            if not text:
                raise ValueError(f"{path}:{index} missing text/input")
            jobs.append(
                TtsJob(
                    id=str(item.get("id") or item.get("name") or f"{index:04d}"),
                    text=text,
                    instruction=str(item.get("instruction") or "").strip(),
                    voice=str(item.get("voice") or "").strip(),
                    model=str(item.get("model") or "").strip(),
                )
            )
    return jobs


def read_csv_jobs(path: Path) -> list[TtsJob]:
    jobs: list[TtsJob] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for index, item in enumerate(reader, start=1):
            text = str(item.get("text") or item.get("input") or "").strip()
            if not text:
                raise ValueError(f"{path}:{index} missing text/input column")
            jobs.append(
                TtsJob(
                    id=str(item.get("id") or item.get("name") or f"{index:04d}"),
                    text=text,
                    instruction=str(item.get("instruction") or "").strip(),
                    voice=str(item.get("voice") or "").strip(),
                    model=str(item.get("model") or "").strip(),
                )
            )
    return jobs


def read_text_jobs(path: Path) -> list[TtsJob]:
    jobs: list[TtsJob] = []
    with path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            text = line.strip()
            if text:
                jobs.append(TtsJob(id=f"{index:04d}", text=text))
    return jobs


def safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    name = name.strip("._-")
    return name or "item"


def write_pcm_wav(path: Path, pcm: bytes, *, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


def build_payload(
    job: TtsJob,
    *,
    model: str,
    voice: str,
    audio_format: str,
    sample_rate: int,
    instruction: str,
    speed: float | None,
    volume: float | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": job.model or model,
        "input": job.text,
        "voice": job.voice or voice,
        "response_format": audio_format,
    }
    if sample_rate:
        payload["sample_rate"] = sample_rate
    effective_instruction = job.instruction or instruction
    if effective_instruction:
        payload["instruction"] = effective_instruction
    if speed is not None:
        payload["speed"] = speed
    if volume is not None:
        payload["volume"] = volume
    return payload


def post_tts(
    endpoint: str,
    api_key: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    retries: int,
) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    last_error = ""
    for attempt in range(retries + 1):
        req = request.Request(endpoint, data=body, method="POST", headers=headers)
        try:
            with request.urlopen(req, timeout=max(1.0, timeout)) as response:
                audio = response.read()
                if not audio:
                    raise RuntimeError("empty audio response")
                return audio
        except error.HTTPError as exc:
            detail = safe_http_error(exc, api_key=api_key)
            last_error = f"HTTP {exc.code}: {detail}"
            if exc.code in {401, 403}:
                raise RuntimeError(last_error) from exc
            if exc.code not in {408, 409, 425, 429, 500, 502, 503, 504}:
                raise RuntimeError(last_error) from exc
        except (OSError, TimeoutError, RuntimeError) as exc:
            last_error = exc.__class__.__name__ if not str(exc) else str(exc)
        if attempt < retries:
            time.sleep(min(8.0, 0.8 * (2**attempt)))
    raise RuntimeError(last_error or "TTS request failed")


def safe_http_error(exc: error.HTTPError, *, api_key: str) -> str:
    try:
        data = exc.read(800)
    except Exception:
        return ""
    text = data.decode("utf-8", errors="replace").strip()
    if api_key:
        text = text.replace(api_key, "[redacted]")
    for name in ("STEP_API_KEY", "STEPFUN_API_KEY"):
        secret = os.environ.get(name, "")
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:500]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch-generate StepFun Step Plan TTS audio.")
    parser.add_argument("--input", required=True, type=Path, help="Input .txt, .jsonl, or .csv file.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for generated audio.")
    parser.add_argument("--base-url", default=env_value("STEPFUN_TTS_BASE_URL", default=DEFAULT_BASE_URL))
    parser.add_argument("--model", default=env_value("STEPFUN_TTS_MODEL", default=DEFAULT_MODEL))
    parser.add_argument("--voice", default=env_value("STEPFUN_TTS_VOICE", default=DEFAULT_VOICE))
    parser.add_argument("--api-key-env", default="STEP_API_KEY", help="API key env var name. Falls back to STEPFUN_API_KEY.")
    parser.add_argument("--format", default=env_value("STEPFUN_TTS_FORMAT", default=DEFAULT_FORMAT), choices=["pcm", "wav", "mp3", "flac", "opus"])
    parser.add_argument("--sample-rate", default=int(env_value("STEPFUN_TTS_SAMPLE_RATE", default=str(DEFAULT_SAMPLE_RATE))), type=int)
    parser.add_argument("--instruction", default=env_value("STEPFUN_TTS_INSTRUCTION", default=""))
    parser.add_argument("--speed", type=float, default=None)
    parser.add_argument("--volume", type=float, default=None)
    parser.add_argument("--timeout", type=float, default=float(env_value("STEPFUN_TTS_TIMEOUT", default="30")))
    parser.add_argument("--retries", type=int, default=int(env_value("STEPFUN_TTS_RETRIES", default="2")))
    parser.add_argument("--raw-pcm", action="store_true", help="When --format pcm, save .pcm instead of wrapping as .wav.")
    parser.add_argument("--dry-run", action="store_true", help="Validate inputs and print planned jobs without calling StepFun.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    api_key = env_value(args.api_key_env, "STEPFUN_API_KEY")
    if not api_key and not args.dry_run:
        print(f"Missing API key. Set {args.api_key_env} or STEPFUN_API_KEY.", file=sys.stderr)
        return 2

    jobs = read_jobs(args.input)
    if not jobs:
        print("No jobs found.", file=sys.stderr)
        return 2

    too_long = [job for job in jobs if len(job.text) > MAX_INPUT_CHARS]
    if too_long:
        names = ", ".join(job.id for job in too_long[:5])
        print(f"{len(too_long)} job(s) exceed {MAX_INPUT_CHARS} characters: {names}", file=sys.stderr)
        return 2

    endpoint = speech_url(args.base_url)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    print(f"endpoint={endpoint}")
    print(f"model={args.model} voice={args.voice} format={args.format} sample_rate={args.sample_rate}")
    print(f"jobs={len(jobs)} output_dir={args.output_dir}")

    if args.dry_run:
        for job in jobs:
            print(f"DRY {job.id}: {job.text[:48]}")
        return 0

    for index, job in enumerate(jobs, start=1):
        payload = build_payload(
            job,
            model=args.model,
            voice=args.voice,
            audio_format=args.format,
            sample_rate=args.sample_rate,
            instruction=args.instruction,
            speed=args.speed,
            volume=args.volume,
        )
        audio = post_tts(endpoint, api_key, payload, timeout=args.timeout, retries=args.retries)
        stem = f"{index:04d}_{safe_name(job.id)}"
        if args.format == "pcm" and not args.raw_pcm:
            out_path = args.output_dir / f"{stem}.wav"
            write_pcm_wav(out_path, audio, sample_rate=args.sample_rate)
        else:
            suffix = "pcm" if args.format == "pcm" else args.format
            out_path = args.output_dir / f"{stem}.{suffix}"
            out_path.write_bytes(audio)
        print(f"OK {index}/{len(jobs)} {out_path} bytes={out_path.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
