#!/usr/bin/env python3
"""Build the training set for Margin's decision router (OpenAI-style `messages` JSONL).

No public dataset labels Margin's decisions (which tool, which hint level, what the command
means), so this script makes them:

1. Seed problems. Calculus subsets of licence-clean datasets: NuminaMath-1.5 (Apache-2.0,
   problem_type == "Calculus", AoPS sources dropped) and DeepMath-103K (MIT, "Calculus" topics),
   plus generated problems (always available, no download). A problem is kept only if SymPy can
   read it as an indefinite integral, a definite integral or a derivative and solve it.
2. Pages. server.checker writes the correct step chain. A misconception catalogue (sign slips,
   chain-rule misses, (uv)' = u'v', lost constants, missing + C, wrong integration-by-parts
   terms, power-rule slips, ...) turns some chains into wrong student work; SymPy then finds the
   first wrong line and names the error. 20-40% of rows keep a fully correct chain.
3. Commands. Templates per intent ("hint", "answer", "check", "typeset", "graph", "verify",
   "other", two-tool combos, empty), with wake words and simulated speech-recognition noise.
4. Labels. {intent, tools, hint_level, confidence} from rules, for clear cases only.

The router's input is server.router.build_state() and its prompt server.router.SYSTEM_PROMPT,
so training rows and live requests have one schema.

The test split is held out three ways: by problem type, by mistake type and by command phrasing.

Output (default data/router/, git-ignored; never commit downloaded data):
    train.jsonl     {"messages": [system, user, assistant]}   upload as is to Fireworks or Unsloth
    test.jsonl      the same plus "meta" (problem type, mistake, error, noisy, ...) for the eval
    manifest.json   counts, sources and licences, held-out sets

Run (from the repo root):
    uv run --no-project -p 3.11 --with-requirements requirements.txt --with duckdb \\
        python scripts/build_router_data.py
    ... python scripts/build_router_data.py --sources synthetic     # offline, no downloads

Downloads use the Hugging Face datasets-server filter API, or DuckDB over the dataset's parquet
files when that is installed (faster and more reliable for the full subsets). Raw rows are
cached in data/raw/.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
from pathlib import Path
import random
import re
import signal
import sys
import threading
import time
from typing import Any, Callable, Iterator
import warnings

# Dataset text such as "f(x)(x+1)" makes Python's compiler warn inside SymPy's parser.
warnings.filterwarnings("ignore", category=SyntaxWarning)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sympy as sp  # noqa: E402

from server import checker, router, skills  # noqa: E402

X = checker.X
DATASETS_SERVER = "https://datasets-server.huggingface.co"

# --------------------------------------------------------------------------- sources

SOURCES: dict[str, dict[str, Any]] = {
    "numina": {
        "dataset": "project-numina/NuminaMath-1.5", "licence": "Apache-2.0",
        "where": "\"problem_type\"='Calculus'", "columns": ["problem", "source", "problem_is_valid"],
        "text": "problem",
        # AoPS sent a DMCA notice for MATH in 2025: drop every row traced to it.
        "keep": lambda r: "aops" not in str(r.get("source", "")).lower() and r.get("problem_is_valid") != "No",
    },
    "deepmath": {
        "dataset": "zwhe99/DeepMath-103K", "licence": "MIT",
        "where": "\"topic\" LIKE '%Calculus%'", "columns": ["question", "topic"],
        "text": "question",
        "keep": lambda r: "Calculus" in str(r.get("topic", "")),
    },
}
SYNTHETIC_LICENCE = "generated in this repo"


def _get_json(url: str, params: dict[str, Any], tries: int = 5) -> dict[str, Any]:
    """GET a datasets-server endpoint, retrying its frequent "index is loading" answers."""
    import httpx

    delay = 5.0
    for attempt in range(tries):
        try:
            data = httpx.get(url, params=params, timeout=90.0).json()
        except Exception as exc:  # network blips, non-JSON error pages
            data = {"error": f"{type(exc).__name__}: {exc}"}
        if "error" not in data:
            return data
        if attempt == tries - 1:
            raise RuntimeError(str(data["error"]))
        print(f"  datasets-server: {str(data['error'])[:80]} (retry in {delay:.0f}s)", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2, 60.0)
    raise RuntimeError("unreachable")


def _fetch_filter_api(spec: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while len(rows) < limit:
        data = _get_json(f"{DATASETS_SERVER}/filter", {
            "dataset": spec["dataset"], "config": "default", "split": "train",
            "where": spec["where"], "offset": offset, "length": 100})
        page = [r["row"] for r in data.get("rows", [])]
        if not page:
            break
        rows.extend({k: r.get(k) for k in spec["columns"]} for r in page)
        offset += len(page)
        total = data.get("num_rows_total")
        if total is not None and offset >= int(total):
            break
    return rows[:limit]


def _fetch_duckdb(spec: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    import duckdb  # optional: uv run --with duckdb

    files = _get_json(f"{DATASETS_SERVER}/parquet", {"dataset": spec["dataset"]})["parquet_files"]
    urls = [f["url"] for f in files if f["split"] == "train"]
    con = duckdb.connect()
    con.sql("INSTALL httpfs; LOAD httpfs;")
    cols = ", ".join(f'"{c}"' for c in spec["columns"])
    cur = con.execute(f"SELECT {cols} FROM read_parquet(?) WHERE {spec['where']} LIMIT {int(limit)}", [urls])
    return [dict(zip(spec["columns"], row)) for row in cur.fetchall()]


def load_source(name: str, limit: int, raw_dir: Path) -> list[dict[str, Any]]:
    """Calculus rows of one dataset (cached in raw_dir); [] if it cannot be downloaded."""
    spec = SOURCES[name]
    cache, info = raw_dir / f"{name}.jsonl", raw_dir / f"{name}.info.json"
    if cache.exists():
        rows = [json.loads(l) for l in cache.read_text().splitlines() if l.strip()]
        asked = json.loads(info.read_text()).get("limit", 0) if info.exists() else 0
        if len(rows) >= limit or asked >= limit:  # enough rows, or the subset has no more
            return rows[:limit]
    rows = []
    try:
        import duckdb  # noqa: F401
        fetchers: list[Callable[..., list[dict[str, Any]]]] = [_fetch_duckdb, _fetch_filter_api]
    except ImportError:
        fetchers = [_fetch_filter_api]
    for fetch in fetchers:
        try:
            print(f"downloading {spec['dataset']} via {fetch.__name__[7:]} ...", file=sys.stderr)
            rows = fetch(spec, limit)
            break
        except Exception as exc:
            print(f"  {fetch.__name__[7:]} failed: {str(exc)[:160]}", file=sys.stderr)
    if rows:
        raw_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        info.write_text(json.dumps({"dataset": spec["dataset"], "where": spec["where"], "limit": limit,
                                    "rows": len(rows), "licence": spec["licence"]}) + "\n")
    else:
        print(f"  no rows from {name}; continuing without it", file=sys.stderr)
    return rows


# --------------------------------------------------------------------------- seeds


@dataclass
class Seed:
    sid: str
    source: str
    kind: str  # integral | definite | derivative
    f: sp.Expr  # the integrand, or the function to differentiate
    limits: tuple[sp.Expr, sp.Expr] | None = None
    ptype: str = ""


class SympyTimeout(BaseException):
    """Not an Exception: SymPy and the checker catch Exception broadly and would swallow it."""


@contextmanager
def time_limit(seconds: float) -> Iterator[None]:
    """Abort slow SymPy work (SIGALRM: main thread only; no limit elsewhere)."""
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _raise(signum, frame):
        raise SympyTimeout

    old = signal.signal(signal.SIGALRM, _raise)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _is_power_sum(f: sp.Expr) -> bool:
    return all(checker._monomial(t, X) is not None for t in sp.Add.make_args(sp.expand(f)))


def classify(kind: str, f: sp.Expr) -> str:
    """Problem type: power, trig, exp_log, product, chain, quotient, by_parts, u_sub, definite."""
    if kind == "definite":
        return "definite"
    inner = checker._inner_args(f, X)
    nonlinear = [a for a in inner if not (a.is_polynomial(X) and sp.degree(a, X) <= 1)]
    if kind == "derivative":
        num, den = sp.fraction(sp.together(f))
        if X in den.free_symbols and X in num.free_symbols:
            return "quotient"
        if checker._product_split(f, X):
            return "product"
        if nonlinear:
            return "chain"
    else:
        if checker._parts_split(f, X):
            return "by_parts"
        if nonlinear and checker.u_sub(f, X) is not None:
            return "u_sub"
    if _is_power_sum(f):
        return "power"
    if f.has(sp.sin, sp.cos, sp.tan):
        return "trig"
    return "exp_log"


def _synthetic_templates() -> list[tuple[str, Callable[[random.Random], Any]]]:
    x, e, s, c, ln = X, sp.exp, sp.sin, sp.cos, sp.log
    k = lambda r: r.choice([2, 3, 4, 5])  # noqa: E731
    n = lambda r: r.choice([2, 3, 4])  # noqa: E731
    return [
        # indefinite integrals
        ("integral", lambda r: k(r) * x ** n(r) + r.choice([1, 2, 3]) * x + r.choice([1, 2, 5])),
        ("integral", lambda r: x ** n(r) - k(r) * x),
        ("integral", lambda r: c(k(r) * x)),
        ("integral", lambda r: s(k(r) * x)),
        ("integral", lambda r: e(k(r) * x)),
        ("integral", lambda r: e(x) + k(r) * x),
        ("integral", lambda r: k(r) * c(x) - s(x)),
        ("integral", lambda r: k(r) / x),
        ("integral", lambda r: x * c(r.choice([1, 2, 3]) * x)),
        ("integral", lambda r: x * s(r.choice([1, 2, 3]) * x)),
        ("integral", lambda r: x * e(r.choice([1, 2, 3]) * x)),
        ("integral", lambda r: x ** 2 * e(x)),
        ("integral", lambda r: x ** r.choice([1, 2]) * ln(x)),
        ("integral", lambda r: ln(x)),
        ("integral", lambda r: 2 * x * e(x ** 2)),
        ("integral", lambda r: x * (x ** 2 + 1) ** n(r)),
        ("integral", lambda r: 3 * x ** 2 * (x ** 3 + r.choice([1, 2])) ** n(r)),
        ("integral", lambda r: s(x) * c(x)),
        ("integral", lambda r: x * c(x ** 2)),
        ("integral", lambda r: c(x) * e(s(x))),
        # definite integrals
        ("definite", lambda r: (k(r) * x ** n(r) + 1, (0, r.choice([1, 2, 3])))),
        ("definite", lambda r: (x ** 2, (1, r.choice([2, 3])))),
        ("definite", lambda r: (2 * x + r.choice([1, 3]), (1, 3))),
        ("definite", lambda r: (s(x), (0, sp.pi))),
        ("definite", lambda r: (c(x), (0, sp.pi / 2))),
        ("definite", lambda r: (e(x), (0, 1))),
        ("definite", lambda r: (1 / x, (1, r.choice([2, 3, sp.E])))),
        # derivatives
        ("derivative", lambda r: k(r) * x ** n(r) + r.choice([1, 2, 5]) * x - 7),
        ("derivative", lambda r: x ** n(r) - k(r) * x ** 2),
        ("derivative", lambda r: s(k(r) * x)),
        ("derivative", lambda r: c(k(r) * x)),
        ("derivative", lambda r: e(k(r) * x)),
        ("derivative", lambda r: e(x) + x ** n(r)),
        ("derivative", lambda r: k(r) * e(x)),
        ("derivative", lambda r: ln(x) + k(r) * x),
        ("derivative", lambda r: x ** n(r) * s(x)),
        ("derivative", lambda r: x ** n(r) * c(x)),
        ("derivative", lambda r: x ** n(r) * e(x)),
        ("derivative", lambda r: x ** n(r) * ln(x)),
        ("derivative", lambda r: e(x) * s(x)),
        ("derivative", lambda r: (x ** 2 + 1) ** n(r)),
        ("derivative", lambda r: (k(r) * x ** 2 - 1) ** n(r)),
        ("derivative", lambda r: s(x ** 2)),
        ("derivative", lambda r: e(x ** 2)),
        ("derivative", lambda r: ln(x ** 2 + 1)),
        ("derivative", lambda r: c(x ** n(r))),
        ("derivative", lambda r: s(x) / x),
        ("derivative", lambda r: x / (x ** 2 + 1)),
        ("derivative", lambda r: e(x) / x),
        ("derivative", lambda r: ln(x) / x),
        ("derivative", lambda r: (x + 1) / (x - 1)),
    ]


def synthetic_seeds(rng: random.Random, draws: int = 4) -> list[Seed]:
    seeds, seen = [], set()
    for t, (kind, make) in enumerate(_synthetic_templates()):
        for _ in range(draws):
            val = make(rng)
            f, limits = (val[0], tuple(sp.sympify(v) for v in val[1])) if kind == "definite" else (val, None)
            key = (kind, sp.srepr(f), limits)
            if key in seen:
                continue
            seen.add(key)
            seeds.append(Seed(f"syn-{t}-{len(seeds)}", "synthetic", kind, f, limits))
    return seeds


# LaTeX in dataset problems -> the plain text checker.parse_line reads.
_LATEX_SUBS = [
    (r"\\[dt]frac", r"\\frac"), (r"\\left|\\right|\\displaystyle|\\limits", ""),
    (r"\\(?:mathrm|operatorname|text|mathit)\s*\{([^{}]*)\}", r"\1"), (r"\\[,;!:]|\\quad|\\qquad|~", " "),
    (r"\\cdot|\\times", "*"),
]
# Innermost groups first, so nested braces (\frac{x^{2}+1}{e^{x}}) unwind one level per pass.
_LATEX_GROUPS = [
    (r"\\sqrt\s*\[\s*(\w+)\s*\]\s*\{([^{}]*)\}", r"((\2)^(1/\1))"), (r"\\sqrt\s*\{([^{}]*)\}", r"sqrt(\1)"),
    (r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"((\1)/(\2))"), (r"([\^_])\s*\{([^{}]*)\}", r"\1(\2)"),
]


def latex_to_text(s: str) -> str | None:
    for pat, rep in _LATEX_SUBS:
        s = re.sub(pat, rep, s)
    for _ in range(6):
        before = s
        for pat, rep in _LATEX_GROUPS:
            s = re.sub(pat, rep, s)
        if s == before:
            break
    s = re.sub(r"\\(arcsin|arccos|arctan|sinh|cosh|tanh|sin|cos|tan|sec|csc|cot|ln|log|exp|pi)\b", r" \1 ", s)
    s = s.replace("{", "(").replace("}", ")")
    if "\\" in s or len(s) > 160:
        return None
    return re.sub(r"\s+", " ", s).strip(" ,.;$")


_MATH = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$|\\\((.+?)\\\)|\\\[(.+?)\\\]", re.S)
_INT_INDEF = re.compile(r"\\int(?!_)\s*(.+?)\s*\\?,?\s*(?:\\mathrm\{d\}|\{\\rm d\}|d)\s*([a-z])\b")
_INT_DEF = re.compile(r"\\int_\s*(\{[^{}]+\}|-?\w+)\s*\^\s*(\{[^{}]+\}|-?\w+)\s*(.+?)\s*\\?,?\s*(?:\\mathrm\{d\}|d)\s*([a-z])\b")
_FUNC_DEF = re.compile(r"(?:^|[^a-zA-Z])(?:[fgh]\s*\(\s*([a-z])\s*\)|y)\s*=\s*(.+)$")
_DERIV_WORDS = re.compile(r"derivative|differentiat|f\s*'\s*\(|f\s*′|y\s*'|\\frac\s*\{\s*d\s*y\s*\}", re.I)


def _single_var(expr: sp.Expr | None, var: str | None = None) -> sp.Expr | None:
    if expr is None or not isinstance(expr, sp.Expr) or expr.has(sp.Integral, sp.Derivative):
        return None
    syms = expr.free_symbols
    if len(syms) != 1 or (var and str(next(iter(syms))) != var):
        return None
    v = next(iter(syms))
    if str(v) in ("e", "C", "c", "k", "a", "b", "n", "m"):
        return None
    f = expr.subs(v, X)
    return f if sp.count_ops(f) <= 25 else None


def _parse_text(text: str | None) -> sp.Expr | None:
    if not text:
        return None
    p = checker.parse_line(text)
    return p.parts[0] if p.ok and len(p.parts) == 1 and not p.continuation else None


def extract_seed(text: str, source: str, sid: str) -> Seed | None:
    """A Seed from a word problem's LaTeX, or None when it is not a plain integral/derivative."""
    for m in _MATH.finditer(text or ""):
        math = next(g for g in m.groups() if g is not None)
        dm = _INT_DEF.search(math)
        if dm:
            a, b = (_parse_text(latex_to_text(g.strip("{}"))) for g in dm.group(1, 2))
            f = _single_var(_parse_text(latex_to_text(dm.group(3))), dm.group(4))
            if f is not None and a is not None and b is not None and not (a.free_symbols or b.free_symbols):
                return Seed(sid, source, "definite", f, (a, b))
            continue
        im = _INT_INDEF.search(math)
        if im:
            f = _single_var(_parse_text(latex_to_text(im.group(1))), im.group(2))
            if f is not None:
                return Seed(sid, source, "integral", f)
            continue
        if _DERIV_WORDS.search(text):
            fm = _FUNC_DEF.search(math)
            if fm:
                f = _single_var(_parse_text(latex_to_text(fm.group(2))), fm.group(1) or None)
                if f is not None and not _is_trivial(f):
                    return Seed(sid, source, "derivative", f)
    return None


