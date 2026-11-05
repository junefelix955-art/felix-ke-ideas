"""A small, dependency-free maths engine with step-by-step working.

Supports: arithmetic, simplify/expand polynomials, linear / quadratic / (rational-root) polynomial equations,
2x2 linear systems, derivatives, indefinite integrals of common forms, and basic statistics.
Everything is exact (fractions) where possible. Anything else raises SolveError with a friendly message."""
import math
import re
from fractions import Fraction
from itertools import product

FUNCS = {"sin", "cos", "tan", "ln", "log", "exp", "sqrt", "abs"}
CONSTS = {"pi": math.pi, "e": math.e}
MAX_LEN = 300


class SolveError(Exception):
    pass


# ------------------------------------------------------------------ parsing
def normalise(text):
    t = text.strip()
    for a, b in (("−", "-"), ("–", "-"), ("×", "*"), ("·", "*"), ("÷", "/"), ("**", "^"), ("π", "pi"), ("√", "sqrt"),
                 ("²", "^2"), ("³", "^3"), ("⁴", "^4"), ("＝", "="), ("，", ","), ("−", "-")):
        t = t.replace(a, b)
    t = re.sub(r"(?<=\d),(?=\d{3}(\D|$))", "", t)  # 1,000 -> 1000
    t = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s+", r"(\1/100)*", t, flags=re.I)  # 15% of 240 -> (15/100)*240
    t = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s*(?:degrees|degree|deg|°)", r"(\1*pi/180)", t, flags=re.I)  # 30 degrees -> radians

    def fact(m):
        n = int(m.group(1))
        if n > 25:
            raise SolveError("Factorials above 25! are too large for this solver.")
        return "(%d)" % math.factorial(n)
    t = re.sub(r"(\d+)\s*!(?!=)", fact, t)  # 5! -> (120)
    return t


def tokenize(s):
    toks, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch.isspace():
            i += 1
        elif ch.isdigit() or (ch == "." and i + 1 < len(s) and s[i + 1].isdigit()):
            m = re.match(r"\d*\.?\d*", s[i:])
            toks.append(("num", Fraction(m.group(0))))
            i += len(m.group(0))
        elif ch.isalpha():
            m = re.match(r"[A-Za-z]+", s[i:])
            word = m.group(0)
            i += len(word)
            low = word.lower()
            before = len(toks)
            while word:  # split "xy" -> x, y but keep function and constant names whole
                for name in sorted(FUNCS | set(CONSTS), key=len, reverse=True):
                    if word.lower().startswith(name) and (name in FUNCS or name == "pi" or len(word) == 1 or name == "e"):
                        toks.append(("fn" if name in FUNCS else "const", name))
                        word = word[len(name):]
                        break
                else:
                    toks.append(("var", word[0].lower()))
                    word = word[1:]
            if sum(1 for t in toks[before:] if t[0] == "var") > 2:
                raise SolveError("That does not look like a maths problem. Use symbols like x, +, -, =, or words such as "
                                 "'derivative of', 'integral of' or 'mean of'.")
        elif ch in "+-*/^(),=!":
            toks.append((ch, ch))
            i += 1
        else:
            raise SolveError("I do not understand the symbol '%s'." % ch)
    return toks


def num(v):
    return ("num", Fraction(v))


class Parser:
    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self):
        return self.t[self.i][0] if self.i < len(self.t) else None

    def take(self):
        tok = self.t[self.i]
        self.i += 1
        return tok

    def parse(self):
        node = self.expr()
        if self.i != len(self.t):
            raise SolveError("I could not read part of that expression.")
        return node

    def expr(self):
        node = self.term()
        while self.peek() in ("+", "-"):
            op = self.take()[0]
            rhs = self.term()
            node = ("add", node, rhs if op == "+" else ("neg", rhs))
        return node

    def starts_atom(self):
        return self.peek() in ("num", "var", "const", "fn", "(")

    def term(self):
        node = self.unary()
        while True:
            p = self.peek()
            if p == "*":
                self.take()
                node = ("mul", node, self.unary())
            elif p == "/":
                self.take()
                node = ("div", node, self.unary())
            elif self.starts_atom():  # implicit multiplication: 2x, 3(x+1), (a)(b)
                node = ("mul", node, self.unary())
            else:
                return node

    def unary(self):
        if self.peek() == "-":
            self.take()
            return ("neg", self.unary())
        if self.peek() == "+":
            self.take()
            return self.unary()
        return self.power()

    def power(self):
        base = self.atom()
        if self.peek() == "^":
            self.take()
            ex = self.unary()  # right associative, allows 2^-1
            return ("fn", "exp", ex) if base == ("const", "e") else ("pow", base, ex)
        return base

    def atom(self):
        p = self.peek()
        if p is None:
            raise SolveError("The expression ends too early.")
        kind, val = self.take()
        if kind == "num":
            return ("num", val)
        if kind == "var":
            return ("var", val)
        if kind == "const":
            return ("const", val)
        if kind == "(":
            node = self.expr()
            if self.peek() != ")":
                raise SolveError("A bracket is not closed.")
            self.take()
            return node
        if kind == "fn":
            if self.peek() == "(":
                self.take()
                arg = self.expr()
                if self.peek() != ")":
                    raise SolveError("A bracket is not closed.")
                self.take()
            else:
                arg = self.power()
            node = ("fn", val, arg)
            return node
        raise SolveError("I could not read that expression.")


