"""Host smart router: OpenAI-compatible front door for LiteLLM + local subscription CLIs."""
import asyncio
import base64
import hmac
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request

import aa_routing
from fastapi.responses import JSONResponse, StreamingResponse

LITELLM = "http://localhost:4000/v1/chat/completions"
ROUTES = ["sub-claude", "sub-codex", "sub-gemini", "sub-grok", "sub-claude-agent", "local-daily", "openrouter-auto",
          "hf-inference"]
MODELS = ["smart-router", *ROUTES]
CLI_TIMEOUT = 300
MEDIA_TIMEOUT = 960  # video generation waits on Higgsfield (its tool allows 900s)
AGENT_TIMEOUT = 1800
SANDBOX = Path(__file__).parent / "sandbox"  # holds one empty-or-uploads-only cwd per request
SANDBOX.mkdir(exist_ok=True)
WORKSPACE = Path(__file__).parent / "workspace"  # the only host folder sub-claude-agent's container can see
WORKSPACE.mkdir(exist_ok=True)
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}

# Tool lockdown per CLI: chat models plus web search/fetch and reading the request's uploads. No shell, no writes,
# no files outside the per-request folder. Verified with a canary-file probe.
CLAUDE_TOOLS = ["Read", "WebSearch", "WebFetch"]
CLAUDE_LOCKDOWN = ["--restricted",  # confines Read to the cwd (the request folder)
                   "--tools", *CLAUDE_TOOLS, "--strict-mcp-config", "--permission-mode", "dontAsk",
                   "--setting-sources", "project",  # skip user settings: no hooks, plugins, allow-rules, auto mode
                   "--disable-slash-commands", "--no-session-persistence"]
# media mode adds ONLY the hub's own MCP servers (Higgsfield/Grok :8002, ElevenLabs :8001)
CLAUDE_MEDIA_MCP = ["--mcp-config", str(Path(__file__).parent / "claude-media-mcp.json")]
CODEX_LOCKDOWN = ["--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "-c", "web_search=live",
                  # code_mode_host stays on: codex's web search runs through it (canary probe: no file access)
                  *[a for f in ("shell_tool", "unified_exec", "apps", "browser_use",
                                "browser_use_external", "in_app_browser") for a in ("--disable", f)]]
# Grok's read_file is NOT confined to its cwd (canary probe), so Grok gets web tools but no file access or images
GROK_LOCKDOWN = ["--tools", "todo_write,web_search,web_fetch", "--disallowed-tools",
                 "read_file,run_terminal_command,write,search_replace,grep,list_dir,get_command_or_subagent_output,"
                 "kill_command_or_subagent,monitor,workflow,image_edit,image_gen,image_to_video,reference_to_video,"
                 "send_feedback,ask_user_question"]
AGY_LOCKDOWN = ["--disable-slash-commands"]
# sub-claude-agent: full tools inside a throwaway container; only ./workspace is mounted. Docker is the fence:
# on Windows, Claude's own Bash sandbox is off, so the CLI's shell could otherwise read any host file.
AGENT_DOCKER = ["run", "--rm", "-i", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "512", "--memory", "4g",
                "--add-host", "host.docker.internal:127.0.0.1",  # don't hand it the name of the hub's host
                "-v", f"{WORKSPACE}:/workspace", "-e", "CLAUDE_CODE_OAUTH_TOKEN", "ai-hub-agent",
                "timeout", str(AGENT_TIMEOUT - 10), "claude", "-p", "--output-format", "text",
                "--permission-mode", "bypassPermissions", "--no-session-persistence"]
# agy runs from its own profile (strict perms, deny list, no trusted workspaces); sign-in comes from the OS keyring
AGY_HOME = str(Path(__file__).parent / "agy-home")
_agy_cfg = Path(AGY_HOME) / ".gemini" / "antigravity-cli" / "settings.json"
_agy_cfg.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(Path(__file__).parent / "agy-settings.json", _agy_cfg)  # re-assert lockdown on every start

