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

import io
import keyword
import random
import re
import tokenize
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
    # part index -> (F, var, a, b) for an evaluation bracket [F]_a^b written on that part
    brackets: dict[int, tuple[sp.Expr, sp.Symbol, sp.Expr, sp.Expr]] = field(default_factory=dict)
    defines: str | None = None  # "f" for a definition line like f(x) = x^2 sin x

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
        "′": "'", "’": "'", "\\left": "", "\\right": "",
    }.items():
        s = s.replace(a, b)
    s = re.sub(r"\\(sin|cos|tan|ln|log|exp|sqrt|sec|csc|cot|pi)\b", r"\1", s)
    s = re.sub(r"\\frac\(d\)\(d([a-z])\)", r"d/d\1", s)
    s = re.sub(r"\\frac\(([^()]*)\)\(([^()]*)\)", r"((\1)/(\2))", s)
    s = re.sub(r"\b(?:int|integral)\s+from\s+(\S+)\s+to\s+(\S+)\s*", r"∫_(\1)^(\2) ", s)
    s = re.sub(r"\b(?:int|integral)\b", "∫", s)
    # |u| -> (Abs(u)); the outer parens stop `ln` from grabbing the "A" as its argument.
    s = re.sub(r"\|([^|]+)\|", r"(Abs(\1))", s)
    # sin^-1 x, tan^(-1) x -> arcsin x, arctan x
    s = re.sub(r"(?<![a-zA-Z])(sin|cos|tan)\s*\^\s*\(?\s*-\s*1\s*\)?", r"arc\1 ", s)
    return s


# A limit of integration / evaluation: (0), {pi}, 1, -2.5, pi, a
LIM = r"(\{[^{}]*\}|\([^()]*\)|-?[0-9.]+|-?[a-zA-Zπ]+)"


def _mark_evals(s: str) -> str:
    """`[F]_a^b` and `F |_a^b` -> `EVAL((F),(a),(b))`. Runs before _normalize eats brackets."""
    s = s.replace("\\left", "").replace("\\right", "").replace("\\Big", "").replace("\\big", "")
    s = re.sub(rf"\[([^\[\]=]+)\]\s*_\s*{LIM}\s*\^\s*{LIM}", r"EVAL((\1),(\2),(\3))", s)
    s = re.sub(rf"([^=|\[\]]+?)\s*\|\s*_\s*{LIM}\s*\^\s*{LIM}", r"EVAL((\1),(\2),(\3))", s)
    return s


def _convert_integrals(s: str) -> tuple[str, sp.Symbol | None]:
    """Turn `∫ body dx` into `Integral((body), x)` (or `(x, a, b)` for `∫_a^b`), innermost first."""
    var = None
    while "∫" in s:
        i = s.rfind("∫")
        rest = s[i + 1 :]
        limits = None
        lm = re.match(rf"\s*_\s*{LIM}\s*\^\s*{LIM}", rest)
        if lm:
            limits, rest = (lm.group(1), lm.group(2)), rest[lm.end():]
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
        lim = f"({v}, {limits[0]}, {limits[1]})" if limits else v
        s = f"{s[:i]} Integral(({body}), {lim}) {tail}"
    return s, var


def _match_open(s: str, close: int) -> int | None:
    """Index of the '(' matching the ')' at s[close]."""
    depth = 0
    for j in range(close, -1, -1):
        depth += {")": 1, "(": -1}.get(s[j], 0)
        if depth == 0:
            return j
    return None


def _match_close(s: str, open_: int) -> int | None:
    depth = 0
    for j in range(open_, len(s)):
        depth += {"(": 1, ")": -1}.get(s[j], 0)
        if depth == 0:
            return j
    return None