def parse(text):
    text = normalise(text)
    if not text:
        raise SolveError("Type or scan a problem first.")
    if len(text) > MAX_LEN:
        raise SolveError("That problem is too long. Try a shorter one.")
    return Parser(tokenize(text)).parse()


# ------------------------------------------------------------------ polynomial form
def variables(n, acc=None):
    acc = set() if acc is None else acc
    if n[0] == "var":
        acc.add(n[1])
    elif n[0] in ("add", "mul", "div", "pow"):
        variables(n[1], acc), variables(n[2], acc)
    elif n[0] == "neg":
        variables(n[1], acc)
    elif n[0] == "fn":
        variables(n[2], acc)
    return acc


def poly(n, v):
    """Return {power: Fraction} if n is a polynomial in v with rational coefficients, else None."""
    k = n[0]
    if k == "num":
        return {0: n[1]} if n[1] else {}
    if k == "var":
        return {1: Fraction(1)} if n[1] == v else None
    if k == "neg":
        p = poly(n[1], v)
        return None if p is None else {e: -c for e, c in p.items()}
    if k == "add":
        a, b = poly(n[1], v), poly(n[2], v)
        if a is None or b is None:
            return None
        out = dict(a)
        for e, c in b.items():
            out[e] = out.get(e, 0) + c
        return {e: c for e, c in out.items() if c}
    if k == "mul":
        a, b = poly(n[1], v), poly(n[2], v)
        if a is None or b is None:
            return None
        out = {}
        for (e1, c1), (e2, c2) in product(a.items(), b.items()):
            out[e1 + e2] = out.get(e1 + e2, 0) + c1 * c2
        return {e: c for e, c in out.items() if c}
    if k == "div":
        a, b = poly(n[1], v), poly(n[2], v)
        if a is None or b is None or set(b) - {0} or not b:
            return None
        return {e: c / b[0] for e, c in a.items()}
    if k == "pow":
        base, ex = poly(n[1], v), n[2]
        if base is None or ex[0] != "num" or ex[1].denominator != 1 or not 0 <= ex[1] <= 12:
            return None
        out = {0: Fraction(1)}
        for _ in range(int(ex[1])):
            nxt = {}
            for (e1, c1), (e2, c2) in product(out.items(), base.items()):
                nxt[e1 + e2] = nxt.get(e1 + e2, 0) + c1 * c2
            out = {e: c for e, c in nxt.items() if c}
        return out
    return None


def from_poly(p, v):
    if not p:
        return num(0)
    node = None
    for e in sorted(p, reverse=True):
        c = p[e]
        x = ("num", abs(c)) if e == 0 else (("var", v) if e == 1 else ("pow", ("var", v), num(e)))
        term = x if e == 0 else (x if abs(c) == 1 else ("mul", ("num", abs(c)), x))
        if node is None:
            node = ("neg", term) if c < 0 else term
        else:
            node = ("add", node, ("neg", term) if c < 0 else term)
    return node


# ------------------------------------------------------------------ printing
def fmt_num(f):
    if f.denominator == 1:
        return str(f.numerator)
    return "%d/%d" % (f.numerator, f.denominator)


PREC = {"add": 1, "mul": 2, "div": 2, "neg": 3, "pow": 4}


def to_str(n, parent=0):
    k = n[0]
    if k == "num":
        s = fmt_num(n[1])
        return "(%s)" % s if n[1].denominator != 1 and parent >= 2 else s
    if k in ("var", "const"):
        return n[1]
    if k == "fn":
        return "|%s|" % to_str(n[2]) if n[1] == "abs" else "%s(%s)" % (n[1], to_str(n[2]))
    if k == "neg":
        s = "-" + to_str(n[1], 3)
        return "(%s)" % s if parent > 1 else s
    if k == "add":
        a = to_str(n[1], 1)
        if n[2][0] == "neg":
            s = "%s - %s" % (a, to_str(n[2][1], 2))
        elif n[2][0] == "num" and n[2][1] < 0:
            s = "%s - %s" % (a, fmt_num(-n[2][1]))
        else:
            s = "%s + %s" % (a, to_str(n[2], 1))
    elif k == "mul":
        a, b = n[1], n[2]
        if a[0] == "num" and a[1].denominator != 1:
            p, q = abs(a[1].numerator), a[1].denominator
            inner = to_str(b, 2)
            top = inner if p == 1 else (str(p) + inner if b[0] in ("var", "const", "pow") else "%d*%s" % (p, inner))
            s = ("-" if a[1] < 0 else "") + top + "/" + str(q)
            return "(%s)" % s if (parent > 1 if a[1] < 0 else parent >= 2) else s
        if a[0] == "num" and b[0] in ("var", "const", "pow") and a[1].denominator == 1:
            s = to_str(a, 2) + to_str(b, 2)
        else:
            s = "%s*%s" % (to_str(a, 2), to_str(b, 3))
    elif k == "div":
        s = "%s/%s" % (to_str(n[1], 2), to_str(n[2], 3))
    elif k == "pow":
        s = "%s^%s" % (to_str(n[1], 5), to_str(n[2], 5))
        return s
    else:
        s = "?"
    return "(%s)" % s if PREC[k] < parent else s


