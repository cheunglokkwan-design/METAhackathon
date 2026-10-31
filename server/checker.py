"""The referee: SymPy-backed, calculus-aware checking of handwritten steps.

A page is a list of lines. Each line is one or more expressions joined by "=".
A line that starts with "=" (or has no integral sign and follows a chain) continues
the previous line's expression. Every expression must equal the one before it; when
the chain involves an integral we compare up to a constant by differentiating.

Public API:
    parse_line(text)          -> ParsedLine
    check_lines(lines)        -> CheckResult  (per-line status, first wrong line, hints)
    solve_remaining(lines)    -> list[str]    (next steps that finish the problem)
    pretty(expr)              -> str          (ink-friendly text, re-parseable)
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any

import sympy as sp
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)
from sympy.printing.str import StrPrinter

X = sp.Symbol("x")
TRANSFORMS = standard_transformations + (implicit_multiplication_application, convert_xor)
FUNCS = (
    "arcsin|arccos|arctan|asin|acos|atan|sinh|cosh|tanh|"
    "sin|cos|tan|sec|csc|cot|ln|log|exp|sqrt"
)
# A bare function argument such as `x`, `2x`, `x^2`, `3x^2`.
ARG = r"[0-9.]*[a-zA-Z](?:\^(?:\d+|[a-zA-Z]))?"


# --------------------------------------------------------------------------- parsing


@dataclass
class ParsedLine:
    """One student line: its '='-separated parts as SymPy expressions."""

    text: str
    parts: list[sp.Expr | None] = field(default_factory=list)
    has_c: list[bool] = field(default_factory=list)
    continuation: bool = False
    var: sp.Symbol = X
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.parts) and all(p is not None for p in self.parts)


def _normalize(text: str) -> str:
    """Map handwriting/unicode notation to plain ASCII the parser understands."""
    s = text.strip()
    for a, b in {
        "−": "-", "–": "-", "—": "-", "·": "*", "×": "*", "⋅": "*", "÷": "/",
        "²": "^2", "³": "^3", "π": "pi", "√": " sqrt ", "⁻¹": "^-1", "{": "(", "}": ")",
        "[": "(", "]": ")", "\\int": "∫", "\\cdot": "*", "\\,": " ",
    }.items():
        s = s.replace(a, b)
    s = re.sub(r"\\(sin|cos|tan|ln|log|exp|sqrt|sec|csc|cot|pi)\b", r"\1", s)
    s = re.sub(r"\\frac\(([^()]*)\)\(([^()]*)\)", r"((\1)/(\2))", s)
    s = re.sub(r"\bint\b", "∫", s)
    # |u| -> (Abs(u)); the outer parens stop `ln` from grabbing the "A" as its argument.
    s = re.sub(r"\|([^|]+)\|", r"(Abs(\1))", s)
    # sin^-1 x, tan^(-1) x -> arcsin x, arctan x
    s = re.sub(r"(?<![a-zA-Z])(sin|cos|tan)\s*\^\s*\(?\s*-\s*1\s*\)?", r"arc\1 ", s)
    return s


def _convert_integrals(s: str) -> tuple[str, sp.Symbol | None]:
    """Turn `∫ body dx` into `Integral((body), x)`, innermost (rightmost) first."""
    var = None
    while "∫" in s:
        i = s.rfind("∫")
        rest = s[i + 1 :]
        # `dx` may be glued to the integrand (`xdx`); no function name contains a "d".
        m = re.search(r"d([a-zA-Z])(?![a-zA-Z(])", rest)
        if m:
            body, tail, v = rest[: m.start()], rest[m.end() :], m.group(1)
            if not body.strip() and re.match(r"\s*/", tail):  # ∫ dx/x
                fm = re.match(r"\s*(/\s*(?:\([^()]*\)|[0-9.]*[a-zA-Z](?:\^\d+)?))", tail)
                if fm:
                    body, tail = "1" + fm.group(1), tail[fm.end():]
        else:
            body, tail, v = rest, "", "x"
        body = body.strip() or "1"
        var = var or sp.Symbol(v)
        s = f"{s[:i]} Integral(({body}), {v}) {tail}"
    return s, var


def _convert_functions(s: str) -> str:
    """`sin x` -> `sin(x)`, `sin^2 x` -> `sin(x)^2`, `e^2x` -> `e^(2x)`."""
    s = re.sub(rf"(?<![a-zA-Z])({FUNCS})\^(\d+)\s*({ARG})", r"\1(\3)^\2", s)
    s = re.sub(rf"(?<![a-zA-Z])({FUNCS})\s*(?!\s*\()({ARG})", r"\1(\2)", s)
    s = re.sub(r"(?<![a-zA-Z])e\^\s*([0-9.]*[a-zA-Z])(?![a-zA-Z(])", r"e^(\1)", s)
    return s


MAX_LINE_CHARS = 500
_SAFE_GLOBALS: dict[str, Any] = {
    "__builtins__": {},
    "Symbol": sp.Symbol, "Integer": sp.Integer, "Float": sp.Float, "Rational": sp.Rational,
    "Function": sp.Function, "Integral": sp.Integral, "Abs": sp.Abs, "pi": sp.pi, "E": sp.E,
    "sin": sp.sin, "cos": sp.cos, "tan": sp.tan, "sec": sp.sec, "csc": sp.csc, "cot": sp.cot,
    "asin": sp.asin, "acos": sp.acos, "atan": sp.atan, "sinh": sp.sinh, "cosh": sp.cosh, "tanh": sp.tanh,
    "exp": sp.exp, "log": sp.log, "sqrt": sp.sqrt,
}


def _local_dict(var: sp.Symbol) -> dict[str, Any]:
    return {
        "e": sp.E, "pi": sp.pi, "ln": sp.log, "log": sp.log, "Integral": sp.Integral,
        "arcsin": sp.asin, "arccos": sp.acos, "arctan": sp.atan, "Abs": sp.Abs,
        str(var): var, "x": X,
    }


def _strip_c(part: str) -> tuple[str, bool]:
    """Remove a trailing `+ C` (any case, also `+ const`)."""
    m = re.search(r"\+\s*(C|c|K|const)\s*$", part)
    if m:
        return part[: m.start()].strip(), True
    return part, False


def parse_line(text: str) -> ParsedLine:
    """Parse one handwritten line into '='-separated SymPy expressions."""
    line = ParsedLine(text=text)
    if len(text) > MAX_LINE_CHARS:
        line.error = "line too long"
        return line
    s = _normalize(text)
    if not s:
        line.error = "empty line"
        return line
    if s.startswith("="):
        line.continuation = True
        s = s[1:]
    raw_parts = [p.strip() for p in s.split("=")]
    for raw in raw_parts:
        if not raw:
            line.parts.append(None)
            line.has_c.append(False)
            line.error = line.error or "empty side of '='"
            continue
        raw, has_c = _strip_c(raw)
        conv, v = _convert_integrals(raw)
        if v is not None:
            line.var = v
        conv = _convert_functions(conv)
        try:
            if "__" in conv or re.search(r"\.[a-zA-Z_]|\^\s*\d{4,}", raw):
                raise ValueError("unsafe expression")
            expr = parse_expr(conv, local_dict=_local_dict(line.var), global_dict=_SAFE_GLOBALS, transformations=TRANSFORMS)
            if not isinstance(expr, sp.Expr):
                raise ValueError("not an expression")
        except Exception as exc:  # SymPy raises many exception types on bad input
            expr = None
            line.error = line.error or f"could not read '{raw}' ({type(exc).__name__})"
        line.parts.append(expr)
        line.has_c.append(has_c)
    if v_free := _guess_var(line):
        line.var = v_free
    return line


def _guess_var(line: ParsedLine) -> sp.Symbol | None:
    """Use the integration variable if any, else the single free symbol, else x."""
    for p in line.parts:
        if p is not None:
            for integ in p.atoms(sp.Integral):
                return integ.limits[0][0]
    syms = set().union(*[p.free_symbols for p in line.parts if p is not None]) if line.parts else set()
    if len(syms) == 1:
        return next(iter(syms))
    return None


# --------------------------------------------------------------------------- printing


class _InkPrinter(StrPrinter):
    """String printer tuned for ink: `∫ f dx`, `sin x`, `e^x`, `ln x`."""

    _names = {"log": "ln", "asin": "arcsin", "acos": "arccos", "atan": "arctan"}

    def _print_Integral(self, expr):
        f, var = expr.function, expr.limits[0][0]
        body = self._print(f)
        if f.is_Add:
            body = f"({body})"
        return f"∫ {body} d{var}"

    def _print_Function(self, expr):
        name = self._names.get(expr.func.__name__, expr.func.__name__)
        if len(expr.args) == 1 and isinstance(expr.args[0], sp.Abs):
            return f"{name}{self._print(expr.args[0])}"
        if len(expr.args) == 1 and expr.args[0].is_Symbol:
            return f"{name} {self._print(expr.args[0])}"
        return f"{name}({', '.join(self._print(a) for a in expr.args)})"

    def _print_exp(self, expr):
        arg = expr.args[0]
        return f"e^{arg}" if arg.is_Symbol else f"e^({self._print(arg)})"

    _print_log = _print_Function

    def _print_Abs(self, expr):
        return f"|{self._print(expr.args[0])}|"

    def _print_Mul(self, expr):
        if not any(isinstance(a, sp.Integral) for a in expr.args):
            return super()._print_Mul(expr)
        c, rest = expr.as_coeff_Mul()
        body = super()._print_Mul(rest) if rest.is_Mul else self._print(rest)
        if c == 1:
            return body
        if c == -1:
            return f"-{body}"
        if c.is_Integer:
            return f"{c} {body}"
        return f"({c}) {body}" if c > 0 else f"-({-c}) {body}"

    def _print_Pow(self, expr, rational=False):
        base, exp_ = expr.args
        if isinstance(base, sp.Function) and exp_.is_Integer and exp_ > 0 and base.args[0].is_Symbol:
            return f"{self._names.get(base.func.__name__, base.func.__name__)}^{exp_} {base.args[0]}"
        if isinstance(base, sp.Function) and exp_.is_Integer and exp_ > 0:
            return f"({self._print(base)})**{exp_}"
        return super()._print_Pow(expr, rational)


def pretty(expr: sp.Expr) -> str:
    """Ink-friendly text for an expression; re-parseable by parse_line."""
    s = _InkPrinter().doprint(expr)
    s = s.replace("**", "^").replace("*", " ")
    s = re.sub(r"(^|[\s(+\-/])(\d+) (?=[a-zA-Z(])", r"\1\2", s)
    return re.sub(r"\s+", " ", s).strip()


def latex_of(line: ParsedLine) -> str:
    """LaTeX for a parsed line ('' if it did not parse)."""
    if not line.ok:
        return ""
    bits = [sp.latex(p) + (" + C" if c else "") for p, c in zip(line.parts, line.has_c)]
    return ("= " if line.continuation else "") + " = ".join(bits)


# --------------------------------------------------------------------------- comparisons


def _plain(e: sp.Expr) -> sp.Expr:
    """Treat |u| as u so that d/dx ln|x| behaves like 1/x."""
    return e.replace(sp.Abs, lambda a: a)


def _is_zero(e: sp.Expr, var: sp.Symbol) -> bool:
    """True if e is identically zero: symbolic simplify, then numeric spot checks."""
    e = _plain(e)
    try:
        if sp.simplify(e) == 0:
            return True
    except Exception:
        pass
    rng = random.Random(7)
    hits = 0
    for _ in range(6):
        val = rng.uniform(0.3, 2.7)
        try:
            num = complex(e.subs(var, val).evalf())
        except Exception:
            continue
        if abs(num) > 1e-7:
            return False
        hits += 1
    return hits >= 3


def _const_ratio(a: sp.Expr, b: sp.Expr, var: sp.Symbol) -> sp.Expr | None:
    """If a/b is a constant (independent of var), return it."""
    try:
        if _is_zero(b, var):
            return None
        r = sp.nsimplify(sp.simplify(_plain(a) / _plain(b)))
    except Exception:
        return None
    if var not in r.free_symbols and r.is_number and r.is_finite:
        return r
    return None


class _Comparator:
    """Equality between consecutive steps: exact, or up to a constant when integrals are involved."""

    def __init__(self, var: sp.Symbol, up_to_constant: bool):
        self.var = var
        self.up_to_constant = up_to_constant

    def norm(self, e: sp.Expr) -> sp.Expr:
        """What actually gets compared: the derivative (integral chains) or the expression."""
        e = _plain(e)
        return sp.diff(e, self.var) if self.up_to_constant else e

    def same(self, a: sp.Expr, b: sp.Expr) -> bool:
        return _is_zero(self.norm(a) - self.norm(b), self.var)


# --------------------------------------------------------------------------- diagnosis


@dataclass
class Diagnosis:
    """Why a step is wrong, with a machine verdict and level 1/2/3 rule hints."""

    kind: str
    verdict: str
    hints: dict[int, str]
    fix: str | None = None


def _bare_integrand(expr: sp.Expr) -> sp.Expr | None:
    """If expr is exactly ∫ f dx, return f."""
    if isinstance(expr, sp.Integral) and len(expr.limits[0]) == 1:
        return expr.function
    return None


def _monomial(expr: sp.Expr, var: sp.Symbol) -> tuple[sp.Expr, sp.Expr] | None:
    """a*var**n -> (a, n); None for anything else."""
    expr = sp.expand(expr)
    if expr.is_Add:
        return None
    coeff, rest = expr.as_independent(var, as_Add=False)
    if rest == 1:
        return coeff, sp.Integer(0)
    if rest == var:
        return coeff, sp.Integer(1)
    if rest.is_Pow and rest.base == var and var not in rest.exp.free_symbols:
        return coeff, rest.exp
    return None


def _with_c(text: str, final: bool) -> str:
    return f"{text} + C" if final else text


def diagnose(prev: sp.Expr, cur: sp.Expr, cmp: _Comparator, line_no: int, final: bool) -> Diagnosis:
    """Explain why `cur` does not follow from `prev`. Detectors run most-specific first."""
    var = cmp.var
    S, D = cmp.norm(cur), cmp.norm(prev)
    what = f"d/dx(line {line_no})" if cmp.up_to_constant else f"line {line_no}"
    base = f"{what} = {pretty(S)}, expected {pretty(D)}"
    integrand = _bare_integrand(prev)

    # Differentiated instead of integrated: ∫ x^2 dx = 2x
    if integrand is not None and not cur.has(sp.Integral):
        if _is_zero(cur - sp.diff(integrand, var), var):
            fix = pretty(sp.integrate(integrand, var))
            return Diagnosis(
                "differentiated", f"differentiated instead of integrated: {base}",
                {1: "Integrate here, don't differentiate",
                 2: "Reverse the derivative: what gives this?",
                 3: f"= {fix} + C"}, fix=f"{fix} + C")

    # Whole line has the wrong sign
    if cmp.same(-cur, prev):
        fix = _with_c(pretty(-cur), final)
        return Diagnosis(
            "sign", f"sign error (whole line negated): {base}",
            {1: "Check your signs here", 2: "The whole line has the wrong sign", 3: f"= {fix}"},
            fix=fix)

    # One term has the wrong sign
    terms = sp.Add.make_args(cur)
    if len(terms) > 1:
        for t in terms:
            if cmp.same(cur - 2 * t, prev):
                fix = _with_c(pretty(cur - 2 * t), final)
                return Diagnosis(
                    "sign", f"sign error on term {pretty(t)}: {base}",
                    {1: "Check your signs here",
                     2: f"Sign slip on the {pretty(-t if t.could_extract_minus_sign() else t)} term",
                     3: f"= {fix}"},
                    fix=fix)

    # Power rule slips: ∫ a x^n dx
    if integrand is not None and not cur.has(sp.Integral):
        mono_in, mono_out = _monomial(integrand, var), _monomial(cur, var)
        if mono_in and mono_out and mono_in[1] != -1 and var in cur.free_symbols:
            a, n = mono_in
            k, m = mono_out
            right = sp.simplify(a / (n + 1))
            if sp.simplify(m - n) == 0 or (sp.simplify(m - n - 1) == 0 and sp.simplify(k - right) != 0):
                fix = pretty(right * var ** (n + 1))
                why = "Raise the power by one" if sp.simplify(m - n) == 0 else f"Divide by the new power {n + 1}"
                return Diagnosis(
                    "power_rule", f"power rule slip: {base}",
                    {1: "Check the power rule", 2: why, 3: f"= {fix} + C"}, fix=f"{fix} + C")

    # Missing / extra constant factor (e.g. forgot the 1/2 in ∫ cos 2x dx)
    r = _const_ratio(S, D, var)
    if r is not None and r != 1 and r != 0:
        fix = _with_c(pretty(sp.expand(cur / r)), final)
        need = pretty(1 / r)
        return Diagnosis(
            "factor", f"off by constant factor {pretty(r)}: {base}",
            {1: "Check the constant in front",
             2: f"Missing a factor of {need}" if abs(r) > 1 else f"Extra factor: multiply by {need}",
             3: f"= {fix}"},
            fix=fix)

    # Generic
    try:
        expected = sp.simplify(prev.doit())
        fix = None if expected.has(sp.Integral) else _with_c(pretty(expected), final)
    except Exception:
        fix = None
    return Diagnosis(
        "not_equal", f"not equal: {base}",
        {1: "This step doesn't follow", 2: "Redo this step from the line above",
         3: f"= {fix}" if fix else "Redo this step from the line above"},
        fix=fix)


# --------------------------------------------------------------------------- checking


@dataclass
class LineResult:
    id: str
    text: str
    latex: str
    status: str  # ok | wrong | unchecked | start
    parsed: ParsedLine
    diagnosis: Diagnosis | None = None
    note: str | None = None  # minor remark on an ok line (e.g. forgot + C)
    wrong_part: int | None = None  # index of the first wrong '='-part in this line

    def corrected(self) -> str | None:
        """The wrong line rewritten with the fix, e.g. '∫ cos(2x) dx = sin(2x)/2 + C'."""
        if self.diagnosis is None or self.diagnosis.fix is None or self.wrong_part is None:
            return None
        body = self.text.strip().lstrip("=").strip()
        kept = [p.strip() for p in body.split("=")][: self.wrong_part]
        text = " = ".join(kept + [self.diagnosis.fix])
        return f"= {text}" if self.parsed.continuation else text


@dataclass
class CheckResult:
    lines: list[LineResult]
    first_wrong: str | None
    verdict: str
    var: sp.Symbol
    base_value: sp.Expr | None  # last correct expression (where a solver should continue)
    finished: bool  # last correct value has no integral left (in an integral chain)
    integral_chain: bool

    @property
    def wrong(self) -> LineResult | None:
        return next((l for l in self.lines if l.status == "wrong"), None)


def _as_items(lines: list[Any]) -> list[tuple[str, str]]:
    return [(f"L{i}", ln) if isinstance(ln, str) else (str(ln.get("id") or f"L{i}"), str(ln.get("text") or ""))
            for i, ln in enumerate(lines, 1)]


def check_lines(lines: list[Any]) -> CheckResult:
    """Check every step. `lines` are strings or {id, text} dicts, top to bottom.

    Only the last chain on the page (from the last line that starts a new problem)
    drives hints; earlier problems are still checked.
    """
    results: list[LineResult] = []
    prev: sp.Expr | None = None
    last_good: sp.Expr | None = None
    var = X
    integral_chain = False
    first_wrong: str | None = None
    verdict = ""
    step_no = 0

    for line_id, text in _as_items(lines):
        step_no += 1
        p = parse_line(text)
        res = LineResult(line_id, text, latex_of(p), "unchecked", p)
        results.append(res)
        if not p.ok:
            continue
        starts_new = not p.continuation and (prev is None or p.parts[0].has(sp.Integral))
        if starts_new:
            prev, var, integral_chain = p.parts[0], p.var, p.parts[0].has(sp.Integral)
            last_good = prev
            res.status = "start"
            offset = 1
        else:
            offset = 0
        todo = list(zip(p.parts[offset:], p.has_c[offset:]))
        if first_wrong is not None and not starts_new:
            continue  # after the first error, later lines of the chain stay "unchecked"
        status = res.status if not todo else "ok"
        for k, (expr, has_c) in enumerate(todo):
            integral_chain = integral_chain or expr.has(sp.Integral)
            cmp = _Comparator(var, integral_chain)
            final = integral_chain and not expr.has(sp.Integral)
            if cmp.same(expr, prev):
                prev = last_good = expr
                if final and not has_c:
                    res.note = "Don't forget + C"
                elif has_c:
                    res.note = None
                continue
            status = "wrong"
            res.diagnosis = diagnose(prev, expr, cmp, step_no, final)
            res.wrong_part = k + offset
            if first_wrong is None:
                first_wrong, verdict = line_id, res.diagnosis.verdict
            prev = expr
            break
        res.status = status

    # Only the very last line keeps a "+ C" reminder.
    for r in results[:-1]:
        if r.note == "Don't forget + C":
            r.note = None

    finished = integral_chain and last_good is not None and not last_good.has(sp.Integral)
    if not verdict:
        verdict = "all steps check out" if any(r.status in ("ok", "start") for r in results) else "nothing to check"
    return CheckResult(results, first_wrong, verdict, var, last_good, finished, integral_chain)


# --------------------------------------------------------------------------- solving


def _parts_split(f: sp.Expr, var: sp.Symbol) -> tuple[sp.Expr, sp.Expr] | None:
    """Choose (u, dv) for integration by parts on f, LIATE style, or None."""
    coeff, g = f.as_independent(var, as_Add=False)
    factors = sp.Mul.make_args(g)
    logs = [t for t in factors if isinstance(t, sp.log)]
    poly = [t for t in factors if t.is_polynomial(var) and var in t.free_symbols]
    trans = [t for t in factors if t not in poly and t not in logs]

    def linear_arg(t: sp.Expr) -> bool:
        if isinstance(t, (sp.sin, sp.cos, sp.exp)):
            return sp.degree(t.args[0], var) == 1 if t.args[0].is_polynomial(var) else False
        if t.is_Pow and t.base == sp.E:
            return True
        return False

    if len(logs) == 1 and not trans:  # x^n ln x, or ln x alone (dv = dx)
        dv = coeff * sp.Mul(*poly)
        return logs[0], dv
    if poly and len(trans) == 1 and linear_arg(trans[0]) and not logs:
        return sp.Mul(*poly), coeff * trans[0]
    return None


def _split_const(e: sp.Expr, var: sp.Symbol) -> sp.Expr:
    """∫ c·g dx written as c·∫ g dx."""
    c, g = sp.expand(e).as_independent(var, as_Add=False)
    return c * sp.Integral(g, var) if g != 1 else c * var


def _parts_step(value: sp.Expr, var: sp.Symbol) -> sp.Expr | None:
    """Apply one round of integration by parts to each integral in value."""
    changed = False
    repl = {}
    for integ in value.atoms(sp.Integral):
        split = _parts_split(integ.function, var)
        if not split:
            continue
        u, dv = split
        v = sp.integrate(dv, var)
        new_integrand = sp.simplify(v * sp.diff(u, var))
        repl[integ] = u * v - _split_const(new_integrand, var)
        changed = True
    return value.xreplace(repl) if changed else None


def solve_remaining(lines: list[Any]) -> list[str]:
    """Steps (as ink text, each starting with '=') that finish the last problem.

    Continues from the last correct expression. Integration by parts gets a readable
    2-3 step path; anything else jumps straight to `= result + C`.
    """
    res = lines if isinstance(lines, CheckResult) else check_lines(lines)
    value, var = res.base_value, res.var
    if value is None:
        return []
    if not res.integral_chain:
        simp = sp.simplify(value)
        return [] if simp == value else [f"= {pretty(simp)}"]
    if not value.has(sp.Integral):
        return []
    had_log = any(i.function.has(sp.log) for i in value.atoms(sp.Integral))
    steps: list[str] = []
    for _ in range(3):
        nxt = _parts_step(value, var)
        if nxt is None:
            break
        value = nxt
        if value.has(sp.Integral):
            steps.append(f"= {pretty(value)}")
    # Substitution integrals stay compact in g(x); everything else is expanded.
    held = {}
    for integ in value.atoms(sp.Integral):
        F = _integrate_by_sub(integ, var)
        if F is not None:
            held[integ] = (sp.Dummy(), F)
    try:
        final = sp.expand(value.xreplace({k: d for k, (d, _) in held.items()}).doit())
        final = final.xreplace({d: F for d, F in held.values()})
    except Exception:
        final = value
    if final.has(sp.Integral):
        return steps  # SymPy could not finish it either
    if not had_log:  # ∫ 1/x dx = ln|x|, not ln x
        final = final.replace(sp.log, lambda a: sp.log(sp.Abs(a)))
    steps.append(f"= {pretty(final)} + C")
    return steps


def next_step_hints(res: CheckResult) -> dict[int, str] | None:
    """Level 1/2/3 nudges toward the next step when everything so far is right."""
    value, var = res.base_value, res.var
    if value is None or not value.has(sp.Integral):
        return None
    integ = sorted(value.atoms(sp.Integral), key=sp.default_sort_key)[0]
    f = integ.function
    steps = solve_remaining(res)
    nxt = f"Next: {steps[0]}" if steps else "Integrate what's left"
    split = _parts_split(f, var)
    if split:
        u, dv = split
        return {1: "Try integration by parts", 2: f"Let u = {pretty(u)}, dv = {pretty(dv)} d{var}", 3: nxt}
    sub = u_sub(f, var)
    if sub is not None:
        g = sub[0]
        return {1: "Try a u-substitution", 2: f"Let u = {pretty(g)}", 3: nxt}
    if f.is_polynomial(var):
        return {1: "Use the power rule", 2: "x^n integrates to x^(n+1)/(n+1)", 3: nxt}
    if f.is_Add:
        return {1: "Integrate term by term", 2: "Split it up and integrate each term", 3: nxt}
    return {1: "What differentiates to this?", 2: "Think of a derivative you know", 3: nxt}


U = sp.Dummy("u")


def u_sub(f: sp.Expr, var: sp.Symbol) -> tuple[sp.Expr, sp.Expr] | None:
    """Find g, h with f = h(g(x))·g'(x) (e.g. 2x e^(x^2) -> (x^2, e^u)), else None.

    Inner arguments are tried first, then whole functions (sin x cos x -> u = sin x).
    """
    inner: list[sp.Expr] = []
    outer: list[sp.Expr] = []
    for node in sp.preorder_traversal(f):
        if isinstance(node, sp.Function):
            inner.extend(node.args)
            outer.append(node)
        elif node.is_Pow:
            inner.extend(node.args)
    candidates = inner + outer
    u = U
    for g in candidates:
        if g == var or var not in g.free_symbols or g in (sp.Abs(var),):
            continue
        dg = sp.diff(g, var)
        if dg == 0:
            continue
        try:
            in_u = sp.simplify(f / dg).subs(g, u)
        except Exception:
            continue
        if var not in in_u.free_symbols:
            return g, in_u
    return None


def _integrate_by_sub(integ: sp.Integral, var: sp.Symbol) -> sp.Expr | None:
    """∫ h(g) g' dx = H(g): keeps answers like (x^2+1)^6/6 compact."""
    sub = u_sub(integ.function, var)
    if sub is None or len(integ.limits[0]) != 1:
        return None
    g, h = sub
    F = sp.integrate(h, U)
    return None if F.has(sp.Integral) else F.subs(U, g)
