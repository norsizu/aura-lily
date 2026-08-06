#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_MODEL_PATHS = [
    "/opt/homebrew/Cellar/whisper-cpp/1.8.4/share/whisper-cpp/for-tests-ggml-tiny.bin",
    "/opt/homebrew/share/whisper-cpp/for-tests-ggml-tiny.bin",
]


def find_whisper_cli() -> str:
    configured = os.environ.get("AURA_WHISPER_CLI", "").strip()
    if configured:
        return configured
    return shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"


def find_model_path() -> str:
    configured = os.environ.get("AURA_WHISPER_MODEL", "").strip()
    if configured:
        return configured
    for candidate in DEFAULT_MODEL_PATHS:
        if Path(candidate).exists():
            return candidate
    return ""


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], bytes, str]:
    raw = (
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("utf-8") + body
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not message.is_multipart():
        return {}, body, "turn.wav"

    fields: dict[str, str] = {}
    file_bytes = b""
    filename = "turn.wav"
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition") or ""
        payload = part.get_payload(decode=True) or b""
        if part.get_filename():
            file_bytes = payload
            filename = Path(part.get_filename() or filename).name or filename
        elif name:
            fields[name] = payload.decode("utf-8", errors="replace").strip()
    return fields, file_bytes, filename


def extract_transcript(stdout: str) -> str:
    lines: list[str] = []
    for line in stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("[") and "]" in text:
            text = text.split("]", 1)[1].strip()
        if text:
            lines.append(text)
    return " ".join(lines).strip()


def log_asr_result(payload: dict[str, Any]) -> None:
    print("local-asr-result " + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


class LocalWhisperHandler(BaseHTTPRequestHandler):
    server_version = "AuraLocalWhisperASR/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"local-asr: {self.address_string()} {fmt % args}", flush=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/health", "/v1/health"}:
            model_path = find_model_path()
            json_response(self, 200, {
                "ok": bool(find_whisper_cli() and model_path),
                "service": "aura-local-whisper-asr",
                "whisper_cli": find_whisper_cli(),
                "model_configured": bool(model_path),
                "model_name": Path(model_path).name if model_path else "",
            })
            return
        json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/v1/audio/transcriptions", "/audio/transcriptions"}:
            json_response(self, 404, {"ok": False, "error": "not_found"})
            return

        max_bytes = int(os.environ.get("AURA_LOCAL_ASR_MAX_BYTES", str(16 * 1024 * 1024)))
        content_length = int(self.headers.get("content-length") or "0")
        if content_length <= 0 or content_length > max_bytes:
            json_response(self, 413, {"ok": False, "error": "invalid_audio_size"})
            return

        whisper_cli = find_whisper_cli()
        model_path = find_model_path()
        if not Path(whisper_cli).exists() and not shutil.which(whisper_cli):
            json_response(self, 503, {"ok": False, "error": "whisper_cli_missing"})
            return
        if not model_path or not Path(model_path).exists():
            json_response(self, 503, {"ok": False, "error": "whisper_model_missing"})
            return

        body = self.rfile.read(content_length)
        fields, audio, filename = parse_multipart(self.headers.get("content-type", ""), body)
        if not audio:
            json_response(self, 400, {"ok": False, "error": "missing_audio_file"})
            return

        language = fields.get("language") or os.environ.get("AURA_WHISPER_LANGUAGE", "zh")
        timeout = float(os.environ.get("AURA_WHISPER_TIMEOUT_SECONDS", "60"))
        suffix = Path(filename).suffix if Path(filename).suffix else ".wav"
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="aura-local-asr-") as tmp:
            audio_path = Path(tmp) / f"turn{suffix}"
            audio_path.write_bytes(audio)
            command = [
                whisper_cli,
                "-m", model_path,
                "-f", str(audio_path),
                "-l", language,
                "-nt",
                "-np",
            ]
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        latency_ms = int((time.perf_counter() - started) * 1000)

        transcript = extract_transcript(completed.stdout)
        if completed.returncode != 0:
            log_asr_result({
                "ok": False,
                "model": Path(model_path).name,
                "language": language,
                "audio_bytes": len(audio),
                "latency_ms": latency_ms,
                "error": "whisper_failed",
            })
            json_response(self, 502, {
                "ok": False,
                "error": "whisper_failed",
                "detail": (completed.stderr or completed.stdout or "").strip()[-500:],
            })
            return
        log_asr_result({
            "ok": True,
            "model": Path(model_path).name,
            "language": language,
            "audio_bytes": len(audio),
            "latency_ms": latency_ms,
            "text_chars": len(transcript),
            "text": transcript[:200],
        })
        json_response(self, 200, {
            "text": transcript,
            "language": language,
            "duration": 0,
            "segments": [],
        })


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAI-compatible local ASR bridge for Aura Lily.")
    parser.add_argument("--host", default=os.environ.get("AURA_LOCAL_ASR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AURA_LOCAL_ASR_PORT", "8766")))
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), LocalWhisperHandler)
    print(
        f"local-asr listening on http://{args.host}:{args.port}/v1/audio/transcriptions "
        f"(model={Path(find_model_path()).name if find_model_path() else 'missing'})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