# ------------------------------------------------------------------ simplification
def simp(n):
    """Light simplification: constant folding and identities (exact)."""
    k = n[0]
    if k in ("num", "var", "const"):
        return n
    if k == "fn":
        a = simp(n[2])
        if a[0] == "num":
            if n[1] == "exp" and a[1] == 0:
                return num(1)
            if n[1] in ("sin", "tan", "sqrt", "abs") and a[1] == 0:
                return num(0)
            if n[1] == "cos" and a[1] == 0:
                return num(1)
            if n[1] == "ln" and a[1] == 1:
                return num(0)
            if n[1] == "sqrt" and a[1] >= 0:
                r = exact_sqrt(a[1])
                if r is not None:
                    return num(r)
        return ("fn", n[1], a)
    if k == "neg":
        a = simp(n[1])
        if a[0] == "num":
            return num(-a[1])
        if a[0] == "neg":
            return a[1]
        return ("neg", a)
    a, b = simp(n[1]), simp(n[2])
    if k == "add":
        if a[0] == "num" and b[0] == "num":
            return num(a[1] + b[1])
        if a == num(0):
            return b
        if b == num(0):
            return a
        if b[0] == "neg" and b[1] == a:
            return num(0)
        return ("add", a, b)
    if k == "mul":
        if a[0] == "num" and b[0] == "num":
            return num(a[1] * b[1])
        if a == num(0) or b == num(0):
            return num(0)
        if a == num(1):
            return b
        if b == num(1):
            return a
        if a[0] == "neg":
            return simp(("neg", ("mul", a[1], b)))
        if b[0] == "neg":
            return simp(("neg", ("mul", a, b[1])))
        if a[0] == "div" and a[1] == num(1):
            return simp(("div", b, a[2]))
        if b[0] == "div" and b[1] == num(1):
            return simp(("div", a, b[2]))
        if b[0] == "num" and a[0] != "num":
            return ("mul", b, a)
        if a[0] == "num" and b[0] == "mul" and b[1][0] == "num":
            return simp(("mul", num(a[1] * b[1][1]), b[2]))
        return ("mul", a, b)
    if k == "div":
        if b[0] == "num":
            if b[1] == 0:
                raise SolveError("Division by zero.")
            return simp(("mul", num(1 / b[1]), a))
        if a == num(0):
            return num(0)
        if a[0] in ("add", "neg"):
            a = clean(a)
        return ("div", a, b)
    if k == "pow":
        if b == num(0):
            return num(1)
        if b == num(1):
            return a
        if a[0] == "num" and b[0] == "num" and b[1].denominator == 1 and abs(b[1]) <= 64 and (a[1] != 0 or b[1] > 0):
            return num(a[1] ** int(b[1]))
        if a == num(1):
            return num(1)
        return ("pow", a, b)
    return n


def clean(n, v=None):
    """Simplify; if the result is a polynomial in the variable, show it expanded in descending order."""
    n = simp(n)
    vs = variables(n)
    if len(vs) == 1:
        p = poly(n, next(iter(vs)))
        if p is not None:
            return from_poly(p, next(iter(vs)))
    elif not vs:
        pass
    return n


def exact_sqrt(f):
    if f < 0:
        return None
    a, b = math.isqrt(f.numerator), math.isqrt(f.denominator)
    return Fraction(a, b) if a * a == f.numerator and b * b == f.denominator else None


# ------------------------------------------------------------------ numeric evaluation
def evaluate(n, env=None):
    env = env or {}
    k = n[0]
    if k == "num":
        return n[1]
    if k == "const":
        return CONSTS[n[1]]
    if k == "var":
        if n[1] in env:
            return env[n[1]]
        raise SolveError("The letter '%s' needs a value." % n[1])
    if k == "neg":
        return -evaluate(n[1], env)
    a = evaluate(n[1], env) if k != "fn" else None
    if k == "fn":
        x = float(evaluate(n[2], env))
        try:
            return {"sin": math.sin, "cos": math.cos, "tan": math.tan, "ln": math.log, "log": math.log10,
                    "exp": math.exp, "sqrt": math.sqrt, "abs": abs}[n[1]](x)
        except (ValueError, OverflowError):
            raise SolveError("That value is outside the domain of %s." % n[1])
    b = evaluate(n[2], env)
    if k == "add":
        return a + b
    if k == "mul":
        return a * b
    if k == "div":
        if b == 0:
            raise SolveError("Division by zero is undefined.")
        return a / b
    if k == "pow":
        if isinstance(a, Fraction) and isinstance(b, Fraction) and b.denominator == 1 and abs(b) <= 200 and abs(a) < 10 ** 6:
            if a == 0 and b < 0:
                raise SolveError("Division by zero is undefined.")
            return a ** int(b)
        try:
            r = float(a) ** float(b)
        except (OverflowError, ZeroDivisionError):
            raise SolveError("That number is too large or undefined.")
        if isinstance(r, complex):
            raise SolveError("The result is not a real number.")
        return r
    raise SolveError("I could not evaluate that.")


