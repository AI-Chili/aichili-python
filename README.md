# AI Chili Python SDK, CLI & Stdio MCP Server (`aichili`)

[![CI](https://github.com/AI-Chili/aichili-python/actions/workflows/ci.yml/badge.svg)](https://github.com/AI-Chili/aichili-python/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-amber.svg)](https://github.com/AI-Chili/aichili-python/blob/main/LICENSE)
[![Python >=3.10](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://github.com/AI-Chili/aichili-python)

`mcp-name: io.github.AI-Chili/aichili`

Official **zero-dependency** Python (`>=3.10`) SDK, CLI (`aichili`), and stdio Model Context Protocol (MCP) server (`aichili-mcp`) for **[AI Chili (AIChili)](https://aichili.ai)** cinematic AI video generation and **[OnlyChili Cinema](https://onlychili.aichili.ai)** creator monetization.

- **Zero external runtime dependencies** (`dependencies = []`) — built strictly on the Python 3.10+ standard library (`urllib.request`, `json`, `re`, `uuid`, `time`, `ssl`) for supply-chain safety, instant cold start, and PEP 561 static typing (`py.typed`).
- **100% behavioral & pricing parity** with the official Node.js [`aichili` / `@aichili/mcp`](https://github.com/AI-Chili/aichili) package and `https://videoapi.aichili.ai`.
- **Two console entrypoints in one wheel**: `aichili` (developer CLI) and `aichili-mcp` (newline-delimited JSON-RPC 2.0 stdio MCP server with `1 MiB` line-buffer DoS guard).

---

## Installation

### Install from PyPI (once PyPI Trusted Publisher release is live)

```bash
pip install aichili

# Or run ephemerally with Astral uvx (zero global state)
uvx aichili --version
uvx aichili models
uvx --from aichili aichili-mcp
```

### Install directly from GitHub

```bash
pip install git+https://github.com/AI-Chili/aichili-python.git

# Or install isolated CLI binaries via uv
uv tool install git+https://github.com/AI-Chili/aichili-python.git
aichili --version
```

---

## Python SDK Quickstart

```python
from aichili import AiChiliClient, estimate_cost

# 1. Offline deterministic PAYG credit + OnlyChili 80% creator net revenue calculator
quote = estimate_cost(
    seconds=90,
    resolution="768p",
    model_id="h3-vdn",  # alias "h3-sol" also supported
    subscribers=50,
    sub_price=9.99,
)
print(quote["totalCredits"], quote["recommendedPack"], quote["onlyChiliProjection"]["creatorNetRevenueUsd"])

# 2. Public live model registry (no API key required)
client = AiChiliClient()  # or AiChiliClient(api_key="chili_sk_...")
registry = client.list_models()
for model in registry["models"]:
    print(model["model_id"], model["display_name"], model["provider"])

# 3. Authenticated wallet, $0 dry-run quote, idempotent submission & polling
authed = AiChiliClient(api_key="chili_sk_your_key_here")  # or export AICHILI_API_KEY="chili_sk_..."
wallet = authed.get_credits()
print("Credits:", wallet["balance"])

plan = authed.plan_generation(
    mode="t2v",
    model_id="h3-vdn",
    prompt="Rain-slick neon alley in Tokyo, slow dolly-in, stereo street ambience",
    duration_s=15,
    resolution="1080p",
    ratio="16:9",
)
print("Dry-run credits:", plan["total_credits"])

job = authed.create_generation(
    mode="t2v",
    model_id="h3-vdn",
    prompt="Rain-slick neon alley in Tokyo, slow dolly-in, stereo street ambience",
    duration_s=15,
    resolution="1080p",
    ratio="16:9",
    idempotency_key="tokyo-alley-shot-001",
)
completed = authed.wait_for_generation(job["id"], poll_interval_s=5.0, timeout_s=600.0)
print(completed["status"], completed)
```

---

## Command-Line Interface (`aichili`)

```bash
# Inspect live models from https://videoapi.aichili.ai/v1/registry
aichili models
aichili models --scope available --json

# Estimate PAYG credits, optimal pack combination & OnlyChili 80% creator net share
aichili estimate --seconds 90 --resolution 768p --subscribers 50 --sub-price 9.99 --json
aichili estimate --model avatar-talk --workflow photo --script-units 120 --free-used --json

# Authenticated wallet, dry-run plan, generation (with repeatable reference assets), and polling
export AICHILI_API_KEY="chili_sk_..."
aichili credits --json

aichili plan \
  --model h3-vdn \
  --mode t2v \
  --prompt "Golden hour coastal cliff aerial tracking shot, ocean surf audio" \
  --duration 15 \
  --resolution 1080p \
  --ratio 16:9

aichili generate \
  --model h3-vdn \
  --mode i2v \
  --prompt "Subject turns toward camera with subtle warm studio rim light" \
  --ref-asset 00000000-0000-0000-0000-000000000001 \
  --ref-role first_frame \
  --duration 10 \
  --resolution 1080p \
  --idempotency-key shot-001 \
  --wait \
  --json

aichili status <job_id> --json
```

---

## Model Context Protocol (MCP) Server (`aichili-mcp`)

### Local Stdio MCP Configuration (Claude Desktop, Cursor, Windsurf, Claude Code, Gemini CLI)

Using `uvx` (once published on PyPI):

```json
{
  "mcpServers": {
    "aichili-python": {
      "command": "uvx",
      "args": ["--from", "aichili", "aichili-mcp"],
      "env": {
        "AICHILI_API_KEY": "chili_sk_your_key_here"
      }
    }
  }
}
```

Or from a GitHub / virtual-environment install (`pip install git+https://github.com/AI-Chili/aichili-python.git`):

```json
{
  "mcpServers": {
    "aichili-python": {
      "command": "aichili-mcp",
      "env": {
        "AICHILI_API_KEY": "chili_sk_your_key_here"
      }
    }
  }
}
```

### Remote Streamable HTTP MCP (Zero Local Install)

```bash
claude mcp add --transport http aichili https://aichili.ai/mcp
claude mcp add --transport http onlychili https://onlychili.aichili.ai/mcp
```

### MCP Tools Included (7 Tools)

| Tool | Auth | Description |
|---|---|---|
| `platform_overview` | Public | Canonical metadata, endpoints, 5-model table, PAYG credit packs, and OnlyChili Cinema revenue share |
| `list_video_models` | Public | Live capabilities, workflows, durations, ratios, and pricing rules from `GET /v1/registry` |
| `estimate_video_cost` | Public | Deterministic PAYG credit calculator + `O(1)` pack solver + OnlyChili `80%` creator net earnings |
| `get_credit_balance` | `chili_sk_*` | Current credit balance, plan, and PAYG unlock status via `GET /v1/credits` |
| `plan_video_generation` | `chili_sk_*` | Server-side dry-run quote (`POST /v1/generations/plan`) — validates & prices with zero charge |
| `create_video_generation` | `chili_sk_*` | Enqueue video or talking-avatar generation (`POST /v1/generations`) with optional `idempotency_key` |
| `get_video_generation` | `chili_sk_*` | Poll job status, segment progress, and output asset via `GET /v1/generations/{job_id}` |

---

## Live Public Video & Avatar Models

| Model ID | Display Name | Modes | Single-Call Duration | Resolutions | Native Audio | Pricing |
|---|---|---|---|---|---|---|
| `h3-vdn` (`h3-sol`) | MiniMax H3 · SOL NSFW | `t2v`, `i2v` | `5–15s` | `480p`, `768p`, `1080p`, `2k` | Yes (Stereo) | `0.6` / `1.0` / `1.8` / `2.5` cr/s |
| `minimax-h3-gmi` | MiniMax H3 | `t2v`, `i2v` | `4–15s` | `768P`, `2K` | Yes | Live quote (`/v1/generations/plan`) |
| `wan3.0-video` | Wan 3.0 | `t2v`, `i2v` | `2–30s` | `480P`, `720P`, `1080P` | Yes | Live quote (`/v1/generations/plan`) |
| `seedance-2.5` | Seedance 2.5 | `t2v`, `i2v` | `4–15s` | `480p`, `720p`, `1080p` | Yes | Live quote (`/v1/generations/plan`) |
| `avatar-talk` | Avatar Talk | `i2v` (`talk`, `photo`) | `3–300s` | `768p`, `source` | Yes (Lip-Sync) | `0.2` cr / `5` script units (`+10` cr on `photo`) |

---

## PAYG Credit Packs & OnlyChili Cinema Monetization

| Pack | Price (USD) | Credits | Effective Rate |
|---|---|---|---|
| **Free Sign-Up** | `$0.00` | `25` credits | Free trial grant |
| **Starter** | `$9.90` | `150` credits | `$0.0660` / credit |
| **Creator** | `$29.90` | `460` credits | `~$0.0650` / credit |
| **Studio & API** | `$49.90` | `780` credits | `~$0.0640` / credit |

- **OnlyChili Cinema**: Creators keep **`80%` net revenue** across **`47` languages** with `$2.99–$99.99/mo` channel memberships and `$0.99–$500.00` pay-per-view (PPV) unlocks.

---

## PyPI Trusted Publishing (OIDC)

`.github/workflows/publish-pypi.yml` publishes `aichili` to PyPI automatically via GitHub OIDC Trusted Publishing (`pypa/gh-action-pypi-publish@release/v1`, zero long-lived API tokens) whenever a GitHub Release is published (`v1.0.1`):

- **PyPI Project Name**: `aichili`
- **Owner**: `AI-Chili`
- **Repository**: `aichili-python`
- **Workflow Name**: `publish-pypi.yml`
- **Environment Name**: `pypi`

---

## Security & Testing

Run the zero-dependency test suite (unit tests, PAYG & `O(1)` pack solver parity, security guards, CLI subprocess checks, stdio MCP JSON-RPC + `1 MiB` oversize line guard, and live read-only `GET https://videoapi.aichili.ai/v1/registry` smoke check):

```bash
python3 -m unittest discover -s tests -v
```

## Official Links

- **AI Chili Studio**: [https://aichili.ai](https://aichili.ai)
- **Developer Docs & API Reference**: [https://aichili.ai/developers](https://aichili.ai/developers)
- **Changelog**: [https://aichili.ai/changelog](https://aichili.ai/changelog)
- **OnlyChili Cinema**: [https://onlychili.aichili.ai](https://onlychili.aichili.ai)
- **Node.js CLI & MCP (`aichili` / `@aichili/mcp`)**: [https://github.com/AI-Chili/aichili](https://github.com/AI-Chili/aichili)

## License

MIT — Copyright (c) 2026 AI Chili Inc.