def _convert_derivatives(s: str, defs: dict[str, tuple[sp.Expr, sp.Symbol]]) -> str:
    """`d/dx (u)`, `(u)'`, `f'(x)`, `y'`, `dy/dx` -> `Derivative((u), x)`."""
    for name, (_, v) in defs.items():
        s = re.sub(rf"(?<![a-zA-Z]){name}\s*'\s*(?:\(\s*{v}\s*\))?", f"Derivative((DEF_{name}), {v})", s)
        s = re.sub(rf"(?<![a-zA-Z])d{name}\s*/\s*d{v}(?![a-zA-Z])", f"Derivative((DEF_{name}), {v})", s)
    while (i := s.find(")'")) >= 0:
        j = _match_open(s, i)
        if j is None:
            break
        fm = re.search(rf"(?<![a-zA-Z])({FUNCS})\s*$", s[:j])  # sin(x)' differentiates sin(x)
        if fm:
            j = fm.start()
        s = f"{s[:j]}Derivative(({s[j:i + 1]}), x){s[i + 2:]}"
    while m := re.search(r"(?<![a-zA-Z])d\s*/\s*d([a-z])(?![a-zA-Z])", s):
        v, rest = m.group(1), s[m.end():].lstrip()
        k = _match_close(rest, 0) if rest.startswith("(") else None
        if k is not None and rest[k + 1:].lstrip().startswith("^"):
            k = None  # d/dx (x^2+1)^5 differentiates the whole power
        body, tail = (rest[: k + 1], rest[k + 1:]) if k is not None else (f"({rest})", "")
        s = f"{s[:m.start()]}Derivative({body}, {v}){tail}"
    return s


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
    "Function": sp.Function, "Integral": sp.Integral, "Derivative": sp.Derivative, "Abs": sp.Abs,
    "pi": sp.pi, "E": sp.E,
    "sin": sp.sin, "cos": sp.cos, "tan": sp.tan, "sec": sp.sec, "csc": sp.csc, "cot": sp.cot,
    "asin": sp.asin, "acos": sp.acos, "atan": sp.atan, "sinh": sp.sinh, "cosh": sp.cosh, "tanh": sp.tanh,
    "exp": sp.exp, "log": sp.log, "sqrt": sp.sqrt,
}


# Largest exponent a term may carry, multiplied through nested powers: x^999 is fine,
# x^1001 and (x^40)^40 are not. Number towers such as 9^9^9 are caught before SymPy
# tries to build the exact integer.
MAX_EXPONENT = 1000
MAX_NUMBER_BITS = 20_000  # ~6000 digits: the biggest exact number a power may produce


