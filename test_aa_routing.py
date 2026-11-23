"""Self-check for aa_routing: python test_aa_routing.py"""
from aa_routing import build, choose, sub_for

assert sub_for("Claude Fable 5.2 Max") == ("sub-claude", "fable", "max")
assert sub_for("Gemini 4 Neon (Xhigh)") == ("sub-gemini", "gemini-4-neon-xhigh", None)
assert sub_for("Gemini 4 Argon Max") == ("sub-gemini", "gemini-4-argon-high", None)  # clamps to CLI max
assert sub_for("GPT-6.2 Astra Max") is None  # desktop-only, not in codex CLI
assert sub_for("Grok 4.7") == ("sub-grok", "grok-4.7", None)
assert sub_for("Claude Opus 5") is None  # no prefix false-positive on a different version


def model(name, score, orid=None):
    return {"name": name, "openrouter_api_id": orid, "evaluations": {"artificial_analysis_intelligence_index": score}}


filler = [model(f"Other {i}", 10 + i) for i in range(10)]
# Best sub wins even below API models; other CLIs then API are the fallbacks
r = build([model("API One", 90, "a/one"), model("API Two", 85), model("Claude Opus 5.5 High", 80),
           model("Claude Sonnet 5.5", 70), model("Grok 4.6", 50)] + filler)
c = choose(r, "coding")  # coding falls back to the Intelligence Index on free-tier data
assert (c["route"], c["model"], c["effort"], c["rank"]) == ("sub-claude", "opus", "high", 3), c
assert [f["route"] for f in c["fallbacks"]] == ["sub-grok", "openrouter/a/one"], c["fallbacks"]
# No sub ranked at all -> API
r = build([model("API One", 90, "a/one")] + filler)
assert choose(r, "general")["route"] == "openrouter/a/one"
print("aa_routing self-check passed")