def show_value(x):
    if isinstance(x, Fraction):
        if x.denominator == 1:
            return str(x.numerator)
        return "%s  (= %s)" % (fmt_num(x), trim(float(x)))
    return trim(x)


def trim(x):
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return ("%.6f" % x).rstrip("0").rstrip(".")


# ------------------------------------------------------------------ algebra
def split_equation(text):
    parts = text.split("=")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise SolveError("An equation needs exactly one '=' sign with something on both sides.")
    return parse(parts[0]), parse(parts[1])


def rat_str(f):
    return fmt_num(f)


def solve_poly(coeffs, v, steps):
    """coeffs {power: Fraction}. Returns list of root strings (real and complex)."""
    deg = max(coeffs) if coeffs else 0
    if deg == 0:
        raise SolveError("There is no %s in that equation." % v)
    if deg == 1:
        a, b = coeffs.get(1, 0), coeffs.get(0, 0)
        steps.append("This is a linear equation: %s%s %s= 0" % (("" if a == 1 else fmt_num(a)), v, ("+ %s " % fmt_num(b) if b > 0 else "- %s " % fmt_num(-b)) if b else ""))
        if b:
            steps.append("Move the constant to the other side: %s%s = %s" % ("" if a == 1 else fmt_num(a), v, fmt_num(-b)))
        if a != 1:
            steps.append("Divide both sides by %s: %s = %s" % (fmt_num(a), v, fmt_num(-b / a)))
        return [fmt_num(-b / a)]
    if deg == 2:
        a, b, c = coeffs.get(2, Fraction(0)), coeffs.get(1, Fraction(0)), coeffs.get(0, Fraction(0))
        steps.append("This is a quadratic: a = %s, b = %s, c = %s" % (fmt_num(a), fmt_num(b), fmt_num(c)))
        d = b * b - 4 * a * c
        steps.append("Discriminant D = b^2 - 4ac = %s^2 - 4(%s)(%s) = %s" % (fmt_num(b), fmt_num(a), fmt_num(c), fmt_num(d)))
        if d == 0:
            r = -b / (2 * a)
            steps.append("D = 0, so there is one repeated root: %s = -b/(2a) = %s" % (v, fmt_num(r)))
            return [fmt_num(r)]
        if d > 0:
            s = exact_sqrt(d)
            steps.append("D > 0, so there are two real roots: %s = (-b +/- sqrt(D)) / (2a)" % v)
            if s is not None:
                r1, r2 = (-b + s) / (2 * a), (-b - s) / (2 * a)
                steps.append("sqrt(%s) = %s, so %s = (%s +/- %s) / %s" % (fmt_num(d), fmt_num(s), v, fmt_num(-b), fmt_num(s), fmt_num(2 * a)))
                return [fmt_num(r1), fmt_num(r2)]
            sd = math.sqrt(d)
            r1, r2 = (-float(b) + sd) / (2 * float(a)), (-float(b) - sd) / (2 * float(a))
            steps.append("sqrt(%s) is not a whole number, so the roots are irrational:" % fmt_num(d))
            return ["(%s + sqrt(%s)) / %s = %s" % (fmt_num(-b), fmt_num(d), fmt_num(2 * a), trim(r1)),
                    "(%s - sqrt(%s)) / %s = %s" % (fmt_num(-b), fmt_num(d), fmt_num(2 * a), trim(r2))]
        steps.append("D < 0, so there are no real roots; the two complex roots are:")
        re_, im = -float(b) / (2 * float(a)), math.sqrt(-float(d)) / (2 * abs(float(a)))
        return ["%s + %si" % (trim(re_), trim(im)), "%s - %si" % (trim(re_), trim(im))]
    # degree >= 3: rational root theorem
    steps.append("This has degree %d. Try rational roots p/q (p divides the constant, q divides the leading coefficient)." % deg)
    mult = 1
    for c in coeffs.values():
        mult = mult * c.denominator // math.gcd(mult, c.denominator)
    ints = {e: int(c * mult) for e, c in coeffs.items()}
    roots, cur = [], dict(ints)
    while max(cur) > 2:
        const = cur.get(0, 0)
        if const == 0:
            roots.append(Fraction(0))
            cur = {e - 1: c for e, c in cur.items() if e > 0}
            steps.append("0 is a root (no constant term), so factor out %s." % v)
            continue
        lead = cur[max(cur)]
        found = None
        ps = divisors(abs(const))
        qs = divisors(abs(lead))
        for p in ps:
            for q in qs:
                for sgn in (1, -1):
                    r = Fraction(sgn * p, q)
                    if sum(c * r ** e for e, c in cur.items()) == 0:
                        found = r
                        break
                if found is not None:
                    break
            if found is not None:
                break
        if found is None:
            raise SolveError("This polynomial has no simple rational roots, so I cannot solve it exactly here.")
        steps.append("Test %s = %s: the polynomial equals 0, so it is a root. Divide it out." % (v, fmt_num(found)))
        roots.append(found)
        d = max(cur)
        q_, rem = {}, Fraction(0)
        for e in range(d, -1, -1):
            rem = rem * found + cur.get(e, 0)
            if e > 0:
                q_[e - 1] = rem
        cur = {e: c for e, c in q_.items() if c}
    out = [fmt_num(r) for r in roots]
    rest = {e: Fraction(c) for e, c in cur.items()}
    if max(rest) >= 1:
        steps.append("The remaining factor is %s = 0" % to_str(from_poly(rest, v)))
        out += solve_poly(rest, v, steps)
    return out