def _reject_unsafe_tokens(conv: str) -> None:
    """Reject attribute access, dunders and Python keywords before anything is evaluated."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(conv).readline))
    except (tokenize.TokenError, SyntaxError):
        raise ValueError("unreadable expression") from None
    for tok in tokens:
        if (tok.type == tokenize.OP and tok.string == ".") or (
            tok.type == tokenize.NAME and ("__" in tok.string or keyword.iskeyword(tok.string))
        ):
            raise ValueError("unsafe expression")


def _exponent_size(ex: sp.Expr) -> float | None:
    """How large an exponent is, or None when it cannot blow up (e.g. `2^x`).

    A symbolic exponent with a log in it (`e^(9999 ln x)`) evaluates to a plain power,
    so its biggest coefficient counts as the exponent.
    """
    if not ex.free_symbols:
        val = sp.N(ex)
        return float(abs(val)) if val.is_Number else None
    if ex.has(sp.log):
        return max((float(abs(n)) for n in ex.atoms(sp.Number)), default=None)
    return None


def _check_powers(e: sp.Basic) -> int:
    """Walk an unevaluated expression bottom-up; return its nested exponent product.

    Raises ValueError for anything that would make SymPy build a huge exact number or
    polynomial. Children are checked before a node is evaluated numerically, so the
    sizes measured here are always small enough to compute.
    """
    if not e.args:
        return 1
    if isinstance(e, sp.Pow) or isinstance(e, sp.exp):
        base, ex = e.args if isinstance(e, sp.Pow) else (sp.E, e.args[0])
        mult = _check_powers(base)
        _check_powers(ex)
        size = _exponent_size(ex)
        if size is None:
            return mult
        mult = max(1, round(mult * max(size, 1.0)))
        if mult > MAX_EXPONENT:
            raise ValueError("exponent too large")
        if not base.free_symbols and isinstance(e, sp.Pow):
            b = sp.N(base)
            if b.is_Number and b != 0 and size * abs(float(sp.log(abs(b), 2))) > MAX_NUMBER_BITS:
                raise ValueError("number too large")
        return mult
    return max(_check_powers(a) for a in e.args)


def _guard_size(conv: str, local: dict[str, Any]) -> None:
    """Parse `conv` without evaluating anything and reject oversized powers."""

    def _eval_at(F, a, b):
        syms = sorted(F.free_symbols, key=str)
        v_ = syms[0] if len(syms) == 1 else X
        return F.xreplace({v_: b}) - F.xreplace({v_: a})

    with sp.evaluate(False):
        expr = parse_expr(conv, local_dict={**local, "EVAL": _eval_at},
                          global_dict=_SAFE_GLOBALS, transformations=TRANSFORMS)
    # Measure outside the context: numeric evaluation needs evaluate=True to terminate.
    if isinstance(expr, sp.Basic):
        _check_powers(expr)


def _local_dict(var: sp.Symbol) -> dict[str, Any]:
    return {
        "e": sp.E, "pi": sp.pi, "ln": sp.log, "log": sp.log, "Integral": sp.Integral,
        "Derivative": sp.Derivative,
        "arcsin": sp.asin, "arccos": sp.acos, "arctan": sp.atan, "Abs": sp.Abs,
        str(var): var, "x": X,
    }


def _strip_c(part: str) -> tuple[str, bool]:
    """Remove a trailing `+ C` (any case, also `+ const`)."""
    m = re.search(r"\+\s*(C|c|K|const)\s*$", part)
    if m:
        return part[: m.start()].strip(), True
    return part, False


DEF_RE = re.compile(r"^\s*(?:([a-dfghk-wzA-Z])\s*\(\s*([a-z])\s*\)|(y))\s*=\s*([^=]+)$")


def parse_line(text: str, defs: dict[str, tuple[sp.Expr, sp.Symbol]] | None = None) -> ParsedLine:
    """Parse one handwritten line into '='-separated SymPy expressions.

    `defs` maps names defined on earlier lines (f(x) = ..., y = ...) to (expr, var), so
    that `f'(x)`, `y'` and `dy/dx` can be read as derivatives.
    """
    defs = defs or {}
    line = ParsedLine(text=text)
    if len(text) > MAX_LINE_CHARS:
        line.error = "line too long"
        return line
    dm = DEF_RE.match(text.replace("−", "-"))
    if dm and "∫" not in text and "'" not in text:
        name, v = dm.group(1) or dm.group(3), sp.Symbol(dm.group(2) or "x")
        rhs = parse_line(dm.group(4), defs)
        if rhs.ok and len(rhs.parts) == 1 and not rhs.continuation:
            rhs.text, rhs.defines, rhs.var = text, name, v
            return rhs
    s = _normalize(_mark_evals(text))
    if not s:
        line.error = "empty line"
        return line
    if s.startswith("="):
        line.continuation = True
        s = s[1:]
    raw_parts = [p.strip() for p in s.split("=")]
    for k, raw in enumerate(raw_parts):
        if not raw:
            line.parts.append(None)
            line.has_c.append(False)
            line.error = line.error or "empty side of '='"
            continue
        raw, has_c = _strip_c(raw)
        conv = _convert_derivatives(raw, defs)
        conv, v = _convert_integrals(conv)
        if v is not None:
            line.var = v
        conv = _convert_functions(conv)
        local = _local_dict(line.var)
        local.update({f"DEF_{n}": e for n, (e, _) in defs.items()})
        evals: list[tuple[sp.Expr, sp.Symbol, sp.Expr, sp.Expr]] = []

        def _eval_at(F, a, b, _evals=evals):
            syms = sorted(F.free_symbols, key=str)
            v_ = syms[0] if len(syms) == 1 else line.var
            _evals.append((F, v_, a, b))
            return F.subs(v_, b) - F.subs(v_, a)

        local["EVAL"] = _eval_at
        try:
            _reject_unsafe_tokens(conv)
            _guard_size(conv, local)
            expr = parse_expr(conv, local_dict=local, global_dict=_SAFE_GLOBALS, transformations=TRANSFORMS)
            if not isinstance(expr, sp.Expr):
                raise ValueError("not an expression")
        except Exception as exc:  # SymPy raises many exception types on bad input
            expr = None
            line.error = line.error or f"could not read '{raw}' ({type(exc).__name__})"
        if expr is not None and len(evals) == 1:
            line.brackets[k] = evals[0]
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
            for der in p.atoms(sp.Derivative):
                return der.variables[0]
    syms = set().union(*[p.free_symbols for p in line.parts if p is not None]) if line.parts else set()
    if len(syms) == 1:
        return next(iter(syms))
    return None


# --------------------------------------------------------------------------- printing


class _InkPrinter(StrPrinter):
    """String printer tuned for ink: `∫ f dx`, `sin x`, `e^x`, `ln x`."""

    _names = {"log": "ln", "asin": "arcsin", "acos": "arccos", "atan": "arctan"}

    def _print_Integral(self, expr):
        f, lim = expr.function, expr.limits[0]
        body = self._print(f)
        if f.is_Add:
            body = f"({body})"
        if len(lim) == 3:
            return f"∫_{_lim(self._print(lim[1]))}^{_lim(self._print(lim[2]))} {body} d{lim[0]}"
        return f"∫ {body} d{lim[0]}"

    def _print_Derivative(self, expr):
        """Inside a line: (u)'. A whole part that is a derivative prints as d/dx (u), see pretty()."""
        return f"({self._print(expr.expr)})'"

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


