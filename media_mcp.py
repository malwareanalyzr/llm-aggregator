"""Custom media MCP server (Streamable HTTP on 127.0.0.1:8002/mcp, bearer-key protected)."""
import asyncio
import hmac
import json
import os
import re
import shutil
from pathlib import Path

import httpx
import uvicorn
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

load_dotenv()
SANDBOX = Path(__file__).parent / "sandbox"
SANDBOX.mkdir(exist_ok=True)
HF_TIMEOUT = 900  # video jobs can take minutes with --wait
SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

mcp = FastMCP(
    "media",
    transport_security=TransportSecuritySettings(  # DNS-rebinding guard; Docker reaches us as host.docker.internal
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "host.docker.internal:*"],
    ),
)


@mcp.tool()
async def ask_grok(prompt: str) -> str:
    """Ask xAI Grok (via the logged-in grok CLI subscription) a question and return its answer."""
    async with httpx.AsyncClient(timeout=320) as c:
        r = await c.post(
            "http://127.0.0.1:8010/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.getenv('ROUTER_API_KEY', '')}"},
            json={"model": "sub-grok", "messages": [{"role": "user", "content": prompt}]},
        )
    if r.status_code != 200:
        return f"router error {r.status_code}: {r.text[:500]}"
    return r.json()["choices"][0]["message"]["content"]


def higgsfield_cmd() -> list[str]:
    """Run the CLI without its npm .cmd shim, so prompts never pass through cmd.exe argument parsing."""
    exe = shutil.which("higgsfield")
    if not exe:
        raise RuntimeError("higgsfield CLI not installed (npm install -g @higgsfield/cli, then higgsfield auth login)")
    if not exe.lower().endswith((".cmd", ".bat", ".ps1")):
        return [exe]
    pkg = Path(exe).parent / "node_modules" / "@higgsfield" / "cli"
    native = [p for p in pkg.rglob("higgsfield*.exe")]
    if native:
        return [str(native[0])]
    bin_field = json.loads((pkg / "package.json").read_text())["bin"]
    entry = bin_field if isinstance(bin_field, str) else next(iter(bin_field.values()))
    return [shutil.which("node"), str(pkg / entry)]


async def higgsfield(*args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not re.search(r"KEY|TOKEN|SECRET|PASSWORD", k, re.I)}
    proc = await asyncio.create_subprocess_exec(
        *higgsfield_cmd(), *args, cwd=SANDBOX, env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), HF_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return f"higgsfield timed out after {HF_TIMEOUT}s"
    text = out.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        return f"higgsfield exited {proc.returncode}: {(err.decode('utf-8', 'replace') or text)[-1500:]}"
    return text


def check_id(value: str, what: str) -> str:
    if not SAFE_ID.match(value):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


@mcp.tool()
async def generate_image_higgsfield(prompt: str, model: str = "gpt_image_2_5", aspect_ratio: str = "1:1") -> str:
    """Generate an image with Higgsfield (uses subscription credits). Returns job JSON including the result URL.
    Models include gpt_image_2_5 (default), nano_banana_2, flux_2, soul_v2."""
    return await higgsfield("generate", "create", check_id(model, "model"), f"--prompt={prompt}",
                            f"--aspect_ratio={check_id(aspect_ratio, 'aspect_ratio')}", "--wait", "--json")


@mcp.tool()
async def generate_video_higgsfield(prompt: str, duration_sec: int = 5, model: str = "seedance_2_5",
                                    aspect_ratio: str = "16:9") -> str:
    """Generate a video with Higgsfield (uses subscription credits). Returns job JSON including the result URL.
    Models include seedance_2_5 (default), kling3_0."""
    if not 1 <= duration_sec <= 30:
        return "duration_sec must be 1-30"
    return await higgsfield("generate", "create", check_id(model, "model"), f"--prompt={prompt}",
                            f"--aspect_ratio={check_id(aspect_ratio, 'aspect_ratio')}",
                            f"--duration={duration_sec}", "--wait", "--json")


@mcp.tool()
async def higgsfield_job_status(job_id: str) -> str:
    """Get the status / result URL of a Higgsfield generation job."""
    return await higgsfield("generate", "get", check_id(job_id, "job_id"), "--json")


def require_key(app):
    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode().removeprefix("Bearer ").strip()
            expected = os.getenv("MCP_API_KEY", "")
            if not expected or not hmac.compare_digest(auth, expected):
                return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        await app(scope, receive, send)
    return guarded


if __name__ == "__main__":
    uvicorn.run(require_key(mcp.streamable_http_app()), host="127.0.0.1", port=8002)