def divisors(n):
    if n > 10 ** 6:
        raise SolveError("The numbers are too large for me to search for roots.")
    return [d for d in range(1, n + 1) if n % d == 0]


def solve_equation(text):
    lhs, rhs = split_equation(text)
    vs = variables(lhs) | variables(rhs)
    if not vs:
        raise SolveError("There is no unknown letter to solve for.")
    if len(vs) > 1:
        raise SolveError("That has more than one unknown. Give one unknown, or two linear equations separated by ';'.")
    v = next(iter(vs))
    pl, pr = poly(lhs, v), poly(rhs, v)
    steps = ["Equation: %s = %s" % (to_str(clean(lhs)), to_str(clean(rhs)))]
    if pl is None or pr is None:
        return solve_numeric(lhs, rhs, v, steps)
    diff = dict(pl)
    for e, c in pr.items():
        diff[e] = diff.get(e, 0) - c
    diff = {e: c for e, c in diff.items() if c}
    if not diff:
        return steps + ["Both sides are identical, so every value works."], "Infinitely many solutions"
    if max(diff) == 0:
        return steps + ["The %s terms cancel and leave %s = 0, which is false." % (v, fmt_num(diff[0]))], "No solution"
    steps.append("Bring everything to one side: %s = 0" % to_str(from_poly(diff, v)))
    roots = solve_poly(diff, v, steps)
    uniq = []
    for r in roots:
        if r not in uniq:
            uniq.append(r)
    if all(re.fullmatch(r"-?\d+(/\d+)?", r) for r in uniq):
        uniq.sort(key=Fraction)
    answer = "; ".join("%s = %s" % (v, r) for r in uniq)
    # check rational solutions in the original equation
    if len(uniq) and all(re.fullmatch(r"-?\d+(/\d+)?", r) for r in uniq):
        chk = []
        for r in uniq[:3]:
            val = Fraction(r)
            chk.append("%s: left = %s, right = %s" % (r, fmt_num(evaluate(lhs, {v: val})), fmt_num(evaluate(rhs, {v: val}))))
        steps.append("Check by substituting: " + " | ".join(chk))
    return steps, answer


def solve_numeric(lhs, rhs, v, steps):
    """Non-polynomial equation: bisection search for real roots on [-10, 10]."""
    def f(x):
        try:
            return float(evaluate(lhs, {v: x})) - float(evaluate(rhs, {v: x}))
        except (SolveError, ZeroDivisionError, OverflowError, ValueError):
            return None
    steps.append("This is not a polynomial, so I search for real roots numerically between -10 and 10.")
    roots, prev, x = [], None, -10.0
    while x <= 10.0:
        y = f(x)
        if prev is not None and y is not None and prev[1] is not None and prev[1] * y <= 0 and not (prev[1] == 0 and y == 0):
            lo, hi = prev[0], x
            flo = prev[1]
            for _ in range(80):
                mid = (lo + hi) / 2
                fm = f(mid)
                if fm is None:
                    break
                if flo * fm <= 0:
                    hi = mid
                else:
                    lo, flo = mid, fm
            r = (lo + hi) / 2
            if abs(f(r) or 0) < 1e-6 and not any(abs(r - q) < 1e-5 for q in roots):
                roots.append(r)
        prev = (x, y)
        x = round(x + 0.05, 10)
    if not roots:
        raise SolveError("I could not find a real solution between -10 and 10.")
    return steps, "; ".join("%s ≈ %s" % (v, trim(r)) for r in roots[:8]) + ("   (periodic functions have more solutions outside this range)" if len(roots) > 2 else "")


def solve_system(text):
    parts = [p for p in re.split(r"[;\n]|,(?=[^,]*=)", text) if p.strip()]
    if len(parts) != 2:
        raise SolveError("Give exactly two equations separated by ';', for example: x + y = 5; x - y = 1")
    eqs = [split_equation(p) for p in parts]
    vs = sorted(set().union(*[variables(a) | variables(b) for a, b in eqs]))
    if len(vs) != 2:
        raise SolveError("A system of two equations needs exactly two unknown letters.")
    rows, steps = [], []
    for i, (a, b) in enumerate(eqs, 1):
        d = ("add", a, ("neg", b))
        coef = []
        for var in vs:
            other = [w for w in vs if w != var][0]
            p0 = linear_coeff(d, var, other)
            coef.append(p0)
        const = -evaluate_const(d, vs)
        rows.append((coef[0], coef[1], const))
        steps.append("Equation %d: %s %s %s = %s" % (i, fmt_term(coef[0], vs[0]), "+" if coef[1] >= 0 else "-", fmt_term(abs(coef[1]), vs[1]), fmt_num(const)))
    (a1, b1, c1), (a2, b2, c2) = rows
    det = a1 * b2 - a2 * b1
    steps.append("Determinant D = (%s)(%s) - (%s)(%s) = %s" % (fmt_num(a1), fmt_num(b2), fmt_num(a2), fmt_num(b1), fmt_num(det)))
    if det == 0:
        if a1 * c2 - a2 * c1 == 0 and b1 * c2 - b2 * c1 == 0:
            return steps + ["D = 0 and the equations are multiples of each other."], "Infinitely many solutions"
        return steps + ["D = 0 but the equations disagree (parallel lines)."], "No solution"
    x = (c1 * b2 - c2 * b1) / det
    y = (a1 * c2 - a2 * c1) / det
    steps.append("%s = (%s*%s - %s*%s) / D = %s" % (vs[0], fmt_num(c1), fmt_num(b2), fmt_num(c2), fmt_num(b1), fmt_num(x)))
    steps.append("%s = (%s*%s - %s*%s) / D = %s" % (vs[1], fmt_num(a1), fmt_num(c2), fmt_num(a2), fmt_num(c1), fmt_num(y)))
    steps.append("Check equation 1: %s*%s + %s*%s = %s" % (fmt_num(a1), fmt_num(x), fmt_num(b1), fmt_num(y), fmt_num(a1 * x + b1 * y)))
    return steps, "%s = %s, %s = %s" % (vs[0], fmt_num(x), vs[1], fmt_num(y))