def _is_trivial(f: sp.Expr) -> bool:
    return f.is_polynomial(X) and sp.degree(f, X) <= 1


def dataset_seeds(name: str, rows: list[dict[str, Any]]) -> list[Seed]:
    spec, seeds, seen = SOURCES[name], [], set()
    for i, row in enumerate(rows):
        if not spec["keep"](row):
            continue
        try:
            with time_limit(2.0):
                seed = extract_seed(str(row.get(spec["text"]) or ""), name, f"{name}-{i}")
        except (SympyTimeout, Exception):  # anything SymPy throws on odd input
            continue
        if seed is None:
            continue
        key = (seed.kind, sp.srepr(seed.f), seed.limits)
        if key not in seen:
            seen.add(key)
            seeds.append(seed)
    return seeds


# --------------------------------------------------------------------------- chains & mistakes


def problem_line(seed: Seed) -> str:
    if seed.kind == "integral":
        return checker.pretty(sp.Integral(seed.f, X))
    if seed.kind == "definite":
        return checker.pretty(sp.Integral(seed.f, (X, *seed.limits)))
    return f"d/dx ({checker.pretty(seed.f)})"


def correct_chain(seed: Seed) -> list[str] | None:
    """[problem, step, ..., answer] with every step ticked by the checker, or None."""
    first = problem_line(seed)
    chain = [first] + checker.solve_remaining([first])
    if len(chain) < 2:
        return None
    res = checker.check_lines(chain)
    if res.first_wrong is not None or not res.finished or any(r.status == "unchecked" for r in res.lines):
        return None
    return chain


