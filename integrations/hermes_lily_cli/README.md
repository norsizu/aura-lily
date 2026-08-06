# Aura Lily Hermes CLI Bridge

This is the official Aura Lily bridge direction.

It runs Hermes through the public `hermes` command and does not patch, copy, or
modify Hermes source files.

Run `hermes status` before using this bridge. Provider keys should be configured
in Hermes, not stored in Aura Lily.

## Usage

```bash
python -m integrations.hermes_lily_cli.cli \
  --provider deepseek \
  --model deepseek-v4-pro \
  "帮我总结一下你能做什么"
```

JSON input is also supported:

```bash
echo '{"goal":"查一下今天上海天气","provider":"deepseek","model":"deepseek-v4-pro"}' \
  | python -m integrations.hermes_lily_cli.cli --json-input
```

You can also expose a small local HTTP bridge for ESP32/Mini requests:

```bash
python -m integrations.hermes_lily_cli.server \
  --host 0.0.0.0 \
  --port 8765 \
  --provider deepseek \
  --model deepseek-v4-pro \
  --max-concurrency 1 \
  --queue-timeout 30
```

Then post a turn:

```bash
curl -s http://127.0.0.1:8765/turn \
  -H 'content-type: application/json' \
  -d '{"goal":"你能帮我做什么"}'
```

The output is JSON:

```json
{
  "ok": true,
  "status": "completed",
  "response": "...",
  "request_id": "lily-...",
  "latency_ms": 1234,
  "evidence": {
    "returncode": 0,
    "command": ["hermes", "-z", "<prompt>", "..."]
  }
}
```

## Boundary

Allowed dependencies:

- Python standard library
- Installed `hermes` CLI
- Environment credentials already configured for Hermes

Not allowed:

- Importing Hermes source modules directly
- Applying any Hermes source overlay or patch
- Importing Aura desktop/life/persona/social modules
- Reading or writing private desktop App state

## HTTP Contract

`GET /health`

```json
{"ok": true, "service": "aura-lily-hermes"}
```

`GET /admin/hermes/config`

Requires HTTP Basic auth with the local admin username/password. Legacy
`x-aura-admin-token` and bearer credentials are accepted for compatibility.

```json
{
  "ok": true,
  "config": {
    "provider": "deepseek",
    "model": "deepseek-v4-pro",
    "model_options": [],
    "lily_stores_provider_keys": false
  }
}
```

`POST /admin/hermes/config`

Updates Lily's runtime provider/model selection. Provider API keys stay in
Hermes config or environment.

```json
{"provider": "deepseek", "model": "deepseek-v4-pro"}
```

`POST /turn`

Request:

```json
{"goal": "帮我查一下今天上海天气", "metadata": {"device_id": "esp32-dev"}}
```

Response:

```json
{"ok": true, "status": "completed", "response": "...", "evidence": {}}
```

Busy response:

```json
{"ok": false, "status": "failed", "error": "server is busy; retry later"}
```

The HTTP server runs a bounded queue in front of Hermes. The default is one
active turn at a time, which is safest for small device clients and local Hermes
state. Increase `--max-concurrency` only after validating your Hermes provider
and tool environment can handle parallel turns.

## Smoke Test

Run the CLI and HTTP bridge checks together:

```bash
python -m integrations.hermes_lily_cli.smoke_test \
  --provider deepseek \
  --model deepseek-v4-pro \
  --timeout 90
```

The smoke test starts an ephemeral local HTTP server, checks `/health`, posts a
real `/turn`, then shuts the server down.
