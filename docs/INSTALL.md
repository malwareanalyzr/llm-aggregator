# Install guide (Windows)

## 1. Prerequisites

| Tool | Install | Check |
|---|---|---|
| Docker Desktop | https://www.docker.com/products/docker-desktop | `docker info` |
| Python 3.11+ | https://www.python.org | `python --version` |
| Node.js 20+ | https://nodejs.org | `node --version` |
| Ollama | https://ollama.com. In Settings, turn **off** "Expose Ollama to the network" (Docker reaches it on loopback) | `ollama list` |
| Tailscale (optional, for phone access) | `winget install tailscale.tailscale` | `tailscale ip -4` |

## 2. Subscription CLIs (install what you pay for)

Run each sign-in once in a **normal PowerShell window**. They open a browser.

```powershell
# Claude Code
irm https://claude.ai/install.ps1 | iex ; claude            # sign in, then /exit
# Codex (ChatGPT)
npm install -g @openai/codex ; codex login
# Antigravity CLI (replaces the retired gemini CLI)
irm https://antigravity.google/cli/install.ps1 | iex
& "$env:LOCALAPPDATA\agy\bin\agy.exe"                         # sign in, then exit
# Grok
grok login                                                   # after installing the Grok CLI
# Higgsfield (image/video tools)
npm install -g @higgsfield/cli ; higgsfield auth login
higgsfield workspace list ; higgsfield workspace set <workspace_id>
```

The router skips any CLI that isn't installed. `sub-gemini` falls back to OpenRouter.

## 3. Project setup

```powershell
git clone https://github.com/malwareanalyzr/llm-aggregator.git C:\ai-hub
cd C:\ai-hub
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
notepad .env        # fill in OPENROUTER_API_KEY, HF_TOKEN, ELEVENLABS_API_KEY, AA_API_KEY (all optional)
```

Leave `LITELLM_MASTER_KEY`, `ROUTER_API_KEY` and `MCP_API_KEY` blank. `start.ps1` generates them.

For `sub-claude` and `sub-claude-agent`, run `claude setup-token` in a normal PowerShell window and paste the token it prints into `.env` as
`CLAUDE_CODE_OAUTH_TOKEN=`. Don't paste it into a chat. `start.ps1` builds the agent's Docker image on its first run.

Port 8000 is often taken (Incredibuild, dev servers), so the router uses **8010**.

## 4. Start

```powershell
.\start.ps1
```

On the first run with Tailscale, `tailscale serve` may print a link to enable Serve for your tailnet. Open it,
click **Enable**, then run `.\stop.ps1; .\start.ps1`.

## 5. Open WebUI configuration

1. Open http://localhost:3000 and create the admin account. That first account becomes admin, and sign-ups are disabled after it.
2. Check that **Admin → Settings → Connections → OpenAI API** shows `http://host.docker.internal:8010/v1`, with the key set to `ROUTER_API_KEY` from `.env`.
3. In **Admin → Settings → External Tool Servers**, add two servers, each of type **MCP (Streamable HTTP)**, with auth **Bearer** and key = `MCP_API_KEY`:
   - `http://host.docker.internal:8001/mcp` (ElevenLabs)
   - `http://host.docker.internal:8002/mcp` (Grok + Higgsfield)
4. Pick `smart-router` as the default model.

Higgsfield's hosted MCP (`mcp.higgsfield.ai`) only accepts pre-registered clients, and Open WebUI isn't one of them.
Use the `:8002` tools instead, which go through the Higgsfield CLI.

## 6. Phone

Bookmark `https://<your-machine>.<your-tailnet>.ts.net`; `start.ps1` prints the URL. The phone must be on your tailnet.

## 7. Routing data

Nothing to do: `start.ps1` rebuilds `routing.json` from the public Artificial Analysis models page on every start.
`AA_API_KEY` is used only if that page can't be read. Get one at https://artificialanalysis.ai/api-key-management-redirect.

## Verify

```powershell
$k = (Select-String .env -Pattern '^ROUTER_API_KEY=(.*)').Matches[0].Groups[1].Value
curl.exe -s http://127.0.0.1:8010/health
curl.exe -s -H "Authorization: Bearer $k" http://127.0.0.1:8010/v1/models
.\.venv\Scripts\python.exe test_aa_routing.py
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| LiteLLM won't start: "no master key" | Run `start.ps1`, which generates `LITELLM_MASTER_KEY` |
| `sub-*` says "not found on PATH" | Install the CLI, then open a new terminal so PATH refreshes |
| `sub-gemini` returns "(no output …)" | Gemini tried a blocked tool (file or shell). Rephrase as a plain question |
| `sub-claude-agent`: "CLAUDE_CODE_OAUTH_TOKEN not set" | Run `claude setup-token`, add the token to `.env`, restart |
| Open WebUI shows no models | Check the connection URL and key in Admin → Connections (they're stored in Open WebUI's DB) |
| Claude warns its sign-in expires | Set `CLAUDE_CODE_OAUTH_TOKEN` in `.env` (from `claude setup-token`); `sub-claude` then uses it instead of the expiring sign-in |