def _last_value(chain: list[str]) -> tuple[sp.Expr, bool]:
    p = checker.parse_line(chain[-1])
    return p.parts[-1], p.has_c[-1]


def _line(expr: sp.Expr, plus_c: bool) -> str:
    return f"= {checker.pretty(expr)}" + (" + C" if plus_c else "")


def _replace_last(chain: list[str], expr: sp.Expr) -> list[str]:
    _, has_c = _last_value(chain)
    return chain[:-1] + [_line(expr, has_c)]


def _sign_flip(seed, chain, rng):
    value, _ = _last_value(chain)
    terms = sp.Add.make_args(value)
    wrong = value - 2 * rng.choice(terms) if len(terms) > 1 else -value
    return _replace_last(chain, wrong)


def _chain_rule_miss(seed, chain, rng):
    inner = checker._inner_args(seed.f, X)
    if seed.kind != "derivative" or not inner:
        return None
    h = rng.choice(inner)
    return [chain[0], _line(sp.simplify(sp.diff(seed.f, X) / sp.diff(h, X)), False)]


def _product_split(seed):
    return checker._product_split(seed.f, X) if seed.kind == "derivative" else None


def _product_uv(seed, chain, rng):
    split = _product_split(seed)
    if not split:
        return None
    u, v = split
    return [chain[0], _line(sp.diff(u, X) * sp.diff(v, X), False)]