def _lim(t: str) -> str:
    """A limit as written after _ or ^: bare if simple, else parenthesised."""
    return t if re.fullmatch(r"-?[0-9.]+|[a-zA-Z]+", t) else f"({t})"


def pretty_bracket(F: sp.Expr, a: sp.Expr, b: sp.Expr) -> str:
    """`[x^3/3]_0^1`: an antiderivative evaluated between limits (re-parseable)."""
    return f"[{pretty(F)}]_{_lim(pretty(a))}^{_lim(pretty(b))}"


def pretty(expr: sp.Expr) -> str:
    """Ink-friendly text for an expression; re-parseable by parse_line."""
    if isinstance(expr, sp.Derivative):
        s = f"d/d{expr.variables[0]} ({_InkPrinter().doprint(expr.expr)})"
    else:
        s = _InkPrinter().doprint(expr)
    s = s.replace("**", "^").replace("*", " ")
    s = re.sub(r"\bE\b", "e", s)
    s = re.sub(r"(^|[\s(+\-/])(\d+) (?=[a-zA-Z(])", r"\1\2", s)
    return re.sub(r"\s+", " ", s).strip()


def latex_of(line: ParsedLine) -> str:
    """LaTeX for a parsed line ('' if it did not parse)."""
    if not line.ok:
        return ""
    bits = []
    for k, (p, c) in enumerate(zip(line.parts, line.has_c)):
        if k in line.brackets:
            F, _, a, b = line.brackets[k]
            bits.append(rf"\left[ {sp.latex(F, ln_notation=True)} \right]_{{{sp.latex(a)}}}^{{{sp.latex(b)}}}")
        else:
            bits.append(sp.latex(p, ln_notation=True) + (" + C" if c else ""))
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


def _has_indef(e: sp.Expr) -> bool:
    """Has an indefinite integral (∫ f dx, no limits)."""
    return any(len(i.limits[0]) == 1 for i in e.atoms(sp.Integral))


def chain_kind(e: sp.Expr) -> str:
    """integral (indefinite) | definite | derivative | algebra: what problem a line starts."""
    if _has_indef(e):
        return "integral"
    if e.has(sp.Integral):
        return "definite"
    if e.has(sp.Derivative):
        return "derivative"
    return "algebra"


def evaluate(e: sp.Expr) -> sp.Expr:
    """Carry out every d/dx and every definite integral (indefinite ones stay)."""
    repl = {}
    for d in e.atoms(sp.Derivative):
        repl[d] = d.doit()
    for i in e.atoms(sp.Integral):
        if len(i.limits[0]) == 3:
            repl[i] = i.doit()
    return e.xreplace(repl) if repl else e


class _Comparator:
    """Equality between consecutive steps: exact, or up to a constant when integrals are involved."""

    def __init__(self, var: sp.Symbol, up_to_constant: bool, kind: str = "integral"):
        self.var = var
        self.up_to_constant = up_to_constant
        self.kind = kind

    def norm(self, e: sp.Expr) -> sp.Expr:
        """What actually gets compared: the derivative (integral chains) or the expression."""
        e = _plain(e)
        return sp.diff(e, self.var) if self.up_to_constant else evaluate(e)

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


