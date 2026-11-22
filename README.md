# LLM Aggregator: local-first hybrid AI hub

One chat UI (Open WebUI) in front of every model you already pay for. A smart router sends each prompt to the
best backend for the task, using [Artificial Analysis](https://artificialanalysis.ai) benchmark data, and prefers
the **subscriptions** you're already logged into (Claude, Codex, Gemini/Antigravity, Grok) over per-token API spend.

Built for Windows with Docker Desktop. Remote access goes through Tailscale (tailnet-only HTTPS); nothing listens on your LAN.

## What you get

| Model id (in Open WebUI) | Backend | Billing |
|---|---|---|
| `smart-router` | Classifies the prompt, then picks via the AA routing table | — |
| `sub-claude` | `claude` CLI (Claude Code login) | Claude subscription |
| `sub-codex` | `codex` CLI (ChatGPT login) | ChatGPT subscription |
| `sub-gemini` | `agy` Antigravity CLI (Google login) | Antigravity / Gemini subscription |
| `sub-grok` | `grok` CLI (xAI login) | SuperGrok subscription |
| `local-daily` | Ollama `qwen3:8b` | free / local |
| `openrouter-auto` | OpenRouter auto-router | API spend |
| `hf-inference` | Hugging Face Inference | API spend |

MCP tool servers for Open WebUI:
- **ElevenLabs** (`:8001`): text-to-speech and voice tools.
- **Media** (`:8002`): `ask_grok`, plus `generate_image_higgsfield`, `generate_video_higgsfield` and `higgsfield_job_status`, which use the Higgsfield CLI and your Higgsfield plan credits.

Every reply is prefixed with the backend that handled it, e.g.
`*[Routed via sub-claude (Claude Fable 5.2 Max, task=coding, AA #1 on frontiercode_v2_0)]*`.

## Quick start

```powershell
.\start.ps1     # loads .env, generates local secrets, starts Docker + router + MCP servers + tailscale serve
.\stop.ps1      # stops everything
```

Then open http://localhost:3000. First-time setup: [docs/INSTALL.md](docs/INSTALL.md).
How it works and the security model: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Routing rule

1. A local model (`qwen2.5:3b`) labels the prompt with a task type: coding, science_math, finance, legal, chat, and so on.
2. Each task type maps to the Artificial Analysis evaluation that measures it, for example coding → FrontierCode / Terminal-Bench.
3. If a subscription model is in the **top 3** for that evaluation, the best-ranked one wins, run at the benchmarked effort level.
4. Otherwise the top API model is used through OpenRouter. If that call fails, the router falls back to the best subscription model.
5. `chat` always stays on the free local model.

`python aa_routing.py` refreshes `routing.json` from the AA Data API. It needs `AA_API_KEY`; `start.ps1` runs it automatically when the key is set.

## Attribution

Benchmark data: **[Artificial Analysis](https://artificialanalysis.ai)**. Attribution is required by their API terms.