def fmt_term(c, v):
    return ("" if c == 1 else ("-" if c == -1 else fmt_num(c))) + v


def linear_coeff(d, var, other):
    zero = {other: Fraction(0)}
    f0 = Fraction(evaluate(d, dict(zero, **{var: Fraction(0)})))
    f1 = Fraction(evaluate(d, dict(zero, **{var: Fraction(1)})))
    f2 = Fraction(evaluate(d, dict(zero, **{var: Fraction(2)})))
    if f2 - f1 != f1 - f0:
        raise SolveError("Both equations must be linear (no squares or products of unknowns).")
    return f1 - f0


def evaluate_const(d, vs):
    return Fraction(evaluate(d, {v: Fraction(0) for v in vs}))


# ------------------------------------------------------------------ calculus
def diff(n, v):
    k = n[0]
    if k in ("num", "const"):
        return num(0)
    if k == "var":
        return num(1 if n[1] == v else 0)
    if k == "neg":
        return ("neg", diff(n[1], v))
    if k == "add":
        return ("add", diff(n[1], v), diff(n[2], v))
    if k == "mul":
        return ("add", ("mul", diff(n[1], v), n[2]), ("mul", n[1], diff(n[2], v)))
    if k == "div":
        return ("div", ("add", ("mul", diff(n[1], v), n[2]), ("neg", ("mul", n[1], diff(n[2], v)))), ("pow", n[2], num(2)))
    if k == "pow":
        base, ex = n[1], n[2]
        if v not in variables(ex):
            return ("mul", ("mul", ex, ("pow", base, ("add", ex, num(-1)))), diff(base, v))
        if v not in variables(base):
            return ("mul", ("mul", n, ("fn", "ln", base)), diff(ex, v))
        raise SolveError("I cannot differentiate a variable raised to a variable power yet.")
    if k == "fn":
        u = n[2]
        du = diff(u, v)
        name = n[1]
        outer = {"sin": ("fn", "cos", u), "cos": ("neg", ("fn", "sin", u)), "exp": n, "ln": ("div", num(1), u),
                 "tan": ("div", num(1), ("pow", ("fn", "cos", u), num(2))),
                 "sqrt": ("div", num(1), ("mul", num(2), ("fn", "sqrt", u))),
                 "log": ("div", num(1), ("mul", u, ("fn", "ln", num(10))))}.get(name)
        if outer is None:
            raise SolveError("I cannot differentiate %s yet." % name)
        return ("mul", outer, du)
    raise SolveError("I cannot differentiate that.")


def top_terms(n):
    if n[0] == "add":
        return top_terms(n[1]) + top_terms(n[2])
    return [n]


def rule_name(t, v):
    t = simp(t)
    k = t[0]
    if v not in variables(t):
        return "constant rule"
    if k == "neg":
        return rule_name(t[1], v)
    if k == "var" or (k == "pow" and t[1] == ("var", v) and v not in variables(t[2])):
        return "power rule"
    if k == "mul":
        if v not in variables(t[1]) or v not in variables(t[2]):
            return "constant multiple + " + rule_name(t[2] if v not in variables(t[1]) else t[1], v)
        return "product rule"
    if k == "div":
        return "quotient rule"
    if k == "fn" or k == "pow":
        return "chain rule"
    return "differentiation rules"


def derivative(expr_text):
    expr = parse(expr_text)
    vs = variables(expr)
    if len(vs) > 1:
        raise SolveError("Which letter should I differentiate by? Use only one variable.")
    v = next(iter(vs)) if vs else "x"
    steps = ["Differentiate f(%s) = %s with respect to %s." % (v, to_str(clean(expr)), v)]
    terms = top_terms(simp(expr)) if poly(expr, v) is None else top_terms(from_poly(poly(expr, v), v))
    results = []
    for t in terms:
        d = clean(diff(t, v))
        results.append(d)
        steps.append("d/d%s [%s] = %s   (%s)" % (v, to_str(t), to_str(d), rule_name(t, v)))
    total = results[0]
    for r in results[1:]:
        total = ("add", total, r)
    total = clean(total)
    if len(terms) > 1:
        steps.append("Add the terms: f'(%s) = %s" % (v, to_str(total)))
    return steps, "f'(%s) = %s" % (v, to_str(total))