def _bare_derivative(expr: sp.Expr) -> sp.Expr | None:
    """If expr is exactly d/dx f, return f."""
    if isinstance(expr, sp.Derivative) and len(expr.variables) == 1:
        return expr.expr
    return None


def _bare_definite(expr: sp.Expr) -> tuple[sp.Expr, sp.Symbol, sp.Expr, sp.Expr] | None:
    """If expr is exactly ∫_a^b f dx, return (f, x, a, b)."""
    if isinstance(expr, sp.Integral) and len(expr.limits) == 1 and len(expr.limits[0]) == 3:
        v, a, b = expr.limits[0]
        return expr.function, v, a, b
    return None


def _inner_args(f: sp.Expr, var: sp.Symbol) -> list[sp.Expr]:
    """Inner functions of compositions in f: the 3x in sin 3x, the x^2+1 in (x^2+1)^5."""
    out = []
    for node in sp.preorder_traversal(f):
        if isinstance(node, sp.Function):
            out.extend(a for a in node.args if a != var and var in a.free_symbols)
        elif node.is_Pow:
            if node.base == sp.E:
                out.append(node.exp)
            elif node.base != var and var in node.base.free_symbols and var not in node.exp.free_symbols:
                out.append(node.base)
    return [a for a in dict.fromkeys(out) if sp.diff(a, var) not in (0, 1)]


def _diagnose_derivative(f: sp.Expr, cur: sp.Expr, var: sp.Symbol, base: str) -> Diagnosis | None:
    """Derivative slips: product rule, chain rule, power rule, integrated instead."""
    right = sp.diff(f, var)
    fix = pretty(right)
    close = lambda e: _is_zero(cur - e, var)  # noqa: E731

    coeff, g = f.as_independent(var, as_Add=False)
    facs = [t for t in sp.Mul.make_args(g) if var in t.free_symbols]
    if len(facs) >= 2:
        u, v = facs[0], sp.Mul(*facs[1:])
        du, dv = sp.diff(u, var), sp.diff(v, var)
        if close(coeff * du * dv):
            return Diagnosis(
                "product_rule", f"product rule: multiplied the derivatives: {base}",
                {1: "Check the product rule", 2: "(uv)' is u'v + uv', not u'v'", 3: f"= {fix}"}, fix=fix)
        if close(coeff * du * v) or close(coeff * u * dv):
            return Diagnosis(
                "product_rule", f"product rule: forgot a term: {base}",
                {1: "Check the product rule", 2: "Product rule has two terms: u'v + uv'", 3: f"= {fix}"},
                fix=fix)

    for h in _inner_args(f, var):
        dh = sp.diff(h, var)
        if _is_zero(cur * dh - right, var):
            return Diagnosis(
                "chain_rule", f"chain rule: missing inner derivative {pretty(dh)}: {base}",
                {1: "Check the chain rule", 2: f"Multiply by the derivative of {pretty(h)}", 3: f"= {fix}"},
                fix=fix)

    mono_in, mono_out = _monomial(f, var), _monomial(cur, var)
    if mono_in and mono_out and mono_in[1] not in (0, 1):
        (a, n), (k, m) = mono_in, mono_out
        if sp.simplify(m - n) == 0:
            return Diagnosis(
                "power_rule", f"power rule: did not lower the power: {base}",
                {1: "Check the power rule", 2: "Lower the power by one", 3: f"= {fix}"}, fix=fix)
        if sp.simplify(m - n + 1) == 0 and sp.simplify(k - a * n) != 0:
            return Diagnosis(
                "power_rule", f"power rule: coefficient should be {pretty(a * n)}: {base}",
                {1: "Check the power rule", 2: "Bring the old power down in front", 3: f"= {fix}"}, fix=fix)

    return None


def _diagnose_integrated(f: sp.Expr, cur: sp.Expr, var: sp.Symbol, base: str) -> Diagnosis | None:
    """Wrote an antiderivative where a derivative was asked (runs after the sign checks)."""
    if var in cur.free_symbols and _is_zero(sp.diff(cur, var) - f, var):
        fix = pretty(sp.diff(f, var))
        return Diagnosis(
            "integrated", f"integrated instead of differentiated: {base}",
            {1: "Differentiate here, don't integrate", 2: "This is an antiderivative, not d/dx",
             3: f"= {fix}"}, fix=fix)
    return None


