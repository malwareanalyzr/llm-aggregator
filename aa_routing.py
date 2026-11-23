"""Task -> model routing table built from Artificial Analysis evaluations.

Run `python aa_routing.py` to refresh routing.json. Source: the public artificialanalysis.ai/models page, which carries
every benchmark and domain index; the AA Data API (AA_API_KEY) is the fallback (its free tier lacks per-benchmark data).
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
AA_PUBLIC = "https://artificialanalysis.ai/models"
# public page field -> Data API evaluation key used in TASKS
PUBLIC_FIELDS = {
    "intelligenceIndex": "artificial_analysis_intelligence_index", "frontierCode20": "frontiercode_v2_0",
    "terminalBench40": "terminalbench_v4_0", "tauBanking": "tau_banking", "automationBenchPartialScore": "automationbench_aa",
    "critptAgenticHard": "critpt_v2_hard", "terminalBenchScience": "terminalbench_science", "hleDiamond": "hle_diamond",
    "omniscience": "aa_omniscience_index", "gdpPdfAllPass": "gdp_pdf", "lcr": "aa_lcr", "ifbench": "ifbench",
}
PUBLIC_CAPABILITIES = {
    "financeAndAccounting": "artificial_analysis_finance_and_accounting_index", "legal": "artificial_analysis_legal_index",
    "healthcareAndMedical": "artificial_analysis_healthcare_and_medical_index",
    "engineering": "artificial_analysis_engineering_index", "strategyAndOps": "artificial_analysis_strategy_and_ops_index",
    "economics": "artificial_analysis_economics_index",
}
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
            # first effort word after the base: "max" in "Claude Fable 5.2 (Max, Default Fallback)"
            effort = next((w for w in n[len(base):].replace(",", " ").split() if w in EFFORTS), None)
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
                        key=lambda m: m["evaluations"][key], reverse=True)
        # top 10, plus every sub-runnable model below it so the fallback chain covers all subs before the API
        ranked = ranked[:10] + [m for m in ranked[10:] if sub_for(m["name"])]
        rows = []
        for m in ranked:
            sub = sub_for(m["name"])
            rows.append({"name": m["name"], "score": m["evaluations"][key], "openrouter": m.get("openrouter_api_id"),
                         **({"route": sub[0], "model": sub[1], "effort": sub[2]} if sub else {})})
        tasks[task] = {"eval": key, "ranked": rows}
    return {"generated": time.strftime("%Y-%m-%d"), "tier": tier,
            "source": "Artificial Analysis (https://artificialanalysis.ai)", "tasks": tasks}


def choose(routing: dict, task: str) -> dict:
    """Best-ranked sub wins. Fallbacks: the other sub CLIs in rank order, then the top API model via OpenRouter."""
    ranked = routing["tasks"].get(task, routing["tasks"]["general"])["ranked"]
    subs, seen = [], set()
    for i, r in enumerate(ranked):
        if "route" in r and r["route"] not in seen:  # one entry per CLI: if a CLI fails, its other models will too
            seen.add(r["route"])
            subs.append({**r, "rank": i + 1})
    top = ranked[0] if ranked else {}
    api = {"route": f"openrouter/{top['openrouter']}" if top.get("openrouter") else "openrouter-auto",
           "model": None, "effort": None, "name": top.get("name", "openrouter-auto"), "rank": 1}
    chain = [*subs, api]
    return {**chain[0], "fallbacks": chain[1:]}


def load() -> dict | None:
    try:
        return json.loads(ROUTING_FILE.read_text())
    except (OSError, ValueError):
        return None


def parse_public(html: str) -> list[dict]:
    """Model records from the page's embedded Next.js payload, reshaped like Data API rows."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    payload = "".join(json.loads(f'"{c}"') for c in chunks)
    i = payload.index('"initialModels":') + len('"initialModels":')
    rows, _ = json.JSONDecoder().raw_decode(payload, i)
    out = []
    for r in rows:
        ev = {key: r.get(f) for f, key in PUBLIC_FIELDS.items()}
        ev |= {key: (r.get("capabilities") or {}).get(f) for f, key in PUBLIC_CAPABILITIES.items()}
        out.append({"name": r["name"], "openrouter_api_id": None, "evaluations": ev})
    return out


def fetch_public() -> list[dict]:
    import httpx
    r = httpx.get(AA_PUBLIC, headers={"User-Agent": "Mozilla/5.0"}, timeout=60, follow_redirects=True)
    r.raise_for_status()
    models = parse_public(r.text)
    if len(models) < MIN_COVERAGE:
        raise RuntimeError(f"only {len(models)} models parsed from {AA_PUBLIC}")
    return models


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
    try:
        models, tier = fetch_public(), "public-models-page"
    except Exception as e:
        key = os.getenv("AA_API_KEY")
        if not key:
            sys.exit(f"public AA page failed ({e}) and AA_API_KEY not set; keeping existing routing.json")
        models, tier = fetch(key)
    ROUTING_FILE.write_text(json.dumps(build(models, tier), indent=1))
    print(f"routing.json refreshed from {len(models)} AA models ({tier} tier)")