def _product_one_term(seed, chain, rng):
    split = _product_split(seed)
    if not split:
        return None
    u, v = split
    return [chain[0], _line(rng.choice([sp.diff(u, X) * v, u * sp.diff(v, X)]), False)]


def _quotient_sign(seed, chain, rng):
    num, den = sp.fraction(sp.together(seed.f))
    if seed.kind != "derivative" or X not in den.free_symbols or X not in num.free_symbols:
        return None
    wrong = (sp.diff(num, X) * den + num * sp.diff(den, X)) / den ** 2
    return [chain[0], _line(wrong, False)]


def _lost_factor(seed, chain, rng):
    if seed.kind != "integral":
        return None
    value, _ = _last_value(chain)
    ks = [sp.diff(a, X) for a in checker._inner_args(seed.f, X) if not sp.diff(a, X).free_symbols]
    k = ks[0] if ks else rng.choice([2, 3, sp.Rational(1, 2)])
    return _replace_last(chain, sp.expand(value * k))


def _missing_c(seed, chain, rng):
    if seed.kind != "integral":
        return None
    value, has_c = _last_value(chain)
    return chain[:-1] + [_line(value, False)] if has_c else None


def _ibp_split(seed, chain):
    """(uv part, integral part) of the integration-by-parts line, or None."""
    if seed.kind != "integral" or len(chain) < 3 or not checker._parts_split(seed.f, X):
        return None
    p = checker.parse_line(chain[1])
    if not p.ok:
        return None
    rest, integral = p.parts[-1].as_independent(sp.Integral, as_Add=True)
    return (rest, integral) if integral != 0 else None


def _ibp_sign(seed, chain, rng):
    split = _ibp_split(seed, chain)
    if not split:
        return None
    rest, integral = split
    return [chain[0], _line(rest - integral, False)] + chain[2:3] * rng.choice([0, 1])


def _ibp_wrong_term(seed, chain, rng):
    """Used dv instead of v in the new integral: ∫ x cos x dx = x sin x - ∫ cos x dx."""
    split = _ibp_split(seed, chain)
    if not split:
        return None
    u, dv = checker._parts_split(seed.f, X)
    rest, _ = split
    return [chain[0], _line(rest - sp.Integral(sp.diff(u, X) * dv, X), False)]


def _power_rule_exp(seed, chain, rng):
    """Power rule applied to e^x: d/dx e^x = x e^(x-1), ∫ e^x dx = e^(x+1)/(x+1)."""
    terms = [t for t in sp.Add.make_args(sp.expand(seed.f)) if t.as_independent(X)[1] == sp.exp(X)]
    if not terms or seed.kind == "definite":
        return None
    t = terms[0]
    c = t.as_independent(X)[0]
    if seed.kind == "derivative":
        wrong = sp.diff(seed.f - t, X) + c * X * sp.exp(X - 1)
        return [chain[0], _line(wrong, False)]
    value, _ = _last_value(chain)
    return _replace_last(chain, value - c * sp.exp(X) + c * sp.exp(X + 1) / (X + 1))