def integrate_term(t, v):
    """Antiderivative of one term (without + C), or raise SolveError."""
    t = simp(t)
    if v not in variables(t):
        return ("mul", t, ("var", v))
    if t[0] == "neg":
        return ("neg", integrate_term(t[1], v))
    if t[0] == "mul" and v not in variables(t[1]):
        return ("mul", t[1], integrate_term(t[2], v))
    if t[0] == "mul" and v not in variables(t[2]):
        return ("mul", t[2], integrate_term(t[1], v))
    if t[0] == "div" and v not in variables(t[2]):
        return ("div", integrate_term(t[1], v), t[2])
    p = poly(t, v)
    if p is not None and len(p) == 1:
        e, c = next(iter(p.items()))
        return ("mul", num(c / (e + 1)), ("pow", ("var", v), num(e + 1))) if e != -1 else None
    if t[0] == "div" and t[1] == num(1):
        lin = poly(t[2], v)
        if lin is not None and max(lin) == 1:
            return ("div", ("fn", "ln", ("fn", "abs", t[2])), num(lin[1]))
    if t[0] == "pow" and t[1] == ("var", v) and t[2][0] == "num" and t[2][1] == -1:
        return ("fn", "ln", ("fn", "abs", ("var", v)))
    if t[0] == "pow" and t[2][0] == "num" and t[2][1] != -1:
        lin = poly(t[1], v)
        if lin is not None and max(lin) == 1:
            e = t[2][1] + 1
            return ("div", ("pow", t[1], num(e)), num(e * lin[1]))
    if t[0] == "fn":
        lin = poly(t[2], v)
        if lin is not None and lin and max(lin) == 1:
            a = num(lin[1])
            inner = {"sin": ("neg", ("fn", "cos", t[2])), "cos": ("fn", "sin", t[2]), "exp": t}.get(t[1])
            if inner is not None:
                return inner if lin[1] == 1 else ("div", inner, a)
    raise SolveError("That integral needs a technique I do not have yet (such as substitution or by parts).")


def antiderivative(expr_text):
    """Return (steps, F, v) where F is the antiderivative (without +C) as a syntax tree."""
    expr = parse(expr_text)
    vs = variables(expr)
    if len(vs) > 1:
        raise SolveError("Use only one variable to integrate.")
    v = next(iter(vs)) if vs else "x"
    steps = ["Integrate f(%s) = %s with respect to %s." % (v, to_str(clean(expr)), v)]
    p = poly(expr, v)
    terms = top_terms(from_poly(p, v)) if p is not None else top_terms(simp(expr))
    parts = []
    for t in terms:
        F = integrate_term(t, v)
        if F is None:
            F = ("fn", "ln", ("fn", "abs", ("var", v)))
            parts.append(clean(F))
            steps.append("The integral of 1/%s is ln|%s|." % (v, v))
            continue
        F = clean(F)
        parts.append(F)
        steps.append("integral of [%s] d%s = %s" % (to_str(t), v, to_str(F)))
    total = parts[0]
    for q in parts[1:]:
        total = ("add", total, q)
    return steps, clean(total), v


def integral(expr_text):
    steps, total, v = antiderivative(expr_text)
    steps.append("Add the terms and the constant of integration C.")
    return steps, "integral = %s + C" % to_str(total)


def definite_integral(expr_text, lo_text, hi_text):
    lo, hi = evaluate(parse(lo_text)), evaluate(parse(hi_text))
    steps, F, v = antiderivative(expr_text)
    steps.append("Antiderivative F(%s) = %s" % (v, to_str(F)))
    fb, fa = evaluate(F, {v: hi}), evaluate(F, {v: lo})
    steps.append("Evaluate at the limits: F(%s) = %s and F(%s) = %s" % (show_value(hi).split("  ")[0], show_value(fb), show_value(lo).split("  ")[0], show_value(fa)))
    val = fb - fa
    steps.append("Subtract: F(%s) - F(%s) = %s" % (show_value(hi).split("  ")[0], show_value(lo).split("  ")[0], show_value(val)))
    return steps, "integral from %s to %s = %s" % (show_value(lo).split("  ")[0], show_value(hi).split("  ")[0], show_value(val))


# ------------------------------------------------------------------ statistics
def numbers_in(text):
    return [Fraction(x) for x in re.findall(r"-?\d*\.?\d+", text.replace(",", " "))]