CLASSIFY_PROMPT = """Classify the user's request into ONE task type. Reply ONLY with JSON: {"task": "<name>"}.
Task types:
- chat: ONLY greetings, thanks or small talk with no real question or task
- media: create/generate a NEW image, picture, video, speech, voice or audio, or check a generation/job status (questions about an attached image are NOT media)
- coding: writing, debugging, reviewing or refactoring code
- agentic: running shell/terminal commands or automation scripts, or testing/operating software systems
- science_math: math, physics, chemistry, biology, quantitative reasoning
- knowledge: factual questions where accuracy and not hallucinating matter
- long_document: summarizing or answering questions about long pasted text
- instructions: requests whose main point is an exact format or constraint (word/bullet limits, templates)
- finance: accounting, valuation (DCF), financial models/statements, budgeting, investing, markets
- legal: law, contracts, compliance
- medical: health, medicine, clinical questions
- engineering: mechanical/electrical/civil/chemical engineering and physical system design (not software, not finance)
- business: strategy, operations, management, marketing, sales
- economics: economics, trade, tariffs, inflation, monetary or fiscal policy
- general: anything substantive not covered above"""
CLASSIFY_MODEL = "local-daily"  # qwen3:8b labels 16/17 test prompts right vs 11/17 for qwen2.5:3b
TASK_NAMES = {"chat", "media", *aa_routing.TASKS}

app = FastAPI()


@app.middleware("http")
async def require_key(request: Request, call_next):
    if request.url.path.startswith("/v1/"):
        expected = os.getenv("ROUTER_API_KEY", "")
        got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not expected or not hmac.compare_digest(got, expected):
            return JSONResponse({"error": {"message": "invalid or missing API key"}}, status_code=401)
    return await call_next(request)


def cli_env(extra: dict | None = None) -> dict:
    """Child env without secrets; CLIs authenticate from their own profile files / OS keyring."""
    env = {k: v for k, v in os.environ.items() if not re.search(r"KEY|TOKEN|SECRET|PASSWORD", k, re.I)}
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"  # keeps claude.ai connectors (Gmail, Drive...) out of sub-claude
    return env | (extra or {})


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multimodal parts
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def save_images(messages: list, folder: Path) -> list[Path]:
    """Write base64 data-URL images (how Open WebUI sends uploads) into folder; returns their paths."""
    out = []
    for m in messages:
        for p in m.get("content") if isinstance(m.get("content"), list) else []:
            url = ((p.get("image_url") or {}).get("url") or "") if isinstance(p, dict) else ""
            mt = re.match(r"data:([\w/+.-]+);base64,", url)
            # shortcut: remote http(s) image URLs are skipped; Open WebUI always sends data URLs
            if not mt or mt[1] not in IMAGE_TYPES or len(url) > MAX_IMAGE_BYTES * 4 // 3 + 100:
                continue
            path = folder / f"image{len(out) + 1}.{IMAGE_TYPES[mt[1]]}"
            path.write_bytes(base64.b64decode(url[mt.end():]))
            out.append(path)
    return out


def flatten(messages: list) -> str:
    if len(messages) == 1:
        return text_of(messages[0].get("content"))
    return "\n\n".join(f"{m.get('role', 'user').upper()}: {text_of(m.get('content'))}" for m in messages)


async def litellm(body: dict) -> dict:
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(LITELLM, json={**body, "stream": False},
                         headers={"Authorization": f"Bearer {os.getenv('LITELLM_MASTER_KEY', '')}"})
        r.raise_for_status()
        return r.json()