def _power_rule_slip(seed, chain, rng):
    if seed.kind == "definite" or not _is_power_sum(seed.f):
        return None
    terms = [t for t in sp.Add.make_args(sp.expand(seed.f)) if checker._monomial(t, X)[1] not in (0, 1)]
    if not terms:
        return None
    a, nn = checker._monomial(rng.choice(terms), X)
    t = a * X ** nn
    if seed.kind == "derivative":
        slip = rng.choice([a * X ** (nn - 1), a * nn * X ** nn])  # forgot to bring down / to lower
        return [chain[0], _line(sp.diff(seed.f - t, X) + slip, False)]
    value, _ = _last_value(chain)
    return _replace_last(chain, value - sp.integrate(t, X) + a * X ** (nn + 1))  # forgot to divide


def _differentiated(seed, chain, rng):
    if seed.kind != "integral" or len(chain) > 2:
        return None
    return [chain[0], _line(sp.diff(seed.f, X), True)]


def _limits_swapped(seed, chain, rng):
    if seed.kind != "definite":
        return None
    value, _ = _last_value(chain)
    return _replace_last(chain, -value) if value != 0 else None


def _lower_limit_dropped(seed, chain, rng):
    if seed.kind != "definite":
        return None
    a, b = seed.limits
    F = sp.integrate(seed.f, X)
    if sp.simplify(F.subs(X, a)) == 0:
        return None
    return _replace_last(chain, sp.simplify(F.subs(X, b)))


# name -> perturbation(seed, chain, rng) -> new page lines, or None when it does not apply
MISTAKES: dict[str, Callable[[Seed, list[str], random.Random], list[str] | None]] = {
    "sign_flip": _sign_flip, "chain_rule_miss": _chain_rule_miss, "product_uv": _product_uv,
    "product_one_term": _product_one_term, "quotient_sign": _quotient_sign, "lost_factor": _lost_factor,
    "missing_c": _missing_c, "ibp_sign": _ibp_sign, "ibp_wrong_term": _ibp_wrong_term,
    "power_rule_exp": _power_rule_exp, "power_rule_slip": _power_rule_slip, "differentiated": _differentiated,
    "limits_swapped": _limits_swapped, "lower_limit_dropped": _lower_limit_dropped,
}


@dataclass
class Page:
    seed: Seed
    lines: list[str]
    mistake: str | None  # None: every line on the page is correct
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def correct(self) -> bool:
        return self.mistake is None


def _checked(seed: Seed, lines: list[str], mistake: str | None) -> Page | None:
    res = checker.check_lines(lines)
    state = router.page_state(res)
    if mistake is None and (res.first_wrong or state["error"] or state["unreadable"]):
        return None
    if mistake == "missing_c" and state["error"] != "missing_c":
        return None
    if mistake not in (None, "missing_c") and res.first_wrong is None:
        return None  # the perturbation happened to be correct (or unreadable)
    return Page(seed, lines, mistake, state)


def pages_for(seed: Seed, rng: random.Random, per_seed_timeout: float = 4.0) -> list[Page]:
    """Correct pages (finished and part-way) and one wrong page per mistake that applies."""
    pages: list[Page] = []
    try:
        with time_limit(per_seed_timeout):
            seed.ptype = seed.ptype or classify(seed.kind, seed.f)
            chain = correct_chain(seed)
            if chain is None:
                return []
            for k in range(1, len(chain) + 1):  # [problem], ..., the full solution
                page = _checked(seed, chain[:k], None)
                if page:
                    pages.append(page)
            for name, perturb in MISTAKES.items():
                lines = perturb(seed, chain, rng)
                if lines and lines != chain:
                    page = _checked(seed, lines, name)
                    if page:
                        pages.append(page)
    except (SympyTimeout, Exception):  # too slow, or SymPy failing on an unusual expression
        return pages
    return pages


# --------------------------------------------------------------------------- commands

