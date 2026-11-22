"""Task -> model routing table built from Artificial Analysis evaluations.

Run `python aa_routing.py` to refresh routing.json from the AA Data API (needs AA_API_KEY in .env).
Data: Artificial Analysis, https://artificialanalysis.ai (attribution required by their terms).
"""
import json
import os
import re
import sys
import time
from pathlib import Path

ROUTING_FILE = Path(__file__).parent / "routing.json"
AA_BASE = "https://artificialanalysis.ai/api/v2/language/models"
GENERAL = "artificial_analysis_intelligence_index"

# Task -> AA evaluation keys, best first. Pro-only keys fall back to the next key, then the Intelligence Index.
TASKS = {
    "coding":        ["frontiercode_v2_0", "terminalbench_v4_0"],
    "agentic":       ["terminalbench_v4_0", "tau_banking", "automationbench_aa"],
    "science_math":  ["critpt_v2_hard", "terminalbench_science", "hle_diamond"],
    "knowledge":     ["aa_omniscience_index"],
    "long_document": ["gdp_pdf", "aa_lcr"],
    "instructions":  ["ifbench"],
    "finance":       ["artificial_analysis_finance_and_accounting_index"],
    "legal":         ["artificial_analysis_legal_index"],
    "medical":       ["artificial_analysis_healthcare_and_medical_index"],
    "engineering":   ["artificial_analysis_engineering_index"],
    "business":      ["artificial_analysis_strategy_and_ops_index"],
    "economics":     ["artificial_analysis_economics_index"],
    "general":       [GENERAL],
}
EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max"]
STD = ["low", "medium", "high", "xhigh", "max"]

# Models reachable through logged-in subscription CLIs: (AA base name, route, CLI model id, efforts the CLI accepts)
SUB_MODELS = [
    ("claude fable 5.2", "sub-claude", "fable", STD),
    ("claude opus 5.5", "sub-claude", "opus", STD),
    ("claude sonnet 5.5", "sub-claude", "sonnet", STD),
    ("gemini 4 neon", "sub-gemini", "gemini-4-neon", ["low", "medium", "high", "xhigh"]),
    ("gemini 4 hydrogen", "sub-gemini", "gemini-4-hydrogen", ["low", "medium", "high", "xhigh"]),
    ("gemini 4 argon", "sub-gemini", "gemini-4-argon", ["low", "medium", "high"]),
    ("gemini 3.8 flash", "sub-gemini", "gemini-3.8-flash", ["low", "medium", "high"]),
    ("gpt 6 luna", "sub-codex", "gpt-6-luna", ["low", "medium", "high", "xhigh"]),
    ("gpt 5.6 terra", "sub-codex", "gpt-5.6-terra", ["low", "medium", "high", "xhigh"]),
    ("gpt 5.6 luna", "sub-codex", "gpt-5.6-luna", ["low", "medium", "high", "xhigh"]),
    ("grok 4.7", "sub-grok", "grok-4.7", ["low", "medium", "high"]),
    ("grok 4.6", "sub-grok", "grok-4.6", ["low", "medium", "high"]),
    ("grok 4.5", "sub-grok", "grok-4.5", ["low", "medium", "high"]),
]
MIN_COVERAGE = 10  # an eval must score at least this many models to be used for ranking


def norm(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_()]", " ", name.lower())).strip()


def sub_for(aa_name: str):
    """Map an AA model name like 'Claude Opus 5.5 Max' to (route, cli_model, effort) if a sub can run it."""
    n = norm(aa_name)
    for base, route, model, efforts in SUB_MODELS:
        if n == base or n.startswith(base + " "):
            effort = n[len(base):].split()[-1] if n != base else None
            if effort not in EFFORTS:
                effort = None
            if effort and effort not in efforts:  # e.g. AA "Max" on a CLI that tops out at xhigh
                effort = efforts[-1]
            if route == "sub-gemini":  # agy encodes effort in the model slug
                return route, f"{model}-{effort or efforts[-1]}", None
            return route, model, effort
    return None


def build(models: list[dict], tier: str = "unknown") -> dict:
    tasks = {}
    for task, keys in TASKS.items():
        key = next((k for k in keys + [GENERAL]
                    if sum(1 for m in models if (m.get("evaluations") or {}).get(k) is not None) >= MIN_COVERAGE), GENERAL)
        ranked = sorted((m for m in models if (m.get("evaluations") or {}).get(key) is not None),
                        key=lambda m: m["evaluations"][key], reverse=True)[:10]
        rows = []
        for m in ranked:
            sub = sub_for(m["name"])
            rows.append({"name": m["name"], "score": m["evaluations"][key], "openrouter": m.get("openrouter_api_id"),
                         **({"route": sub[0], "model": sub[1], "effort": sub[2]} if sub else {})})
        tasks[task] = {"eval": key, "ranked": rows}
    return {"generated": time.strftime("%Y-%m-%d"), "tier": tier,
            "source": "Artificial Analysis (https://artificialanalysis.ai)", "tasks": tasks}


def choose(routing: dict, task: str) -> dict:
    """Sub in the top 3 wins; otherwise the top API model via OpenRouter, with the best sub as fallback."""
    ranked = routing["tasks"].get(task, routing["tasks"]["general"])["ranked"]
    best_sub = next((r for r in ranked if "route" in r), None)
    top3_sub = next((r for r in ranked[:3] if "route" in r), None)
    if top3_sub:
        return {**top3_sub, "rank": ranked.index(top3_sub) + 1}
    top = ranked[0] if ranked else {}
    api = {"route": f"openrouter/{top['openrouter']}" if top.get("openrouter") else "openrouter-auto",
           "model": None, "effort": None, "name": top.get("name", "openrouter-auto"), "rank": 1}
    return {**api, "fallback": best_sub}


def load() -> dict | None:
    try:
        return json.loads(ROUTING_FILE.read_text())
    except (OSError, ValueError):
        return None


def fetch(key: str) -> tuple[list[dict], str]:
    import httpx
    for url in (AA_BASE, AA_BASE + "/free"):  # Pro has per-benchmark evals; Free only headline/domain indices
        models, page = [], 1
        while True:
            r = httpx.get(url, headers={"x-api-key": key}, params={"page": page}, timeout=60)
            if r.status_code == 403 and url == AA_BASE:
                break
            r.raise_for_status()
            body = r.json()
            models += body["data"]
            if not (body.get("pagination") or {}).get("has_more"):
                return models, body.get("tier", "unknown")
            page += 1
    raise RuntimeError("no accessible AA endpoint")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
    key = os.getenv("AA_API_KEY")
    if not key:
        sys.exit("AA_API_KEY not set in .env; keeping existing routing.json")
    models, tier = fetch(key)
    ROUTING_FILE.write_text(json.dumps(build(models, tier), indent=1))
    print(f"routing.json refreshed from {len(models)} AA models ({tier} tier)")