async def classify(messages: list) -> str:
    last = next((text_of(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
    try:
        data = await litellm({
            "model": CLASSIFY_MODEL,
            "messages": [{"role": "system", "content": CLASSIFY_PROMPT}, {"role": "user", "content": last[:4000]}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "think": False,  # qwen3: label only, no reasoning pass (377 -> 7 output tokens)
        })
        task = json.loads(data["choices"][0]["message"]["content"]).get("task")
        return task if task in TASK_NAMES else "chat"
    except Exception:
        return "chat"


def plan(task: str) -> dict:
    """Pick the backend for a task from the AA routing table (see aa_routing.choose)."""
    routing = aa_routing.load()
    if task == "media":  # sub-claude with only the hub's media MCP tools; API model + Open WebUI tools if it fails
        return {"route": "sub-claude", "media": True, "name": "Claude", "label": "task=media, hub MCP tools",
                "fallbacks": [{"route": "openrouter-auto", "name": "openrouter-auto"}]}
    if not routing:
        return {"route": "local-daily", "label": f"task={task}, no routing table"}
    c = aa_routing.choose(routing, "general" if task == "chat" else task)
    ev = routing["tasks"].get(task, routing["tasks"]["general"])["eval"]
    c["label"] = f"{c['name']}, task={task}, AA #{c['rank']} on {ev}"
    return c


async def run_cli(args: list[str], stdin: str | None = None, extra_env: dict | None = None,
                  timeout: int = CLI_TIMEOUT, cwd: Path = SANDBOX) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=cli_env(extra_env),
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode("utf-8") if stdin is not None else None), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{Path(args[0]).name} timed out after {timeout}s")
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(args[0]).name} exited {proc.returncode}: {err.decode('utf-8', 'replace')[-2000:]}")
    return out.decode("utf-8", "replace").strip()


def agy_cli() -> str | None:
    # the old `gemini` CLI is rejected by Google; Antigravity's agy is its replacement
    fallback = Path(os.getenv("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"
    return shutil.which("agy") or (str(fallback) if fallback.exists() else None)


def need(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        raise RuntimeError(f"{name} CLI not found on PATH")
    return exe


async def call_route(route: str, body: dict, model: str | None = None, effort: str | None = None,
                     media: bool = False, work: Path = SANDBOX) -> tuple[str, str, str | None, list | None]:
    """Returns (route_used, assistant_text, note, tool_calls). model/effort select a model on a sub CLI.
    work is the request's folder (cwd for the CLIs, holding only its uploaded images).
    Only LiteLLM routes return tool_calls (Open WebUI runs them); sub CLIs use their own built-in tools."""
    prompt = flatten(body["messages"])
    images = sorted(work.glob("image*"))
    if images:
        prompt += "\n\nAttached images (in the working directory): " + ", ".join(p.name for p in images)
    if images and route in ("sub-grok", "sub-gemini"):  # next route in the chain takes it
        raise RuntimeError(f"{route} can't view uploaded images in this hub")
    if route == "sub-claude":
        sel = [*(["--model", model] if model else []), *(["--effort", effort] if effort else [])]
        # text output: json mode hangs when a tool-less session wants a tool. "--" stops a "-"-prefixed prompt
        # from being parsed as a CLI flag.
        allowed = ["--allowedTools", *CLAUDE_TOOLS, *(["mcp__media", "mcp__elevenlabs"] if media else [])]
        args = [need("claude"), "-p", "--output-format", "text", *CLAUDE_LOCKDOWN, *allowed, *sel]
        # long-lived setup-token, if set, instead of the interactive sign-in that expires
        env = {"CLAUDE_CODE_OAUTH_TOKEN": t} if (t := os.getenv("CLAUDE_CODE_OAUTH_TOKEN")) else {}
        if media:  # the MCP key reaches claude only via env; claude-media-mcp.json references ${MCP_API_KEY}
            env |= {"MCP_API_KEY": os.getenv("MCP_API_KEY", ""), "MCP_TOOL_TIMEOUT": str(MEDIA_TIMEOUT * 1000)}
            return route, await run_cli([*args, *CLAUDE_MEDIA_MCP, "--", prompt], extra_env=env,
                                        timeout=MEDIA_TIMEOUT, cwd=work), None, None
        return route, await run_cli([*args, "--", prompt], extra_env=env, cwd=work), None, None
    if route == "sub-claude-agent":
        token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
        if not token:
            raise RuntimeError("CLAUDE_CODE_OAUTH_TOKEN not set in .env (run `claude setup-token`)")
        if images:  # uploads land in workspace/uploads/<request>/ for the agent
            dest = WORKSPACE / "uploads" / work.name
            dest.mkdir(parents=True, exist_ok=True)
            for p in images:
                shutil.copy(p, dest)
            prompt += f" (copied to ./uploads/{work.name}/)"
        sel = [*(["--model", model] if model else []), *(["--effort", effort] if effort else [])]
        return route, await run_cli([need("docker"), *AGENT_DOCKER, *sel], stdin=prompt,
                                    extra_env={"CLAUDE_CODE_OAUTH_TOKEN": token}, timeout=AGENT_TIMEOUT), None, None
    # .cmd shims (codex) and others get the prompt via stdin, keeping user text out of argument parsing
    if route == "sub-codex":
        sel = [*(["-m", model] if model else []), *(["-c", f"model_reasoning_effort={effort}"] if effort else [])]
        imgs = [a for p in images for a in ("-i", str(p))]
        return route, await run_cli([need("codex"), "exec", *CODEX_LOCKDOWN, *sel, *imgs, "-C", str(work), "-"],
                                    stdin=prompt, cwd=work), None, None
    if route == "sub-grok":
        sel = [*(["-m", model] if model else []), *(["--reasoning-effort", effort] if effort else [])]
        return route, await run_cli([need("grok"), "-p", "--cwd", str(work), *GROK_LOCKDOWN, *sel],
                                    stdin=prompt, cwd=work), None, None
    if route == "sub-gemini":
        exe = agy_cli()
        if exe:
            sel = ["--model", model] if model else []
            out = json.loads(await run_cli([exe, f"--prompt={prompt}", "--output-format", "json", *AGY_LOCKDOWN, *sel],
                                           extra_env={"USERPROFILE": AGY_HOME, "HOME": AGY_HOME}, cwd=work))
            if out.get("status") != "SUCCESS":
                raise RuntimeError(f"agy {out.get('status')}: {out.get('error')}")
            return route, out.get("response") or "(no output: Gemini tried a tool, which this hub blocks)", None, None
        route, note = "openrouter-auto", "sub-gemini unavailable (agy not installed); fell back to openrouter-auto"
        try:
            _, text, _, tool_calls = await call_route(route, body)
        except Exception as e:
            raise RuntimeError(f"{note}, which failed: {e}") from e
        return route, text, note, tool_calls
    passthrough = {k: v for k, v in body.items() if k not in ("model", "stream", "stream_options")}
    data = await litellm({**passthrough, "model": route})
    msg = data["choices"][0]["message"]
    return route, msg.get("content") or "", None, msg.get("tool_calls")


def completion(model: str, text: str, tool_calls: list | None = None) -> dict:
    msg = {"role": "assistant", "content": text, **({"tool_calls": tool_calls} if tool_calls else {})}
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
    }


@app.get("/health")
async def health():
    return {"status": "ok", "clis": {n: bool(shutil.which(n)) for n in ("claude", "codex", "grok")} | {"agy": bool(agy_cli())}}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "ai-hub"} for m in MODELS]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    model = body.get("model", "smart-router")
    if not body.get("messages"):
        return JSONResponse({"error": {"message": "messages is required"}}, status_code=400)
    if model not in MODELS:
        return JSONResponse({"error": {"message": f"unknown model {model}"}}, status_code=400)

    task = await classify(body["messages"]) if model == "smart-router" else None
    p = plan(task) if task else {"route": model}
    note = tool_calls = None
    errors = []
    work = Path(tempfile.mkdtemp(prefix="req-", dir=SANDBOX))
    try:
        images = save_images(body["messages"], work)
        for i, step in enumerate([p, *p.get("fallbacks", [])]):  # subs first, API last (see aa_routing.choose)
            try:
                used, text, note, tool_calls = await call_route(step["route"], body, step.get("model"),
                                                                step.get("effort"), step.get("media", False), work)
            except Exception as e:
                errors.append(f"{step['route']}: {str(e)[:200]}")
                used, text = step["route"], "Backend error: " + " | ".join(errors)
                continue
            if i:
                note = f"fell back to {step.get('name', step['route'])} after: " + " | ".join(errors)
                p = {**step, "label": f"{step.get('name', step['route'])}, fallback"}
            break
    finally:
        shutil.rmtree(work, ignore_errors=True)
    headers = {"X-Router-Route": used}
    if note:
        headers["X-Router-Note"] = " ".join(note.split()).encode("ascii", "replace").decode()[:500]  # header-safe
    label = f" ({p['label']})" if p.get("label") else ""
    # request shape only, never content
    print(f"[chat] model={model} task={task} route={used} fields={sorted(body)} msgs={len(body['messages'])} images={len(images)} out_chars={len(text)} tool_calls={len(tool_calls or [])}", flush=True)
    text = f"*[Routed via {used}{label}]*\n\n{text}"

    if not body.get("stream"):
        return JSONResponse(completion(model, text, tool_calls), headers=headers)

    # shortcut: backends are called non-streaming; we emit the full reply as one SSE chunk.
    async def sse():
        chunk = completion(model, "")
        chunk["object"] = "chat.completion.chunk"
        delta = {"role": "assistant", "content": text}
        if tool_calls:  # streamed tool_calls carry an index per call
            delta["tool_calls"] = [{"index": i, **tc} for i, tc in enumerate(tool_calls)]
        chunk["choices"] = [{"index": 0, "delta": delta, "finish_reason": None}]
        yield f"data: {json.dumps(chunk)}\n\n"
        chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool_calls else "stop"}]
        yield f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream", headers=headers)


if __name__ == "__main__":
    import uvicorn
    # loopback only: Docker Desktop's host.docker.internal still reaches it; LAN/tailnet can't.
    uvicorn.run(app, host="127.0.0.1", port=8010)  # 8000 is taken by Incredibuild's Apache on this PC