# intent -> [(template, escalate)]. {thing}: integral / derivative / problem; {line}: a line name.
COMMANDS: dict[str, list[tuple[str, bool]]] = {
    "hint": [
        ("help me with this {thing}", False), ("I'm stuck", False), ("what's the next step", False),
        ("give me a hint", False), ("can you give me a nudge", False), ("where did I go wrong", False),
        ("what should I do next", False), ("I don't get this part", False), ("how do I start this one", False),
        ("any tips?", False), ("point me in the right direction", False), ("what do I do now", False),
        ("I'm lost on {line}", False), ("can you help", False), ("a little help please", False),
        ("what am I missing here", False), ("help", False), ("how should I approach this {thing}", False),
        ("is there a trick for this {thing}", False), ("which rule do I use here", False),
        ("give me a bigger hint", True), ("more help please", True), ("still stuck", True),
        ("I still don't get it", True), ("can you be more specific", True), ("that didn't help, another hint", True),
    ],
    "answer": [
        ("just give me the answer", False), ("what's the answer", False), ("show me the solution", False),
        ("finish it for me", False), ("solve it", False), ("do the rest", False), ("can you finish this", False),
        ("write out the rest of the steps", False), ("I give up, show me", False), ("work it out for me", False),
        ("complete the problem", False), ("tell me the final answer", False), ("show me how it ends", False),
        ("do it for me", False), ("fill in the remaining steps", False), ("what does this {thing} come out to", False),
    ],
    "check": [
        ("is this right", False), ("check my work", False), ("did I get it right", False), ("am I right", False),
        ("is my answer correct", False), ("can you check this", False), ("look over my steps", False),
        ("any mistakes?", False), ("is {line} correct", False), ("did I make an error", False),
        ("mark my work", False), ("how did I do", False), ("verify my steps", False), ("is this okay", False),
        ("grade this {thing}", False), ("does this look right to you", False),
    ],
    "typeset": [
        ("tidy this up", False), ("typeset the page", False), ("make it neat", False), ("type it up", False),
        ("clean up my handwriting", False), ("write it out properly", False), ("make this readable", False),
        ("convert this to typed math", False), ("neaten the page", False), ("format this nicely", False),
        ("put this in latex", False), ("can you make it look neat", False),
    ],
    "graph": [
        ("graph it", False), ("plot the function", False), ("sketch the curve", False), ("show me a graph", False),
        ("draw the graph", False), ("what does it look like", False), ("plot this for me", False),
        ("can I see the curve", False), ("sketch it and its antiderivative", False), ("visualize this", False),
        ("draw it", False), ("make a plot of this {thing}", False),
    ],
    "verify": [
        ("check it by differentiating", False), ("differentiate my answer to check", False),
        ("take the derivative to verify", False), ("does the derivative give back the integrand", False),
        ("verify by differentiating", False), ("check my antiderivative by taking d/dx", False),
        ("if I differentiate this do I get the original", False), ("confirm it by differentiating", False),
        ("check by taking the derivative of my answer", False), ("differentiate it back", False),
    ],
    "other": [
        ("what's the weather", False), ("thanks", False), ("thank you meta", False), ("never mind", False),
        ("set a timer for ten minutes", False), ("play some music", False), ("who are you", False),
        ("how are you", False), ("what time is it", False), ("tell me a joke", False), ("cancel", False),
        ("open my calendar", False), ("good night", False), ("call mom", False),
    ],
}
# (template, intent, tools): commands that ask for two things, in the order asked.
COMBOS: list[tuple[str, str, list[str]]] = [
    ("check my work and graph it", "check", ["check_steps", "sketch_graph"]),
    ("tidy this up and check it", "typeset", ["typeset_lines", "check_steps"]),
    ("give me a hint and plot the function", "hint", ["give_hint", "sketch_graph"]),
    ("type it up and sketch the graph", "typeset", ["typeset_lines", "sketch_graph"]),
    ("is this right? also draw the curve", "check", ["check_steps", "sketch_graph"]),
    ("graph it and tell me if I'm right", "graph", ["sketch_graph", "check_steps"]),
    ("check it and then differentiate to verify", "check", ["check_steps", "differentiate_check"]),
    ("plot it and neaten the page", "graph", ["sketch_graph", "typeset_lines"]),
]
WAKE = ["", "", "", "hey meta ", "hey meta, ", "meta, ", "ok meta ", "um "]
TAIL = ["", "", "", "?", " please", " thanks"]
INTENT_WEIGHTS = {"hint": 0.30, "check": 0.13, "answer": 0.12, "typeset": 0.08, "graph": 0.08,
                  "verify": 0.08, "other": 0.07, "combo": 0.07, "empty": 0.07}

# Words speech recognisers commonly mishear in this domain.
HOMOPHONES = {
    "integral": ["in a girl", "integer all", "intern gral"], "integrate": ["in to great", "integrates"],
    "derivative": ["the rivet of", "derivatives", "derivate"], "differentiate": ["different she ate", "differentiated"],
    "differentiating": ["different she ating", "differentiation"], "graph": ["graft", "grass", "giraffe"],
    "plot": ["plod", "blot"], "sketch": ["sketched", "catch"], "hint": ["hind", "hand", "hit"],
    "check": ["chuck", "czech", "chick"], "answer": ["ants are", "answers", "cancer"],
    "typeset": ["type set", "tie pset"], "tidy": ["tiny", "tidied"], "meta": ["metta", "better", "matter"],
    "solution": ["pollution", "solutions"], "finish": ["finnish"], "verify": ["very fine", "verified"],
    "right": ["write", "rite"], "stuck": ["stock", "struck"], "curve": ["curb"], "step": ["stop", "steps"],
    "line": ["lion"], "neat": ["need", "neet"], "help": ["health", "hell"], "solve": ["salve", "solved"],
}
FILLERS = ["um", "uh", "like", "so", "okay so", "hmm"]