def statistics(text, kind):
    xs = numbers_in(text.split(" of ", 1)[1] if " of " in text else text)
    if len(xs) < 2:
        raise SolveError("Give me at least two numbers, for example: mean of 4, 8, 15")
    if len(xs) > 200:
        raise SolveError("Please use up to 200 numbers.")
    n = len(xs)
    srt = sorted(xs)
    steps = ["Data (%d values): %s" % (n, ", ".join(fmt_num(x) for x in xs))]
    mean = sum(xs) / n
    if kind == "mean":
        steps += ["Add them: sum = %s" % fmt_num(sum(xs)), "Divide by the count: %s / %d = %s" % (fmt_num(sum(xs)), n, fmt_num(mean))]
        return steps, "mean = %s" % show_value(mean)
    if kind == "median":
        steps.append("Sorted: " + ", ".join(fmt_num(x) for x in srt))
        med = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
        steps.append("Middle value%s: %s" % ("" if n % 2 else "s averaged", fmt_num(med)))
        return steps, "median = %s" % show_value(med)
    if kind == "mode":
        counts = {}
        for x in xs:
            counts[x] = counts.get(x, 0) + 1
        top = max(counts.values())
        modes = [x for x in srt if counts[x] == top]
        steps.append("Most frequent value appears %d time%s." % (top, "" if top == 1 else "s"))
        return steps, ("no mode (all values appear once)" if top == 1 else "mode = " + ", ".join(fmt_num(m) for m in dict.fromkeys(modes)))
    if kind == "range":
        return steps + ["Largest - smallest = %s - %s" % (fmt_num(srt[-1]), fmt_num(srt[0]))], "range = %s" % fmt_num(srt[-1] - srt[0])
    ss = sum((x - mean) ** 2 for x in xs)
    pop, samp = math.sqrt(ss / n), math.sqrt(ss / (n - 1))
    steps += ["Mean = %s" % show_value(mean), "Sum of squared deviations = %s" % fmt_num(ss),
              "Population variance = SS/n = %s" % trim(float(ss / n)), "Sample variance = SS/(n-1) = %s" % trim(float(ss / (n - 1)))]
    return steps, "standard deviation: population = %s, sample = %s" % (trim(pop), trim(samp))


# ------------------------------------------------------------------ entry point
def solve(text):
    """Return {"category", "steps": [...], "answer"} or raise SolveError."""
    raw = normalise(text or "")
    if not raw:
        raise SolveError("Type or scan a problem first.")
    if len(raw) > MAX_LEN:
        raise SolveError("That problem is too long. Try a shorter one.")
    low = raw.lower()
    m = re.match(r"^(?:find |calculate |compute )?(?:the )?(mean|average|median|mode|range|standard deviation|std ?dev)\b", low)
    if m:
        kind = {"average": "mean", "std dev": "sd", "stddev": "sd", "standard deviation": "sd"}.get(m.group(1), m.group(1))
        steps, ans = statistics(raw, kind)
        return {"category": "Statistics", "steps": steps, "answer": ans}
    m = re.match(r"^(?:(?:find|calculate|compute|please)\s+)*(?:the\s+)?(?:differentiate|derivative of|derivative|diff|d/dx(?: of)?)\s*(.*)$", raw, re.I)
    if m and m.group(1).strip():
        steps, ans = derivative(strip_fx(m.group(1)))
        return {"category": "Calculus", "steps": steps, "answer": ans}
    m = re.match(r"^(?:(?:find|calculate|compute|evaluate|please)\s+)*(?:the\s+)?(?:integrate|integral of|integral|antiderivative of|∫)\s*(.*)$", raw, re.I)
    if m and m.group(1).strip():
        body = re.sub(r"\s*d[a-z]\s*$", "", m.group(1)) if re.search(r"[\s)]d[a-z]\s*$", m.group(1)) else m.group(1)
        d = re.search(r"\s*(?:from|between)\s+(.+?)\s+(?:to|and)\s+(.+?)\s*$", body, re.I)
        if d:
            body = body[:d.start()]
            steps, ans = definite_integral(strip_fx(re.sub(r"\s*d[a-z]\s*$", "", body) if re.search(r"[\s)]d[a-z]\s*$", body) else body), d.group(1), d.group(2))
        else:
            steps, ans = integral(strip_fx(body))
        return {"category": "Calculus", "steps": steps, "answer": ans}
    body = re.sub(r"^(?:(?:solve|find|calculate|compute|evaluate|simplify|expand|what is|whats|what's)\b[: ]*)+", "", raw, flags=re.I).strip(" ?.")
    low2 = body.lower()
    if not body:
        raise SolveError("Type or scan a problem first.")
    if low2.count("=") >= 2 and (";" in low2 or "," in low2 or "\n" in raw):
        steps, ans = solve_system(body)
        return {"category": "Algebra", "steps": steps, "answer": ans}
    if "=" in low2:
        steps, ans = solve_equation(body)
        return {"category": "Algebra", "steps": steps, "answer": ans}
    expr = parse(body)
    vs = variables(expr)
    steps = ["Expression: %s" % to_str(expr)]
    if not vs:
        val = evaluate(expr)
        steps.append("Work it out following the order of operations (brackets, powers, multiply/divide, add/subtract).")
        return {"category": "Arithmetic", "steps": steps, "answer": show_value(val)}
    res = clean(expr)
    steps.append("Expand and collect like terms." if len(vs) == 1 else "Simplify using the rules of algebra.")
    return {"category": "Algebra", "steps": steps, "answer": to_str(res)}


def strip_fx(s):
    s = re.sub(r"^\s*(?:of\s+)?(?:f\s*\(\s*[a-z]\s*\)|y)\s*=\s*", "", s.strip(), flags=re.I)
    return re.sub(r"^\s*(?:of\s+)", "", s, flags=re.I).strip()
