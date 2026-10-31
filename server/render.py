"""Neat versions of the page: typeset lines (unicode + LaTeX) and graph samples.

Everything here is computed by SymPy from what the referee parsed; nothing is phrased
by a model.

Public API:
    unicode_math(text)        -> str   ink text (from checker.pretty) with x², ·, −, π, ∫₀¹
    typeset_line(parsed)      -> (text, latex) or None if the line did not parse
    graph_for(result, reveal) -> dict  a "graph" annotation body (without "after"), or None
"""

from __future__ import annotations

import cmath
import math
import re
from typing import Any

import sympy as sp

from . import checker
from .checker import CheckResult, ParsedLine, pretty

SUP = dict(zip("0123456789+-=()abcdefghijklmnoprstuvwxyz",
               "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ"))
SUB = dict(zip("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))
FUNC_WORDS = set(checker.FUNCS.split("|")) | {"d/dx", "d/dt", "d/dy", "∫"}
OPS = {"+", "-", "−", "=", "/", "·"}
_SUPERS = set(SUP.values())


def _script(t: str, table: dict[str, str]) -> str | None:
    """Every character mapped to a super/subscript, or None if any one has no glyph."""
    t = t.replace(" ", "")
    out = []
    for ch in t:
        if ch in table:
            out.append(table[ch])
        elif table is SUP and ch in _SUPERS:
            out.append(ch)
        else:
            return None
    return "".join(out)


def unicode_math(text: str) -> str:
    """`x sin x - ∫ sin x dx` -> `x·sin x − ∫ sin x dx`; `x^2` -> `x²`; `∫_0^1` -> `∫₀¹`."""
    s = re.sub(r"\bpi\b", "π", text)
    s = s.replace("sqrt(", "√(")

    def limits(m: re.Match) -> str:
        lo, hi = m.group(1).strip("()"), m.group(2).strip("()")
        a, b = _script(lo, SUB), _script(hi, SUP)
        return f"{a or '_' + lo}{b or '^' + hi}"

    s = re.sub(r"_(\([^()]*\)|-?[0-9.]+|[a-zA-Zπ]+)\^(\([^()]*\)|-?[0-9.]+|[a-zA-Zπ]+)", limits, s)
    s = re.sub(r"\^(-?\d+)", lambda m: _script(m.group(1), SUP) or m.group(0), s)
    s = re.sub(r"\^([a-z])(?![a-zA-Z(])", lambda m: _script(m.group(1), SUP) or m.group(0), s)
    for _ in range(2):  # e^(x²) -> eˣ²; nested groups resolve inside-out
        s = re.sub(r"\^\(([^()]+)\)", lambda m: _script(m.group(1), SUP) or m.group(0), s)
    s = s.replace("'", "′")
    toks = s.split(" ")
    out = toks[:1]
    for a, b in zip(toks, toks[1:]):
        word = a.lstrip("(").rstrip("".join(_SUPERS))
        dot = (a and b and a not in OPS and b not in OPS and word not in FUNC_WORDS and b[0] != "|"
               and not a.startswith("∫") and not a.endswith("(") and not b.startswith(")")
               and not re.fullmatch(r"d[a-z]\)*", b) and not a.endswith("/"))
        out.append(("·" if dot else " ") + b)
    s = "".join(out)
    s = re.sub(r"(^|[\s(=])-", r"\1−", s).replace(" - ", " − ")
    return s


_FUNC_RE = re.compile(rf"({checker.FUNCS})")


def clean_text(text: str) -> str:
    """The student's own line, tidied for typesetting (order and form kept as written).

    `xcosx dx` -> `x cos x dx`, `x*sin(x)` -> `x sin(x)`, `int from 0 to 1` -> `∫_(0)^(1)`.
    """
    s = text.strip()
    for a, b in {"**": "^", "*": " ", "·": " ", "×": " ", "⋅": " ", "−": "-", "–": "-",
                 "\\int": "∫", "\\cdot": " ", "\\pi": "pi", "√": "sqrt"}.items():
        s = s.replace(a, b)
    s = re.sub(r"\b(?:int|integral)\s+from\s+(\S+)\s+to\s+(\S+)\s*", r"∫_(\1)^(\2) ", s)
    s = re.sub(r"\b(?:int|integral)\b", "∫", s)
    s = re.sub(r"∫(?=[^\s_])", "∫ ", s)
    s = _FUNC_RE.sub(r" \1 ", s)
    s = re.sub(r"(?<=[\w)])d([a-z])(?=\s*(?:=|\+|-|$))", r" d\1", s)  # glued xdx
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([)'^/])|([(/^])\s+", lambda m: m.group(1) or m.group(2), s)
    s = re.sub(rf"(?<![a-zA-Z])({checker.FUNCS})\s+([(|])", r"\1\2", s)
    s = re.sub(r"\s*([=+])\s*", r" \1 ", s).strip()
    s = re.sub(r"(?<=[\w)])\s*-\s*", " - ", s)
    return re.sub(r"^= ?", "= ", s)


def typeset_line(p: ParsedLine) -> tuple[str, str] | None:
    """The neat typed version of one parsed line: (unicode ink text, LaTeX).

    The text keeps the student's own order and form; the LaTeX is SymPy's reading of it
    (so it doubles as "this is how the referee read your line").
    """
    if not p.ok:
        return None
    return unicode_math(clean_text(p.text)), checker.latex_of(p)


# --------------------------------------------------------------------------- graphs

N_POINTS = 120


def _problem(result: CheckResult) -> sp.Expr | None:
    """The expression that started the last problem on the page."""
    for r in reversed(result.lines):
        if r.status == "start":
            return r.parsed.parts[0]
    return None


def _sample(expr: sp.Expr, var: sp.Symbol, lo: float, hi: float) -> list[list[float]]:
    expr = checker._plain(expr)
    try:
        f = sp.lambdify(var, expr, modules=[{"log": _log, "sqrt": _sqrt}, "math"])
    except Exception:
        return []
    pts = []
    for i in range(N_POINTS):
        x = lo + (hi - lo) * i / (N_POINTS - 1)
        try:
            y = f(x)
            if isinstance(y, complex):
                if abs(y.imag) > 1e-9:
                    continue
                y = y.real
            y = float(y)
        except (ValueError, ZeroDivisionError, OverflowError, TypeError):
            continue
        if math.isfinite(y):
            pts.append([round(x, 4), round(y, 4)])
    return pts


def _log(v):
    return math.log(v) if v > 0 else float("nan")


def _sqrt(v):
    return math.sqrt(v) if v >= 0 else float("nan")


def _xrange(exprs: list[sp.Expr], limits: tuple[float, float] | None) -> tuple[float, float]:
    if limits:
        a, b = sorted(limits)
        pad = max(1.0, 0.25 * (b - a))
        return a - pad, b + pad
    if any(e.has(sp.sin, sp.cos, sp.tan) for e in exprs):
        return -2 * math.pi, 2 * math.pi
    if any(e.has(sp.log) for e in exprs):
        return 0.0, 5.0
    return -4.0, 4.0


def _yrange(series: list[list[list[float]]]) -> tuple[float, float]:
    ys = sorted(y for pts in series for _, y in pts)
    if not ys:
        return -1.0, 1.0
    lo, hi = ys[int(0.04 * (len(ys) - 1))], ys[int(0.96 * (len(ys) - 1))]
    span = hi - lo if hi - lo > 1e-9 else 2.0
    lo, hi = lo - 0.15 * span, hi + 0.15 * span
    if 0 < lo < span:  # keep the x-axis in view when it is close
        lo = -0.05 * span
    if -span < hi < 0:
        hi = 0.05 * span
    return round(lo, 3), round(hi, 3)


def _clip(pts: list[list[float]], lo: float, hi: float) -> list[list[float]]:
    span = hi - lo
    return [p for p in pts if lo - span <= p[1] <= hi + span]


def _number(e: sp.Expr) -> float | None:
    try:
        v = complex(sp.N(e))
    except (TypeError, ValueError):
        return None
    return v.real if abs(v.imag) < 1e-12 and cmath.isfinite(v) else None


def graph_for(result: CheckResult, reveal: bool = False) -> dict[str, Any] | None:
    """What to sketch for the page's last problem.

    - indefinite integral: the integrand; once the student's answer is correct (or
      `reveal`, answer mode) the antiderivative with the integrand as `overlay`.
    - definite integral: the integrand with `shade` = [a, b].
    - derivative: the function; with `overlay` = its derivative when finished or `reveal`.
    - otherwise: the last correct expression.
    """
    var = result.var
    problem = _problem(result)
    if problem is None:
        return None
    fn = overlay = None
    shade = None
    if result.chain == "integral":
        integ = checker._bare_integrand(problem)
        if integ is None:
            return None
        if result.finished and result.base_value is not None:
            fn, overlay = result.base_value, integ
        elif reveal:
            F = sp.integrate(integ, var)
            fn, overlay = (F, integ) if not F.has(sp.Integral) else (integ, None)
        else:
            fn = integ
    elif result.chain == "definite":
        d = checker._bare_definite(problem)
        if d is None:
            return None
        fn, var = d[0], d[1]
        a, b = _number(d[2]), _number(d[3])
        shade = [round(a, 4), round(b, 4)] if a is not None and b is not None else None
    elif result.chain == "derivative":
        f = checker._bare_derivative(problem)
        if f is None:
            return None
        fn = f
        if result.finished or reveal:
            overlay = sp.diff(f, var)
    else:
        fn = result.base_value
    if fn is None or fn.free_symbols - {var} or var not in fn.free_symbols and overlay is None:
        return None
    exprs = [fn] + ([overlay] if overlay is not None else [])
    lo, hi = _xrange(exprs, tuple(shade) if shade else None)
    pts = _sample(fn, var, lo, hi)
    over = _sample(overlay, var, lo, hi) if overlay is not None else None
    if len(pts) < 2:
        return None
    ylo, yhi = _yrange([pts] + ([over] if over else []))
    out: dict[str, Any] = {"kind": "graph", "fn": pretty(fn), "points": _clip(pts, ylo, yhi),
                           "xrange": [round(lo, 3), round(hi, 3)], "yrange": [ylo, yhi]}
    if over:
        out["overlay"] = _clip(over, ylo, yhi)
        out["overlay_fn"] = pretty(overlay)
    if shade:
        out["shade"] = shade
    return out