def _diagnose_definite(f: sp.Expr, var: sp.Symbol, a: sp.Expr, b: sp.Expr, cur: sp.Expr,
                       bracket: tuple | None, line_no: int) -> Diagnosis | None:
    """Definite integral slips: wrong antiderivative in [F]_a^b, limits, forgot to evaluate."""
    F = sp.integrate(f, var)
    value = sp.simplify(F.subs(var, b) - F.subs(var, a))
    good_bracket = pretty_bracket(F, a, b)
    if bracket is not None:
        G, v, ga, gb = bracket
        if not _is_zero(sp.diff(G, v) - f.subs(var, v), v):
            inner = diagnose(sp.Integral(f, var), G.subs(v, var), _Comparator(var, True), line_no, False)
            h = dict(inner.hints)
            h[3] = f"= {good_bracket}"
            return Diagnosis("antiderivative", f"antiderivative inside the bracket is wrong ({inner.verdict})",
                             h, fix=good_bracket)
        if sp.simplify(ga - a) != 0 or sp.simplify(gb - b) != 0:
            return Diagnosis("limits", f"bracket limits {pretty(ga)}..{pretty(gb)}, integral has {pretty(a)}..{pretty(b)}",
                             {1: "Check the limits", 2: "Use the same limits as the integral",
                              3: f"= {good_bracket}"}, fix=good_bracket)
    if var in cur.free_symbols and _is_zero(sp.diff(cur, var) - f, var):
        return Diagnosis("no_limits", f"antiderivative not evaluated at {pretty(a)}..{pretty(b)}",
                         {1: "Now use the limits", 2: "Plug in top minus bottom", 3: f"= {good_bracket}"},
                         fix=good_bracket)
    if var not in cur.free_symbols:
        lo, hi = F.subs(var, a), F.subs(var, b)
        if value != 0 and _is_zero(cur + value, var):
            return Diagnosis("limits", f"limits swapped: got {pretty(cur)}, expected {pretty(value)}",
                             {1: "Check the order of the limits", 2: "It's F(top) minus F(bottom)",
                              3: f"= {pretty(value)}"}, fix=pretty(value))
        if sp.simplify(lo) != 0 and _is_zero(cur - hi, var):
            return Diagnosis("limits", f"lower limit not subtracted: got {pretty(cur)}, expected {pretty(value)}",
                             {1: "What about the lower limit?", 2: "Subtract F at the bottom limit",
                              3: f"= {pretty(value)}"}, fix=pretty(value))
    return None


def _trig_sign_hints(t: sp.Expr, fix: str) -> dict[int, str]:
    rule = "d/dx cos x is −sin x" if t.has(sp.sin) else "d/dx sin x is +cos x"
    return {1: "Check the sign of that trig derivative", 2: rule, 3: f"= {fix}"}


def _with_c(text: str, final: bool) -> str:
    return f"{text} + C" if final else text


def diagnose(prev: sp.Expr, cur: sp.Expr, cmp: _Comparator, line_no: int, final: bool,
             bracket: tuple | None = None) -> Diagnosis:
    """Explain why `cur` does not follow from `prev`. Detectors run most-specific first."""
    var = cmp.var
    S, D = cmp.norm(cur), cmp.norm(prev)
    what = f"d/dx(line {line_no})" if cmp.up_to_constant else f"line {line_no}"
    base = f"{what} = {pretty(S)}, expected {pretty(D)}"
    integrand = _bare_integrand(prev)

    definite = _bare_definite(prev)
    if definite is not None and not cur.has(sp.Integral):
        d = _diagnose_definite(*definite, cur, bracket, line_no)
        if d:
            return d
    fn = _bare_derivative(prev)
    if fn is not None and not cur.has(sp.Derivative):
        d = _diagnose_derivative(fn, cur, var, base)
        if d:
            return d
    trig = cmp.kind == "derivative" and bool(cur.atoms(sp.sin, sp.cos))

    # Differentiated instead of integrated: ∫ x^2 dx = 2x (but ∫ sin x = cos x is a sign slip)
    if integrand is not None and not cur.has(sp.Integral) and not cmp.same(-cur, prev):
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
        if trig:
            return Diagnosis("trig_sign", f"sign error on a trig derivative: {base}",
                             _trig_sign_hints(cur, fix), fix=fix)
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
                if trig and t.atoms(sp.sin, sp.cos):
                    return Diagnosis("trig_sign", f"sign error on trig derivative term {pretty(t)}: {base}",
                                     _trig_sign_hints(t, fix), fix=fix)
                return Diagnosis(
                    "sign", f"sign error on term {pretty(t)}: {base}",
                    {1: "Check your signs here",
                     2: f"Sign slip on the {pretty(-t if t.could_extract_minus_sign() else t)} term",
                     3: f"= {fix}"},
                    fix=fix)

    if fn is not None and not cur.has(sp.Derivative):
        d = _diagnose_integrated(fn, cur, var, base)
        if d:
            return d

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
    numeric = var not in S.free_symbols and var not in D.free_symbols
    r = None if numeric else _const_ratio(S, D, var)
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
    if numeric and cmp.kind == "definite":
        return Diagnosis(
            "arithmetic", f"arithmetic: {base}",
            {1: "Check your arithmetic", 2: "Recompute F(top) − F(bottom)",
             3: f"= {fix}" if fix else "Recompute F(top) − F(bottom)"}, fix=fix)
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
    chain: str = "integral"  # integral | definite | derivative | algebra (the last problem)
    bracket_pending: bool = False  # last correct step is [F]_a^b: the number is still to write

    @property
    def wrong(self) -> LineResult | None:
        return next((l for l in self.lines if l.status == "wrong"), None)


