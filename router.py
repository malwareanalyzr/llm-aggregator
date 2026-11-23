"""Host smart router: OpenAI-compatible front door for LiteLLM + local subscription CLIs."""
import asyncio
import hmac
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request

import aa_routing
from fastapi.responses import JSONResponse, StreamingResponse

LITELLM = "http://localhost:4000/v1/chat/completions"
ROUTES = ["sub-claude", "sub-codex", "sub-gemini", "sub-grok", "local-daily", "openrouter-auto", "hf-inference"]
MODELS = ["smart-router", *ROUTES]
CLI_TIMEOUT = 300
MEDIA_TIMEOUT = 960  # video generation waits on Higgsfield (its tool allows 900s)
SANDBOX = Path(__file__).parent / "sandbox"  # empty cwd for CLIs; nothing worth reading in it
SANDBOX.mkdir(exist_ok=True)

# Tool lockdown per CLI: the CLIs answer as chat models only. Verified with a canary-file probe.
CLAUDE_LOCKDOWN = ["--tools", "", "--strict-mcp-config", "--permission-mode", "dontAsk",
                   "--setting-sources", "project",  # skip user settings: no hooks, plugins, allow-rules, auto mode
                   "--disable-slash-commands", "--no-session-persistence"]
# media mode: sub-claude may use ONLY the hub's own MCP servers (Higgsfield/Grok :8002, ElevenLabs :8001)
CLAUDE_MEDIA = ["--mcp-config", str(Path(__file__).parent / "claude-media-mcp.json"),
                "--allowedTools", "mcp__media", "mcp__elevenlabs"]
CODEX_LOCKDOWN = ["--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "-c", "web_search=disabled",
                  *[a for f in ("shell_tool", "unified_exec", "code_mode_host", "apps", "browser_use",
                                "browser_use_external", "in_app_browser") for a in ("--disable", f)]]
GROK_LOCKDOWN = ["--disable-web-search", "--tools", "todo_write", "--disallowed-tools",
                 "read_file,run_terminal_command,write,search_replace,grep,list_dir,get_command_or_subagent_output,"
                 "kill_command_or_subagent,monitor,workflow,image_edit,image_gen,image_to_video,reference_to_video,"
                 "send_feedback,ask_user_question"]
AGY_LOCKDOWN = ["--disable-slash-commands"]
# agy runs from its own profile (strict perms, deny list, no trusted workspaces); sign-in comes from the OS keyring
AGY_HOME = str(Path(__file__).parent / "agy-home")
_agy_cfg = Path(AGY_HOME) / ".gemini" / "antigravity-cli" / "settings.json"
_agy_cfg.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(Path(__file__).parent / "agy-settings.json", _agy_cfg)  # re-assert lockdown on every start

CLASSIFY_PROMPT = """Classify the user's request into ONE task type. Reply ONLY with JSON: {"task": "<name>"}.
Task types:
- chat: greetings, small talk, trivial one-line questions (default)
- media: create/generate an image, picture, video, speech, voice or audio, or check a generation/job status
- coding: writing, debugging, reviewing or refactoring code
- agentic: multi-step workflows, shell/terminal tasks, automation plans
- science_math: math, physics, chemistry, biology, quantitative reasoning
- knowledge: factual questions where accuracy and not hallucinating matter
- long_document: summarizing or answering questions about long pasted text
- instructions: strict formatting or constraint-following output
- finance: accounting, financial analysis, investing
- legal: law, contracts, compliance
- medical: health, medicine, clinical questions
- engineering: non-software engineering and technical design
- business: strategy, operations, management, marketing
- economics: economics and policy
- general: anything substantive not covered above"""
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
            "model": "local-fast",
            "messages": [{"role": "system", "content": CLASSIFY_PROMPT}, {"role": "user", "content": last[:4000]}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
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
                  timeout: int = CLI_TIMEOUT) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=SANDBOX,
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


async def call_route(route: str, body: dict, model: str | None = None,
                     effort: str | None = None, media: bool = False) -> tuple[str, str, str | None, list | None]:
    """Returns (route_used, assistant_text, note, tool_calls). model/effort select a model on a sub CLI.
    Only LiteLLM routes return tool_calls (Open WebUI runs them); sub CLIs stay tool-free by design."""
    prompt = flatten(body["messages"])
    if route == "sub-claude":
        sel = [*(["--model", model] if model else []), *(["--effort", effort] if effort else [])]
        # text output: json mode hangs when a tool-less session wants a tool. "--" stops a "-"-prefixed prompt
        # from being parsed as a CLI flag.
        if media:  # the MCP key reaches claude only via env; claude-media-mcp.json references ${MCP_API_KEY}
            env = {"MCP_API_KEY": os.getenv("MCP_API_KEY", ""), "MCP_TOOL_TIMEOUT": str(MEDIA_TIMEOUT * 1000)}
            return route, await run_cli([need("claude"), "-p", "--output-format", "text", *CLAUDE_LOCKDOWN,
                                         *CLAUDE_MEDIA, *sel, "--", prompt], extra_env=env,
                                        timeout=MEDIA_TIMEOUT), None, None
        return route, await run_cli([need("claude"), "-p", "--output-format", "text", *CLAUDE_LOCKDOWN, *sel,
                                     "--", prompt]), None, None
    # .cmd shims (codex) and others get the prompt via stdin, keeping user text out of argument parsing
    if route == "sub-codex":
        sel = [*(["-m", model] if model else []), *(["-c", f"model_reasoning_effort={effort}"] if effort else [])]
        return route, await run_cli([need("codex"), "exec", *CODEX_LOCKDOWN, *sel, "-C", str(SANDBOX), "-"],
                                    stdin=prompt), None, None
    if route == "sub-grok":
        sel = [*(["-m", model] if model else []), *(["--reasoning-effort", effort] if effort else [])]
        return route, await run_cli([need("grok"), "-p", "--cwd", str(SANDBOX), *GROK_LOCKDOWN, *sel],
                                    stdin=prompt), None, None
    if route == "sub-gemini":
        exe = agy_cli()
        if exe:
            sel = ["--model", model] if model else []
            out = json.loads(await run_cli([exe, f"--prompt={prompt}", "--output-format", "json", *AGY_LOCKDOWN, *sel],
                                           extra_env={"USERPROFILE": AGY_HOME, "HOME": AGY_HOME}))
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
    for i, step in enumerate([p, *p.get("fallbacks", [])]):  # subs first, API last (see aa_routing.choose)
        try:
            used, text, note, tool_calls = await call_route(step["route"], body, step.get("model"),
                                                            step.get("effort"), step.get("media", False))
        except Exception as e:
            errors.append(f"{step['route']}: {str(e)[:200]}")
            used, text = step["route"], "Backend error: " + " | ".join(errors)
            continue
        if i:
            note = f"fell back to {step.get('name', step['route'])} after: " + " | ".join(errors)
            p = {**step, "label": f"{step.get('name', step['route'])}, fallback"}
        break
    headers = {"X-Router-Route": used}
    if note:
        headers["X-Router-Note"] = " ".join(note.split()).encode("ascii", "replace").decode()[:500]  # header-safe
    label = f" ({p['label']})" if p.get("label") else ""
    # request shape only, never content
    print(f"[chat] model={model} task={task} route={used} fields={sorted(body)} msgs={len(body['messages'])} out_chars={len(text)} tool_calls={len(tool_calls or [])}", flush=True)
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
