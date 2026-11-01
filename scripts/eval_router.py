#!/usr/bin/env python3
"""Evaluate the decision router against a prompted-Spark baseline on the held-out test set.

Systems (pick with --systems):
    router    the endpoint in ROUTER_BASE_URL / ROUTER_MODEL / ROUTER_API_KEY: a tuned Glimmer on a
              Fireworks deployment or local vLLM, or base Glimmer on OpenRouter (meta/muse-glimmer-30b)
    spark     prompted Muse Spark (MODEL_BASE_URL / CHAT_MODEL / MODEL_API_KEY) with --few-shot
              examples from train.jsonl: the baseline the router has to beat
    keywords  today's offline rule (meta_client.keyword_intent, hint level 1), no network

Metrics per system:
    json_valid        the whole reply is one schema-valid JSON object (before any guard)
    parsed            a decision can be recovered at all (the server's tolerant parser)
    tool_accuracy     predicted tools == gold tools (order ignored); invalid replies count as wrong
    hint_error        rows where gold and prediction both give a hint: mean error, over-revealing weighted
                      by --over-weight (default 2), plus mean absolute error and over-reveal rate
    answer_leak       gold give_hint, predicted finish_answer: the cell that must stay at zero
    intent_macro_f1   on clean and on speech-noised commands separately
    latency           p50 / p95 ms per decision
and an exact McNemar test on tool accuracy, router vs each other system.

Run (after scripts/build_router_data.py):
    uv run --no-project -p 3.11 --with-requirements requirements.txt \\
        python scripts/eval_router.py --systems router,spark,keywords --limit 300
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import json
from math import comb
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import meta_client, router, skills  # noqa: E402

SYSTEMS = ("router", "spark", "keywords")


@dataclass
class Prediction:
    text: str
    ms: float
    error: str | None = None

    @property
    def decision(self) -> router.Decision | None:
        return router.parse_decision(self.text)


def load_jsonl(path: str | Path, limit: int = 0) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    return rows[:limit] if limit else rows


def gold_of(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row["messages"][2]["content"])


def state_of(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row["messages"][1]["content"])


def few_shot(train: list[dict[str, Any]], k: int) -> list[dict[str, str]]:
    """k user/assistant pairs, round-robin over intents so every intent is shown."""
    if k <= 0 or not train:
        return []
    by_intent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in train:
        by_intent[gold_of(row)["intent"]].append(row)
    picked: list[dict[str, Any]] = []
    while len(picked) < k and any(by_intent.values()):
        for intent in sorted(by_intent):
            if by_intent[intent] and len(picked) < k:
                picked.append(by_intent[intent].pop(0))
    return [m for row in picked for m in row["messages"][1:3]]


# --------------------------------------------------------------------------- systems


def keyword_prediction(row: dict[str, Any]) -> Prediction:
    """Today's offline behaviour: keyword intent, its tool, the client's default level 1."""
    t0 = time.perf_counter()
    intent = meta_client.keyword_intent(state_of(row)["command"])
    text = json.dumps({"intent": intent, "tools": [skills.INTENT_TOOL[intent]], "hint_level": 1, "confidence": 1.0})
    return Prediction(text, (time.perf_counter() - t0) * 1000.0)


def spark_endpoint(fmt: str) -> router.Endpoint:
    key = os.getenv("MODEL_API_KEY", "").strip()
    if not key:
        raise SystemExit("spark baseline needs MODEL_API_KEY")
    # Spark is a reasoning model: thinking tokens count against max_tokens.
    return router.Endpoint(meta_client.base_url(), meta_client.CHAT_MODEL, key, timeout=60.0, max_tokens=2000,
                           response_format=fmt, temperature=0.2)  # the settings run_agent uses


def endpoint_prediction(ep: router.Endpoint, messages: list[dict[str, Any]]) -> Prediction:
    t0 = time.perf_counter()
    try:
        text = ep.complete(messages)
        return Prediction(text, (time.perf_counter() - t0) * 1000.0)
    except Exception as exc:
        return Prediction("", (time.perf_counter() - t0) * 1000.0, f"{type(exc).__name__}: {str(exc)[:120]}")


def run_system(name: str, rows: list[dict[str, Any]], shots: list[dict[str, str]], workers: int = 4,
               spark_format: str = "none") -> list[Prediction]:
    if name == "keywords":
        return [keyword_prediction(r) for r in rows]
    if name == "router":
        if not router.enabled():
            raise SystemExit("router baseline needs ROUTER_BASE_URL and ROUTER_MODEL")
        ep = router.endpoint()
        ep = replace(ep, timeout=max(ep.timeout, 30.0))  # measure slow replies instead of dropping them
    else:
        ep = spark_endpoint(spark_format)
    prompts = [[r["messages"][0], *shots, r["messages"][1]] for r in rows]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(lambda m: endpoint_prediction(ep, m), prompts))


# --------------------------------------------------------------------------- metrics


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def macro_f1(gold: list[str], pred: list[str]) -> float | None:
    """Macro F1 over the labels present in `gold` (an invalid prediction is just a wrong label)."""
    labels = sorted(set(gold))
    if not labels:
        return None
    f1s = []
    for lab in labels:
        tp = sum(g == lab and p == lab for g, p in zip(gold, pred))
        fp = sum(g != lab and p == lab for g, p in zip(gold, pred))
        fn = sum(g == lab and p != lab for g, p in zip(gold, pred))
        f1s.append(0.0 if tp == 0 else 2 * tp / (2 * tp + fp + fn))
    return sum(f1s) / len(f1s)


def mcnemar(a_right: list[bool], b_right: list[bool]) -> dict[str, Any]:
    """Exact two-sided McNemar test on paired right/wrong outcomes."""
    b = sum(x and not y for x, y in zip(a_right, b_right))
    c = sum(y and not x for x, y in zip(a_right, b_right))
    n = b + c
    p = 1.0 if n == 0 else min(1.0, 2 * sum(comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n)
    return {"a_only_right": b, "b_only_right": c, "p_value": p}


def tool_right(gold: dict[str, Any], d: router.Decision | None) -> bool:
    return d is not None and set(d.tools) == set(gold["tools"])


def score(rows: list[dict[str, Any]], preds: list[Prediction], over_weight: float = 2.0) -> dict[str, Any]:
    golds = [gold_of(r) for r in rows]
    decisions = [p.decision for p in preds]
    n = max(1, len(rows))
    out: dict[str, Any] = {
        "rows": len(rows),
        "json_valid": sum(router.is_strict_json(p.text) for p in preds) / n,
        "parsed": sum(d is not None for d in decisions) / n,
        "errors": sum(p.error is not None for p in preds),
        "tool_accuracy": sum(tool_right(g, d) for g, d in zip(golds, decisions)) / n,
    }
    hint_rows = [(g, d) for g, d in zip(golds, decisions) if "give_hint" in g["tools"]]
    diffs = [d.hint_level - g["hint_level"] for g, d in hint_rows if d is not None and "give_hint" in d.tools]
    out["hint_error"] = {
        "rows": len(hint_rows), "scored": len(diffs),
        "weighted": sum(over_weight * x if x > 0 else -x for x in diffs) / len(diffs) if diffs else None,
        "mae": sum(abs(x) for x in diffs) / len(diffs) if diffs else None,
        "over_reveal_rate": sum(x > 0 for x in diffs) / len(diffs) if diffs else None,
    }
    out["answer_leak"] = sum(d is not None and "finish_answer" in d.tools for g, d in hint_rows)
    first = lambda tools: tools[0] if tools else "none"  # noqa: E731
    confusion: dict[str, Counter] = defaultdict(Counter)
    for g, d in zip(golds, decisions):
        confusion[first(g["tools"])][first(list(d.tools)) if d is not None else "invalid"] += 1
    out["confusion_first_tool"] = {k: dict(v) for k, v in sorted(confusion.items())}
    for subset, keep in (("clean", False), ("noisy", True)):
        idx = [i for i, r in enumerate(rows) if bool(r.get("meta", {}).get("noisy")) == keep]
        out[f"intent_macro_f1_{subset}"] = macro_f1(
            [golds[i]["intent"] for i in idx], [decisions[i].intent if decisions[i] else "invalid" for i in idx])
    ms = [p.ms for p in preds if p.error is None]
    out["latency_ms"] = {"p50": percentile(ms, 0.5), "p95": percentile(ms, 0.95)}
    return out


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def print_table(report: dict[str, Any]) -> None:
    systems = list(report["systems"])
    rows = [("json_valid", lambda s: s["json_valid"]), ("parsed", lambda s: s["parsed"]),
            ("tool_accuracy", lambda s: s["tool_accuracy"]), ("hint_error (weighted)", lambda s: s["hint_error"]["weighted"]),
            ("hint_error (mae)", lambda s: s["hint_error"]["mae"]),
            ("over_reveal_rate", lambda s: s["hint_error"]["over_reveal_rate"]),
            ("answer_leak (rows)", lambda s: s["answer_leak"]),
            ("intent_macro_f1 clean", lambda s: s["intent_macro_f1_clean"]),
            ("intent_macro_f1 noisy", lambda s: s["intent_macro_f1_noisy"]),
            ("latency p50 ms", lambda s: s["latency_ms"]["p50"]), ("latency p95 ms", lambda s: s["latency_ms"]["p95"]),
            ("call errors", lambda s: s["errors"])]
    width = max(len(r[0]) for r in rows) + 2
    print("".ljust(width) + "".join(s.rjust(12) for s in systems))
    for name, get in rows:
        print(name.ljust(width) + "".join(_fmt(get(report["systems"][s])).rjust(12) for s in systems))
    for pair, test in report.get("mcnemar", {}).items():
        print(f"McNemar {pair}: router-only right {test['a_only_right']}, other-only right "
              f"{test['b_only_right']}, p = {test['p_value']:.4f}")


def main(argv: list[str] | None = None) -> dict[str, Any]:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default=str(ROOT / "data" / "router" / "test.jsonl"))
    ap.add_argument("--train", default=str(ROOT / "data" / "router" / "train.jsonl"), help="few-shot pool for spark")
    ap.add_argument("--systems", default="router,spark,keywords", type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    ap.add_argument("--limit", type=int, default=0, help="first N test rows (0 = all)")
    ap.add_argument("--few-shot", type=int, default=12, help="examples in the spark prompt")
    ap.add_argument("--router-few-shot", type=int, default=0, help="examples in the router prompt (base, untuned model)")
    ap.add_argument("--spark-format", default="none", choices=("none", "json_object", "json_schema"))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--over-weight", type=float, default=2.0, help="penalty multiplier for a too-high hint level")
    ap.add_argument("--out", default=str(ROOT / "data" / "router" / "eval.json"))
    args = ap.parse_args(argv)
    unknown = [s for s in args.systems if s not in SYSTEMS]
    if unknown:
        ap.error(f"unknown system(s): {', '.join(unknown)}")

    rows = load_jsonl(args.data, args.limit)
    train = load_jsonl(args.train) if Path(args.train).exists() else []
    report: dict[str, Any] = {"data": args.data, "rows": len(rows), "systems": {}, "mcnemar": {}}
    right: dict[str, list[bool]] = {}
    for name in args.systems:
        shots = few_shot(train, args.few_shot if name == "spark" else args.router_few_shot if name == "router" else 0)
        print(f"running {name} on {len(rows)} rows ...", file=sys.stderr)
        preds = run_system(name, rows, shots, args.workers, args.spark_format)
        report["systems"][name] = score(rows, preds, args.over_weight)
        right[name] = [tool_right(gold_of(r), p.decision) for r, p in zip(rows, preds)]
    if "router" in right:
        for other in right:
            if other != "router":
                report["mcnemar"][f"router_vs_{other}"] = mcnemar(right["router"], right[other])
    print_table(report)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    print(f"report written to {args.out}", file=sys.stderr)
    return report


if __name__ == "__main__":
    main()