def _as_items(lines: list[Any]) -> list[tuple[str, str]]:
    return [(f"L{i}", ln) if isinstance(ln, str) else (str(ln.get("id") or f"L{i}"), str(ln.get("text") or ""))
            for i, ln in enumerate(lines, 1)]


def _bracket_ok(prev: sp.Expr, bracket: tuple | None) -> bool:
    """[F]_a^b right after ∫_a^b f dx: F' must be f (even if the number happens to match)."""
    definite = _bare_definite(prev)
    if definite is None or bracket is None:
        return True
    f, var, _, _ = definite
    G, v, _, _ = bracket
    return _is_zero(sp.diff(G, v) - f.subs(var, v), v)


def check_lines(lines: list[Any]) -> CheckResult:
    """Check every step. `lines` are strings or {id, text} dicts, top to bottom.

    Only the last chain on the page (from the last line that starts a new problem)
    drives hints; earlier problems are still checked. A problem is an indefinite
    integral, a definite integral, a derivative (d/dx, (..)', f'(x) after f(x) = ...),
    or a definition line f(x) = ... / y = ...
    """
    results: list[LineResult] = []
    prev: sp.Expr | None = None
    last_good: sp.Expr | None = None
    var = X
    integral_chain = False
    chain = "algebra"
    first_wrong: str | None = None
    verdict = ""
    step_no = 0
    defs: dict[str, tuple[sp.Expr, sp.Symbol]] = {}
    bracket_pending = False

    for line_id, text in _as_items(lines):
        step_no += 1
        p = parse_line(text, defs)
        res = LineResult(line_id, text, latex_of(p), "unchecked", p)
        results.append(res)
        if not p.ok:
            continue
        if p.defines:
            defs[p.defines] = (p.parts[0], p.var)
        head = p.parts[0]
        starts_new = not p.continuation and (
            prev is None or p.defines is not None or chain_kind(head) != "algebra")
        if starts_new:
            prev, var = head, p.var
            bracket_pending = False
            chain = chain_kind(head)
            integral_chain = chain == "integral"
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
            integral_chain = integral_chain or _has_indef(expr)
            cmp = _Comparator(var, integral_chain, chain)
            final = integral_chain and not expr.has(sp.Integral)
            bracket = p.brackets.get(k + offset)
            if _bracket_ok(prev, bracket) and cmp.same(expr, prev):
                prev = last_good = expr
                bracket_pending = bracket is not None
                if final and not has_c:
                    res.note = "Don't forget + C"
                elif has_c:
                    res.note = None
                continue
            status = "wrong"
            res.diagnosis = diagnose(prev, expr, cmp, step_no, final, bracket)
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

    if chain == "integral":
        finished = last_good is not None and not last_good.has(sp.Integral)
    elif chain in ("definite", "derivative"):
        finished = (last_good is not None and not last_good.has(sp.Integral, sp.Derivative)
                    and not bracket_pending)
    else:
        finished = False
    if not verdict:
        verdict = "all steps check out" if any(r.status in ("ok", "start") for r in results) else "nothing to check"
    return CheckResult(results, first_wrong, verdict, var, last_good, finished, integral_chain, chain,
                       bracket_pending)


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