def asr_noise(text: str, rng: random.Random) -> str:
    """Simulated speech recognition: no punctuation, misheard words, fillers, drops, cut-offs."""
    words = re.sub(r"[^\w\s/']", " ", text.lower()).split()
    out: list[str] = []
    for w in words:
        if w in HOMOPHONES and rng.random() < 0.45:
            out.extend(rng.choice(HOMOPHONES[w]).split())
        elif len(words) > 3 and rng.random() < 0.06:
            continue  # dropped word
        else:
            out.append(w)
            if rng.random() < 0.04:
                out.append(w)  # stutter
    if rng.random() < 0.3:
        out.insert(rng.randrange(len(out) + 1), rng.choice(FILLERS))
    if len(out) > 3 and rng.random() < 0.1:
        out[-1] = out[-1][: max(1, len(out[-1]) // 2)]  # cut off mid-word
    noisy = " ".join(out)
    return noisy if noisy != text else f"{rng.choice(FILLERS)} {noisy}"


def render(template: str, page: Page, rng: random.Random) -> str:
    thing = {"integral": "integral", "definite": "integral", "derivative": "derivative"}.get(page.seed.kind, "problem")
    n = page.state.get("first_wrong_index", -1)
    line = f"line {n + 1}" if n >= 0 and rng.random() < 0.5 else rng.choice(["this line", "the last line", "line 2"])
    return template.format(thing=rng.choice([thing, thing, "problem", "one"]), line=line)


# --------------------------------------------------------------------------- labels


def hint_level(session: dict[str, Any], escalate: bool) -> int:
    """Start at 1; one above hints_given after a new attempt or a request for more; else repeat."""
    given = session["hints_given"]
    if given == 0:
        return 1
    return max(1, min(3, given + 1 if (session["wrote_since_hint"] or escalate) else given))


def label(intent: str, tools: list[str], page: Page, session: dict[str, Any], escalate: bool,
          empty: bool, noisy: bool) -> dict[str, Any]:
    """The rule labels. Every case here is unambiguous; ambiguous phrasing is not generated."""
    if empty:  # a bare "hey meta" or the Ask button: help with the page
        done = page.state["finished"] and page.state["first_wrong"] is None and not page.state["error"]
        intent = "check" if done else "hint"
        tools = [skills.INTENT_TOOL[intent]]
    level = hint_level(session, escalate) if "give_hint" in tools else 1
    confidence = 0.8 if empty else (0.85 if noisy else 0.95)
    return {"intent": intent, "tools": tools, "hint_level": level, "confidence": confidence}


def sample_session(rng: random.Random, escalate: bool, page: Page,
                   templates: dict[str, list[tuple[str, bool]]]) -> dict[str, Any]:
    """Line history. last_command uses the split's own phrasings, so test phrasings stay unseen."""
    given = rng.choice([1, 1, 2]) if escalate else rng.choices([0, 1, 2, 3], [0.6, 0.25, 0.12, 0.03])[0]
    wrote = given > 0 and rng.random() < 0.5
    last = ""
    if given > 0:  # the hint they already got came from a request for help
        last = render(rng.choice([t for t, esc in templates["hint"] if not esc]), page, rng)
    elif rng.random() < 0.2:
        last = render(rng.choice([t for i in ("check", "typeset", "graph") for t, _ in templates[i]]), page, rng)
    return router.session_state(given, wrote, last)


# --------------------------------------------------------------------------- dataset


@dataclass
class Split:
    pages_correct: list[Page]
    pages_wrong: list[Page]
    templates: dict[str, list[tuple[str, bool]]]
    combos: list[tuple[str, str, list[str]]]


def held_out_templates(rng: random.Random, fraction: float) -> tuple[dict, dict, list, list]:
    """(train templates, test templates, train combos, test combos): unseen phrasings for the test set."""
    train, test = {}, {}
    for intent, items in COMMANDS.items():
        items = items[:]
        rng.shuffle(items)
        k = max(1, round(len(items) * fraction))
        # keep at least one escalation phrasing on each side
        test[intent], train[intent] = items[:k], items[k:]
        for side, other in ((train, test), (test, train)):
            if any(esc for _, esc in COMMANDS[intent]) and not any(esc for _, esc in side[intent]):
                esc = next(t for t in other[intent] if t[1])
                other[intent].remove(esc)
                side[intent].append(esc)
    combos = COMBOS[:]
    rng.shuffle(combos)
    k = max(1, round(len(combos) * fraction))
    return train, test, combos[k:], combos[:k]


def make_row(page: Page, split: Split, rng: random.Random, noisy_ratio: float) -> dict[str, Any]:
    kind = rng.choices(list(INTENT_WEIGHTS), list(INTENT_WEIGHTS.values()))[0]
    escalate, empty = False, kind == "empty"
    if empty:
        intent, tools, text, template = "hint", ["give_hint"], rng.choice(["", "hey meta", "meta"]), "(empty)"
    elif kind == "combo":
        template, intent, tools = rng.choice(split.combos)
        text = render(template, page, rng)
    else:
        intent = kind
        template, escalate = rng.choice(split.templates[intent])
        tools = [skills.INTENT_TOOL[intent]] if intent != "other" else []
        text = render(template, page, rng)
    if not empty:
        text = (rng.choice(WAKE) + text + rng.choice(TAIL)).strip()
    noisy = not empty and rng.random() < noisy_ratio
    if noisy:
        text = asr_noise(text, rng)
    session = sample_session(rng, escalate, page, split.templates)
    target = label(intent, list(tools), page, session, escalate, empty, noisy)
    state = {"command": text, "page": page.state, "session": session}
    messages = router.messages_for(state) + [{"role": "assistant", "content": json.dumps(target)}]
    meta = {"source": page.seed.source, "seed": page.seed.sid, "problem": page.lines[0], "ptype": page.seed.ptype,
            "mistake": page.mistake, "error": page.state["error"], "correct_chain": page.correct,
            "template": template, "noisy": noisy, "empty": empty, "escalate": escalate, "command_kind": kind}
    return {"messages": messages, "meta": meta}


def sample_rows(n: int, split: Split, rng: random.Random, correct_ratio: float, noisy_ratio: float,
                seen: set[str] | None = None) -> list[dict[str, Any]]:
    rows, seen = [], seen if seen is not None else set()
    tries = 0
    while len(rows) < n and tries < n * 20:
        tries += 1
        pool = split.pages_correct if (rng.random() < correct_ratio or not split.pages_wrong) else split.pages_wrong
        if not pool:
            break
        row = make_row(rng.choice(pool), split, rng, noisy_ratio)
        key = row["messages"][1]["content"]
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def build(args: argparse.Namespace) -> dict[str, Any]:
    rng = random.Random(args.seed)
    out_dir, raw_dir = Path(args.out), Path(args.raw)
    seeds: list[Seed] = []
    source_counts: dict[str, dict[str, Any]] = {}
    for name in args.sources:
        if name == "synthetic":
            got = synthetic_seeds(rng, args.synthetic_draws)
            source_counts[name] = {"licence": SYNTHETIC_LICENCE, "seeds": len(got)}
        else:
            rows = load_source(name, args.limit_per_source, raw_dir)
            got = dataset_seeds(name, rows)
            rng.shuffle(got)
            got = got[: args.max_seeds_per_source]
            source_counts[name] = {"dataset": SOURCES[name]["dataset"], "licence": SOURCES[name]["licence"],
                                   "rows": len(rows), "seeds": len(got)}
        seeds.extend(got)
        print(f"{name}: {len(got)} seed problems", file=sys.stderr)

    pages: list[Page] = []
    t0 = time.monotonic()
    for i, seed in enumerate(seeds, 1):
        made = pages_for(seed, rng, args.seed_timeout)
        pages.extend(made)
        source_counts[seed.source]["pages"] = source_counts[seed.source].get("pages", 0) + len(made)
        if i % 50 == 0:
            print(f"  pages: {i}/{len(seeds)} seeds, {len(pages)} pages, {time.monotonic() - t0:.0f}s", file=sys.stderr)
    if not pages:
        raise SystemExit("no pages: every seed failed")

    hp, hm = set(args.holdout_ptypes), set(args.holdout_mistakes)
    held = lambda p: p.seed.ptype in hp or p.mistake in hm  # noqa: E731
    tr_t, te_t, tr_c, te_c = held_out_templates(rng, args.holdout_templates)
    train_pages = [p for p in pages if not held(p)]
    test_pages = [p for p in pages if held(p)] or train_pages
    train_split = Split([p for p in train_pages if p.correct], [p for p in train_pages if not p.correct], tr_t, tr_c)
    # Test rows: unseen phrasing always; most pages from held-out problem or mistake types.
    test_held = Split([p for p in test_pages if p.correct], [p for p in test_pages if not p.correct], te_t, te_c)
    test_seen = Split(train_split.pages_correct, train_split.pages_wrong, te_t, te_c)

    train = sample_rows(args.train, train_split, rng, args.correct_ratio, args.noisy_ratio)
    seen = {r["messages"][1]["content"] for r in train}
    n_held = round(args.test * 0.6)
    test = sample_rows(n_held, test_held, rng, args.correct_ratio, args.noisy_ratio, seen)
    test += sample_rows(args.test - len(test), test_seen, rng, args.correct_ratio, args.noisy_ratio, seen)
    rng.shuffle(test)

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "train.jsonl", "w") as f:
        for r in train:
            f.write(json.dumps({"messages": r["messages"]}, ensure_ascii=False) + "\n")
    with open(out_dir / "test.jsonl", "w") as f:
        for r in test:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    manifest = {
        "seed": args.seed, "sources": source_counts, "seed_problems": len(seeds), "pages": len(pages),
        "holdout": {"ptypes": sorted(hp), "mistakes": sorted(hm), "templates_fraction": args.holdout_templates,
                    "test_templates": {k: [t for t, _ in v] for k, v in te_t.items()},
                    "test_combos": [t for t, _, _ in te_c]},
        "train": summarize(train), "test": summarize(test),
        "max_chars_per_row": max(len(json.dumps(r["messages"])) for r in train + test),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    labels = [json.loads(r["messages"][2]["content"]) for r in rows]
    metas = [r["meta"] for r in rows]
    n = max(1, len(rows))
    return {
        "rows": len(rows),
        "correct_chain_fraction": round(sum(m["correct_chain"] for m in metas) / n, 3),
        "noisy_fraction": round(sum(m["noisy"] for m in metas) / n, 3),
        "intent": dict(Counter(l["intent"] for l in labels).most_common()),
        "tools": dict(Counter("+".join(l["tools"]) or "-" for l in labels).most_common()),
        "hint_level": dict(sorted(Counter(l["hint_level"] for l in labels if "give_hint" in l["tools"]).items())),
        "ptype": dict(Counter(m["ptype"] for m in metas).most_common()),
        "mistake": dict(Counter(m["mistake"] or "none" for m in metas).most_common()),
        "error": dict(Counter(m["error"] or "none" for m in metas).most_common()),
        "source": dict(Counter(m["source"] for m in metas).most_common()),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "data" / "router"), help="output directory")
    ap.add_argument("--raw", default=str(ROOT / "data" / "raw"), help="download cache")
    ap.add_argument("--sources", default="synthetic,numina,deepmath",
                    type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                    help="comma list of synthetic, numina, deepmath")
    ap.add_argument("--limit-per-source", type=int, default=30000, help="max calculus rows per dataset")
    ap.add_argument("--max-seeds-per-source", type=int, default=400, help="max problems kept per dataset")
    ap.add_argument("--seed-timeout", type=float, default=4.0, help="seconds of SymPy work per problem")
    ap.add_argument("--synthetic-draws", type=int, default=4, help="random draws per generated problem template")
    ap.add_argument("--train", type=int, default=4000, help="training rows")
    ap.add_argument("--test", type=int, default=800, help="held-out test rows")
    ap.add_argument("--correct-ratio", type=float, default=0.3, help="share of rows with a fully correct chain")
    ap.add_argument("--noisy-ratio", type=float, default=0.35, help="share of commands with ASR noise")
    ap.add_argument("--holdout-ptypes", default="quotient,definite", type=lambda s: [x for x in s.split(",") if x])
    ap.add_argument("--holdout-mistakes", default="ibp_wrong_term,power_rule_exp",
                    type=lambda s: [x for x in s.split(",") if x])
    ap.add_argument("--holdout-templates", type=float, default=0.25, help="share of phrasings only in test")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    bad = [s for s in args.sources if s != "synthetic" and s not in SOURCES]
    if bad:
        ap.error(f"unknown source(s): {', '.join(bad)}")
    if not 0.2 <= args.correct_ratio <= 0.4:
        ap.error("--correct-ratio should stay within 0.2-0.4 so the router does not learn to hint on every page")
    unknown = [m for m in args.holdout_mistakes if m not in MISTAKES]
    if unknown:
        ap.error(f"unknown mistake(s): {', '.join(unknown)}; choose from {', '.join(MISTAKES)}")
    return args


def main(argv: list[str] | None = None) -> None:
    manifest = build(parse_args(argv))
    tr, te = manifest["train"], manifest["test"]
    print(f"train {tr['rows']} rows ({tr['correct_chain_fraction']:.0%} correct chains, "
          f"{tr['noisy_fraction']:.0%} noisy), test {te['rows']} rows; {manifest['pages']} pages "
          f"from {manifest['seed_problems']} problems")


if __name__ == "__main__":
    main()