def _product_split(f: sp.Expr, var: sp.Symbol) -> tuple[sp.Expr, sp.Expr] | None:
    """f = u·v with both factors depending on var (constant kept on u), else None."""
    coeff, g = f.as_independent(var, as_Add=False)
    facs = [t for t in sp.Mul.make_args(g) if var in t.free_symbols]
    if len(facs) < 2 or g.is_Pow:
        return None
    return coeff * facs[0], sp.Mul(*facs[1:])


def _wrap(e: sp.Expr) -> str:
    t = pretty(e)
    return f"({t})" if e.is_Add or t.startswith("-") else t


def _solve_derivative(value: sp.Expr, var: sp.Symbol) -> list[str]:
    """`d/dx (u v)` -> product-rule line, then the result. Other derivatives go straight to it."""
    if not value.has(sp.Derivative):
        return []
    steps = []
    f = _bare_derivative(value)
    split = _product_split(f, var) if f is not None else None
    if split:
        u, v = split
        steps.append(f"= ({pretty(u)})' {_wrap(v)} + {_wrap(u)} ({pretty(v)})'")
    result = evaluate(value)
    if result.has(sp.Derivative):
        return steps
    steps.append(f"= {pretty(result)}")
    return steps


def _solve_definite(value: sp.Expr, var: sp.Symbol, pending: bool = False) -> list[str]:
    """`∫_a^b f dx` -> `[F]_a^b` -> number. A bracket already written goes straight to the number."""
    definite = _bare_definite(value)
    steps = []
    if definite is not None:
        f, v, a, b = definite
        F = sp.integrate(f, v)
        if F.has(sp.Integral):
            return []
        steps.append(f"= {pretty_bracket(F, a, b)}")
        total = sp.simplify(F.subs(v, b) - F.subs(v, a))
    else:
        if value.has(sp.Integral):
            return []
        total = sp.simplify(evaluate(value))
        if total == value and not pending:
            return []
    steps.append(f"= {pretty(total)}")
    return steps


def solve_remaining(lines: list[Any]) -> list[str]:
    """Steps (as ink text, each starting with '=') that finish the last problem.

    Continues from the last correct expression. Integration by parts gets a readable
    2-3 step path; anything else jumps straight to `= result + C`.
    """
    res = lines if isinstance(lines, CheckResult) else check_lines(lines)
    value, var = res.base_value, res.var
    if value is None:
        return []
    if res.chain == "derivative":
        return _solve_derivative(value, var)
    if res.chain == "definite":
        return _solve_definite(value, var, res.bracket_pending)
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
    if value is None:
        return None
    if res.chain == "derivative":
        return _derivative_hints(res)
    if res.chain == "definite":
        steps = solve_remaining(res)
        if res.bracket_pending and steps:
            return {1: "Now plug in the limits", 2: "Work out F(top) − F(bottom)", 3: f"Next: {steps[0]}"}
        if _bare_definite(value) is None:
            return None
        nxt = f"Next: {steps[0]}" if steps else "Find an antiderivative"
        return {1: "Find an antiderivative first", 2: "Then do F(top) − F(bottom)", 3: nxt}
    if not value.has(sp.Integral):
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


def _derivative_hints(res: CheckResult) -> dict[int, str] | None:
    value, var = res.base_value, res.var
    f = _bare_derivative(value)
    if f is None:
        return None
    steps = solve_remaining(res)
    nxt = f"Next: {steps[0]}" if steps else "Differentiate what's left"
    if f.is_Add:
        return {1: "Differentiate term by term", 2: "Take d/dx of each term separately", 3: nxt}
    if _product_split(f, var):
        return {1: "Try the product rule", 2: "(uv)' = u'v + uv'", 3: nxt}
    if _inner_args(f, var):
        return {1: "Use the chain rule", 2: "Outer derivative times inner derivative", 3: nxt}
    return {1: "Use a derivative rule you know", 2: "x^n differentiates to n x^(n-1)", 3: nxt}


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
