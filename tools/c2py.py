#!/usr/bin/env python3
"""Translate the f2c-style C source of Dungeon (the mainframe Zork) into Python.

This is a build-time tool: it reads the original C source and writes
zork/engine_gen.py. It handles only the C subset that source uses:

  * K&R function definitions, int/logical/char scalars and 1-D arrays;
  * if / for / while / do / switch / break / continue / return / goto;
  * the "pointer to an integer" idioms (&var, &struct.field, &array[i]).

gotos are handled exactly: a function that contains goto or labels is lowered
to basic blocks run by a small dispatch loop, so control flow is preserved.

Usage:  python tools/c2py.py SRC_DIR OUT_FILE
The C sources are not part of this repository (see NOTICE); point SRC_DIR at a
copy of them. Needs a C preprocessor (uses `python -m ziglang cc -E`).
"""

import os
import re
import subprocess
import sys

KEYWORDS = {"g", "in", "is", "not", "or", "and", "del", "from", "pass", "lambda", "global",
            "def", "class", "yield", "with", "as", "try", "except", "raise", "None",
            "True", "False", "print", "exec", "assert", "async", "await", "nonlocal",
            "elif", "import", "finally", "while", "for", "if", "else", "return",
            "break", "continue"}
TYPE_WORDS = {"register", "integer", "logical", "int", "long", "char", "void", "unsigned", "short",
              "const", "static", "extern", "struct", "FILE", "float", "double"}


def pyname(n):
    return n + "_k" if n in KEYWORDS else n


# ---------------------------------------------------------------- lexer
TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>0[xX][0-9a-fA-F]+|\d+\.\d*|\d+)[uUlL]*
  | (?P<id>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<str>"(?:\\.|[^"\\])*")
  | (?P<chr>'(?:\\.|[^'\\])+')
  | (?P<op><<=|>>=|\.\.\.|->|\+\+|--|<<|>>|<=|>=|==|!=|&&|\|\||\+=|-=|\*=|/=|%=|&=|\|=|\^=|[-+*/%&|^~!<>=?:;,.(){}\[\]])
""", re.X)


def lex(text):
    toks, pos = [], 0
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m:
            raise SyntaxError("lex error at %r" % text[pos:pos + 40])
        pos = m.end()
        k = m.lastgroup
        if k == "ws":
            continue
        toks.append((k, m.group(k) if k != "num" else m.group("num")))
    return toks


def unescape(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 1
            c = s[i]
            if c in "01234567":
                j = i
                while j < len(s) and j < i + 3 and s[j] in "01234567":
                    j += 1
                out.append(chr(int(s[i:j], 8)))
                i = j
                continue
            out.append({"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b",
                        "f": "\f", "v": "\v"}.get(c, c))
        else:
            out.append(c)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------- parser
class Parser:
    def __init__(self, toks):
        self.t = toks
        self.i = 0

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else ("eof", "")

    def val(self, k=0):
        return self.peek(k)[1]

    def next(self):
        tok = self.t[self.i]
        self.i += 1
        return tok

    def at(self, v, k=0):
        t = self.peek(k)
        return t[1] == v and t[0] in ("op", "id")

    def accept(self, v):
        if self.at(v):
            self.i += 1
            return True
        return False

    def expect(self, v):
        if not self.accept(v):
            raise SyntaxError("expected %r near %s" % (v, self.context()))
        return v

    def context(self):
        return " ".join(str(t[1]) for t in self.t[max(0, self.i - 8):self.i + 8])

    # ---- types
    def is_type_start(self, k=0):
        t = self.peek(k)
        return t[0] == "id" and t[1] in TYPE_WORDS

    def parse_type(self):
        """Consume type specifiers (and a struct body if present). Returns dict."""
        info = {"const": False, "words": [], "struct": None}
        while self.is_type_start() or (info["words"] and self.peek()[0] == "id"
                                       and self.peek(1)[0] == "id"):
            w = self.next()[1]
            if w == "const":
                info["const"] = True
            elif w == "struct":
                if self.peek()[0] == "id":
                    self.next()
                if self.at("{"):
                    info["struct"] = self.parse_struct_body()
                info["words"].append("struct")
            else:
                info["words"].append(w)
        return info

    def parse_struct_body(self):
        self.expect("{")
        members = []
        while not self.at("}"):
            ty = self.parse_type()
            while True:
                name, dims, ptr = self.parse_declarator()
                init = None
                if self.accept("="):
                    init = self.parse_initializer()
                members.append({"name": name, "dims": dims, "type": ty,
                                "const": ty["const"], "init": init})
                if not self.accept(","):
                    break
            self.expect(";")
        self.expect("}")
        return members

    def parse_declarator(self):
        ptr = 0
        while self.accept("*"):
            ptr += 1
            while self.at("const"):
                self.next()
        name = self.next()[1]
        dims = []
        while self.accept("["):
            if self.at("]"):
                dims.append(None)
            else:
                dims.append(self.parse_assign())
            self.expect("]")
        return name, dims, ptr

    def parse_initializer(self):
        if self.at("{"):
            self.next()
            items = []
            while not self.at("}"):
                items.append(self.parse_initializer())
                if not self.accept(","):
                    break
            self.expect("}")
            return ("list", items)
        return self.parse_assign()

    # ---- expressions (precedence climbing)
    BINPREC = [["||"], ["&&"], ["|"], ["^"], ["&"], ["==", "!="],
               ["<", ">", "<=", ">="], ["<<", ">>"], ["+", "-"], ["*", "/", "%"]]
    ASSIGN = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}

    def parse_expr(self):
        e = self.parse_assign()
        while self.at(","):
            self.next()
            e = ("comma", e, self.parse_assign())
        return e

    def parse_assign(self):
        lhs = self.parse_cond()
        t = self.peek()
        if t[0] == "op" and t[1] in self.ASSIGN:
            self.next()
            return ("asg", t[1], lhs, self.parse_assign())
        return lhs

    def parse_cond(self):
        c = self.parse_bin(0)
        if self.accept("?"):
            a = self.parse_expr()
            self.expect(":")
            b = self.parse_cond()
            return ("cond", c, a, b)
        return c

    def parse_bin(self, level):
        if level == len(self.BINPREC):
            return self.parse_unary()
        e = self.parse_bin(level + 1)
        while self.peek()[0] == "op" and self.peek()[1] in self.BINPREC[level]:
            op = self.next()[1]
            e = ("bin", op, e, self.parse_bin(level + 1))
        return e

    def parse_unary(self):
        t = self.peek()
        if t[0] == "op":
            if t[1] in ("++", "--"):
                self.next()
                return ("pre", t[1], self.parse_unary())
            if t[1] in ("!", "~", "-", "+"):
                self.next()
                return ("un", t[1], self.parse_unary())
            if t[1] == "*":
                self.next()
                return ("deref", self.parse_unary())
            if t[1] == "&":
                self.next()
                return ("addr", self.parse_unary())
            if t[1] == "(" and self.is_type_start(1):
                self.next()
                ty = self.parse_type()
                ptr = 0
                while self.accept("*"):
                    ptr += 1
                self.expect(")")
                return ("cast", (ty["words"], ptr), self.parse_unary())
        if t == ("id", "sizeof"):
            # Only used in functions that are written by hand; value is irrelevant.
            self.next()
            if self.at("(") and self.is_type_start(1):
                self.next()
                self.parse_type()
                while self.accept("*"):
                    pass
                self.expect(")")
            else:
                self.parse_unary()
            return ("num", 0)
        return self.parse_postfix()

    def parse_postfix(self):
        e = self.parse_primary()
        while True:
            t = self.peek()
            if t[0] != "op":
                break
            if t[1] == "[":
                self.next()
                i = self.parse_expr()
                self.expect("]")
                e = ("idx", e, i)
            elif t[1] == "(":
                self.next()
                args = []
                while not self.at(")"):
                    args.append(self.parse_assign())
                    if not self.accept(","):
                        break
                self.expect(")")
                e = ("call", e, args)
            elif t[1] in (".", "->"):
                self.next()
                e = ("dot", e, self.next()[1])
            elif t[1] in ("++", "--"):
                self.next()
                e = ("post", t[1], e)
            else:
                break
        return e

    def parse_primary(self):
        k, v = self.next()
        if k == "num":
            return ("num", int(v, 16) if v.lower().startswith("0x") else
                    (int(v, 8) if re.fullmatch(r"0[0-7]+", v) else
                     (float(v) if "." in v else int(v))))
        if k == "chr":
            return ("num", ord(unescape(v[1:-1])[0]))
        if k == "str":
            s = unescape(v[1:-1])
            while self.peek()[0] == "str":          # adjacent literals concatenate
                s += unescape(self.next()[1][1:-1])
            return ("str", s)
        if k == "id":
            return ("name", v)
        if v == "(":
            e = self.parse_expr()
            self.expect(")")
            return ("paren", e)
        raise SyntaxError("bad primary %r near %s" % (v, self.context()))

    # ---- statements
    def parse_block(self):
        self.expect("{")
        items = []
        while not self.at("}"):
            items.append(self.parse_stmt())
        self.expect("}")
        return ("block", items)

    def parse_stmt(self):
        t = self.peek()
        if t[0] == "op" and t[1] == "{":
            return self.parse_block()
        if t == ("op", ";"):
            self.next()
            return ("empty",)
        if t[0] == "id":
            w = t[1]
            if self.at(":", 1) and w not in ("default",) and not self.at("?", 0):
                self.next()
                self.next()
                return ("label", w)
            if w == "if":
                self.next()
                self.expect("(")
                c = self.parse_expr()
                self.expect(")")
                a = self.parse_stmt()
                b = self.parse_stmt() if self.accept("else") else None
                return ("if", c, a, b)
            if w == "while":
                self.next()
                self.expect("(")
                c = self.parse_expr()
                self.expect(")")
                return ("while", c, self.parse_stmt())
            if w == "do":
                self.next()
                body = self.parse_stmt()
                self.expect("while")
                self.expect("(")
                c = self.parse_expr()
                self.expect(")")
                self.expect(";")
                return ("do", body, c)
            if w == "for":
                self.next()
                self.expect("(")
                init = None if self.at(";") else self.parse_expr()
                self.expect(";")
                cond = None if self.at(";") else self.parse_expr()
                self.expect(";")
                step = None if self.at(")") else self.parse_expr()
                self.expect(")")
                return ("for", init, cond, step, self.parse_stmt())
            if w == "switch":
                self.next()
                self.expect("(")
                c = self.parse_expr()
                self.expect(")")
                return ("switch", c, self.parse_stmt())
            if w == "case":
                self.next()
                e = self.parse_cond()
                self.expect(":")
                return ("case", e)
            if w == "default":
                self.next()
                self.expect(":")
                return ("default",)
            if w == "break":
                self.next()
                self.expect(";")
                return ("break",)
            if w == "continue":
                self.next()
                self.expect(";")
                return ("continue",)
            if w == "return":
                self.next()
                e = None if self.at(";") else self.parse_expr()
                self.expect(";")
                return ("return", e)
            if w == "goto":
                self.next()
                name = self.next()[1]
                self.expect(";")
                return ("goto", name)
            if w in TYPE_WORDS or self.peek(1)[0] == "id":
                return self.parse_local_decl()
        e = self.parse_expr()
        self.expect(";")
        return ("expr", e)

    def parse_local_decl(self):
        if self.peek()[0] == "id" and self.peek()[1] not in TYPE_WORDS:
            self.next()                      # a typedef name such as time_t
        ty = self.parse_type()
        decls = []
        while True:
            name, dims, ptr = self.parse_declarator()
            init = None
            if self.accept("="):
                init = self.parse_initializer()
            decls.append({"name": name, "dims": dims, "ptr": ptr, "type": ty, "init": init})
            if not self.accept(","):
                break
        self.expect(";")
        return ("decl", ty, decls)

    # ---- top level
    def parse_top(self):
        """Returns list of ('func', ...), ('data', ...) items."""
        items = []
        while self.peek()[0] != "eof":
            if self.accept(";"):
                continue
            if self.at("typedef"):
                while not self.accept(";"):
                    self.next()
                continue
            start = self.i
            ty = self.parse_type()
            if self.at(";"):
                self.next()
                continue
            name, dims, ptr = self.parse_declarator()
            if self.at("("):                    # function (or prototype)
                self.next()
                params = []
                while not self.at(")"):
                    params.append(self.next())
                self.expect(")")
                if self.accept(";") or self.at(",") and self._skip_decl():
                    continue
                pnames = [p[1] for p in params if p[0] == "id" and p[1] not in TYPE_WORDS
                          and p[1] != "void"]
                ptypes = {}
                while not self.at("{"):         # K&R parameter declarations
                    pty = self.parse_type()
                    while True:
                        pn, pd, pp = self.parse_declarator()
                        ptypes[pn] = {"ptr": pp, "dims": pd, "type": pty}
                        if not self.accept(","):
                            break
                    self.expect(";")
                body = self.parse_block()
                items.append(("func", name, pnames, ptypes, body, ty))
            else:                                # data declaration
                self.i = start
                self.parse_type()
                while True:
                    name, dims, ptr = self.parse_declarator()
                    init = self.parse_initializer() if self.accept("=") else None
                    items.append(("data", name, dims, ty, init))
                    if not self.accept(","):
                        break
                self.expect(";")
        return items

    def _skip_decl(self):
        while not self.accept(";"):
            self.next()
        return True


# ---------------------------------------------------------------- generator
class Gen:
    def __init__(self, globals_info, func_names, hand_funcs, lib):
        self.glob = globals_info          # name -> 'state' | 'const'
        self.funcs = func_names
        self.hand = hand_funcs
        self.lib = lib
        self.lines = []
        self.label_ids = {}
        self.cells = set()
        self.locals = set()
        self.tmp = 0
        self.fname = ""

    # ---- expression emission
    def ex(self, e, cond=False):
        k = e[0]
        if k == "num":
            return repr(e[1])
        if k == "str":
            return repr(e[1])
        if k == "paren":
            return "(" + self.ex(e[1], cond) + ")"
        if k == "name":
            return self.name(e[1])
        if k == "dot":
            return self.ex(e[1]) + "." + pyname(e[2])
        if k == "idx":
            return self.ex(e[1]) + "[" + self.ex(e[2]) + "]"
        if k == "un":
            op = e[1]
            if op == "!":
                return "(not %s)" % self.ex(e[2], True)
            return "(%s%s)" % (op, self.ex(e[2]))
        if k == "bin":
            return self.binop(e, cond)
        if k == "cond":
            return "(%s if %s else %s)" % (self.ex(e[2]), self.ex(e[1], True), self.ex(e[3]))
        if k == "call":
            return self.call(e)
        if k == "cast":
            ptr = e[1][1]
            if ptr and e[2][0] == "addr":
                inner = e[2][1]
                return "flat(%s)" % self.ex(inner)
            if ptr and e[2][0] == "paren":
                return self.ex(e[2])
            return self.ex(e[2])
        if k == "addr":
            return self.addr(e[1])
        if k == "deref":
            return self.ex(e[1]) + ".v"
        if k in ("asg", "pre", "post", "comma"):
            return self.expr_with_effect(e)
        raise ValueError("cannot emit %r in %s" % (e, self.fname))

    def cptr(self, e):
        """Expression for a value stored in / passed as a pointer: string
        literals become C-style char buffers."""
        if e[0] == "str":
            return "cstr(%r)" % e[1]
        return self.ex(e)

    def name(self, n):
        if n in self.cells:
            return pyname(n) + ".v"
        if n in self.locals:
            return pyname(n)
        if n == "TRUE_" or n == "FALSE_":
            return "1" if n == "TRUE_" else "0"
        g = self.glob.get(n)
        if g == "state":
            return "g." + pyname(n)
        if g == "const":
            return pyname(n)
        if n in self.funcs or n in self.lib:
            return pyname(n)
        if n in ("NULL", "stdin", "stdout"):
            return "None"
        return pyname(n)

    def binop(self, e, cond):
        op, a, b = e[1], e[2], e[3]
        if op == "&&":
            s = "(%s and %s)" % (self.ex(a, True), self.ex(b, True))
            return s if cond else "bool" + s
        if op == "||":
            s = "(%s or %s)" % (self.ex(a, True), self.ex(b, True))
            return s if cond else "bool" + s
        if op == "/":
            return "cdiv(%s, %s)" % (self.ex(a), self.ex(b))
        if op == "%":
            return "cmod(%s, %s)" % (self.ex(a), self.ex(b))
        return "(%s %s %s)" % (self.ex(a), op, self.ex(b))

    def addr(self, x):
        k = x[0]
        if k == "name":
            if x[1] in self.cells:
                return pyname(x[1])
            raise ValueError("address of non-cell %s in %s" % (x[1], self.fname))
        if k == "dot":
            return "AttrRef(%s, %r)" % (self.ex(x[1]), pyname(x[2]))
        if k == "idx":
            return "ItemRef(%s, %s)" % (self.ex(x[1]), self.ex(x[2]))
        if k == "paren":
            return self.addr(x[1])
        raise ValueError("cannot take address of %r in %s" % (x, self.fname))

    def call(self, e):
        f = e[1]
        if f[0] != "name":
            raise ValueError("indirect call in %s" % self.fname)
        n = f[1]
        if n in self.lib:
            args = ", ".join(self.ex(a) for a in e[2])
        else:
            args = ", ".join(self.cptr(a) for a in e[2])
        if n in self.funcs or n in self.hand:
            return "%s(g%s)" % (pyname(n), ", " + args if args else "")
        if n in self.lib:
            return "%s(g%s)" % (self.lib[n], ", " + args if args else "")
        raise ValueError("unknown function %s in %s" % (n, self.fname))

    def expr_with_effect(self, e):
        k = e[0]
        if k == "comma":
            return "(%s, %s)[1]" % (self.ex(e[1]), self.ex(e[2]))
        if k == "asg" and e[1] == "=" and e[2][0] == "name" and e[2][1] in self.locals                 and e[2][1] not in self.cells:
            return "(%s := %s)" % (pyname(e[2][1]), self.ex(e[3]))
        if k in ("pre", "post") and e[2][0] == "name" and e[2][1] in self.locals                 and e[2][1] not in self.cells:
            n = pyname(e[2][1])
            d = "+" if e[1] == "++" else "-"
            u = "-" if e[1] == "++" else "+"
            if k == "pre":
                return "(%s := %s %s 1)" % (n, n, d)
            return "((%s := %s %s 1) %s 1)" % (n, n, d, u)
        raise ValueError("assignment/increment inside an expression in %s: %r"
                         % (self.fname, e))

    # ---- statements that are expressions
    def expr_stmt(self, e, out, ind):
        k = e[0]
        if k == "paren":
            return self.expr_stmt(e[1], out, ind)
        if k == "asg":
            op, lhs, rhs = e[1], e[2], e[3]
            if rhs[0] == "asg":                        # a = b = c
                self.expr_stmt(rhs, out, ind)
                rhs = rhs[2]
            l = self.ex(lhs)
            r = self.cptr(rhs)
            if op == "=":
                out.append(ind + "%s = %s" % (l, r))
            elif op == "/=":
                out.append(ind + "%s = cdiv(%s, %s)" % (l, l, r))
            elif op == "%=":
                out.append(ind + "%s = cmod(%s, %s)" % (l, l, r))
            else:
                out.append(ind + "%s %s %s" % (l, op, r))
            return
        if k in ("pre", "post"):
            tgt = self.ex(e[2])
            out.append(ind + "%s %s= 1" % (tgt, "+" if e[1] == "++" else "-"))
            return
        if k == "comma":
            self.expr_stmt(e[1], out, ind)
            self.expr_stmt(e[2], out, ind)
            return
        if k == "cast":
            return self.expr_stmt(e[2], out, ind)
        if k == "call":
            out.append(ind + self.ex(e))
            return
        out.append(ind + self.ex(e))

    # ---- structure analysis
    @staticmethod
    def has_goto(s):
        if s is None:
            return False
        if isinstance(s, tuple):
            if s and s[0] in ("goto", "label"):
                return True
            return any(Gen.has_goto(x) for x in s[1:])
        if isinstance(s, list):
            return any(Gen.has_goto(x) for x in s)
        return False

    @staticmethod
    def has_kind(s, kind):
        if isinstance(s, tuple):
            if s and s[0] == kind:
                return True
            return any(Gen.has_kind(x, kind) for x in s[1:])
        if isinstance(s, list):
            return any(Gen.has_kind(x, kind) for x in s)
        return False

    # ---- function emission
    def function(self, name, pnames, ptypes, body, ty):
        self.fname = name
        self.locals = set(pnames)
        self.cells = set()
        # find locals and address-taken scalars
        decls = []

        def scan(s):
            if isinstance(s, tuple):
                if s and s[0] == "decl":
                    decls.append(s)
                elif s and s[0] == "addr" and s[1][0] == "name":
                    self.cells.add(s[1][1])
                for x in s[1:]:
                    scan(x)
            elif isinstance(s, list):
                for x in s:
                    scan(x)
            elif isinstance(s, dict):
                pass
        scan(body)
        # cells may be locals only
        local_names = set(pnames)
        for d in decls:
            for dd in d[2]:
                local_names.add(dd["name"])
        self.cells &= local_names
        self.locals = local_names
        out = []
        params = ["g"] + [pyname(p) for p in pnames]
        out.append("def %s(%s):" % (pyname(name), ", ".join(params)))
        ind = "    "
        # local declarations
        for d in decls:
            for dd in d[2]:
                n = dd["name"]
                if n in pnames:
                    continue
                if dd["dims"]:
                    size = self.const_dim(dd["dims"][0])
                    if dd["init"] is not None:
                        out.append(ind + "%s = CBuf(%s)" % (pyname(n), self.init_list(dd["init"])))
                    else:
                        out.append(ind + "%s = CBuf([0] * %s)" % (pyname(n), size))
                elif n in self.cells:
                    out.append(ind + "%s = Cell(%s)" % (
                        pyname(n), self.ex(dd["init"]) if dd["init"] else "0"))
                else:
                    ini = self.cptr(dd["init"]) if dd["init"] is not None else "0"
                    if dd["type"]["words"] and "char" in dd["type"]["words"] and dd["ptr"]:
                        ini = self.cptr(dd["init"]) if dd["init"] is not None else "None"
                    out.append(ind + "%s = %s" % (pyname(n), ini))
        for p in pnames:                                  # params whose address is taken
            if p in self.cells:
                out.append(ind + "%s = Cell(%s)" % (pyname(p), pyname(p)))
        stmts = body[1]
        if not self.has_goto(stmts) and not self.has_kind(stmts, "switch"):
            self.native(stmts, out, ind, None)
        else:
            self.blocks(stmts, out, ind)
        if len(out) == 1:
            out.append(ind + "pass")
        return out

    def const_dim(self, d):
        if d[0] == "num":
            return str(d[1])
        return self.ex(d)

    def init_list(self, i):
        if i[0] == "list":
            return "[" + ", ".join(self.init_list(x) for x in i[1]) + "]"
        return self.cptr(i)

    # ---- native (structured) emission
    def native(self, stmts, out, ind, loopctx):
        n0 = len(out)
        for s in stmts:
            self.stmt(s, out, ind, loopctx)
        if len(out) == n0:
            out.append(ind + "pass")

    def stmt(self, s, out, ind, loopctx):
        k = s[0]
        if k == "block":
            self.native(s[1], out, ind, loopctx) if s[1] else out.append(ind + "pass")
        elif k == "empty":
            pass
        elif k == "decl":
            for dd in s[2]:                           # initialised locals
                if dd["init"] is not None and not dd["dims"]:
                    self.expr_stmt(("asg", "=", ("name", dd["name"]), dd["init"]), out, ind)
        elif k == "expr":
            self.expr_stmt(s[1], out, ind)
        elif k == "if":
            out.append(ind + "if %s:" % self.ex(s[1], True))
            self.sub(s[2], out, ind + "    ", loopctx)
            if s[3] is not None:
                if s[3][0] == "if":
                    # else-if chain
                    out.append(ind + "else:")
                    self.sub(s[3], out, ind + "    ", loopctx)
                else:
                    out.append(ind + "else:")
                    self.sub(s[3], out, ind + "    ", loopctx)
        elif k == "while":
            out.append(ind + "while %s:" % self.ex(s[1], True))
            self.sub(s[2], out, ind + "    ", "loop")
        elif k == "do":
            out.append(ind + "while True:")
            self.sub(s[1], out, ind + "    ", "loop")
            out.append(ind + "    if not %s:" % self.ex(s[2], True))
            out.append(ind + "        break")
        elif k == "for":
            self.for_native(s, out, ind)
        elif k == "switch":
            self.switch_native(s, out, ind, loopctx)
        elif k == "break":
            if isinstance(loopctx, tuple):
                out.append(ind + "_pc = L:%s" % loopctx[1])
                out.append(ind + "continue")
            else:
                out.append(ind + "break")
        elif k == "continue":
            if isinstance(loopctx, tuple):
                out.append(ind + "_pc = L:%s" % loopctx[2])
                out.append(ind + "continue")
            else:
                out.append(ind + "continue")
        elif k == "return":
            out.append(ind + ("return %s" % self.ex(s[1]) if s[1] is not None else "return"))
        elif k == "goto":
            out.append(ind + "_pc = %s" % self.label_id(s[1]))
            out.append(ind + "continue")
        elif k in ("label", "case", "default"):
            raise ValueError("label/case in native context in %s" % self.fname)
        else:
            raise ValueError("unknown statement %r" % (k,))

    def sub(self, s, out, ind, loopctx):
        n0 = len(out)
        if s[0] == "block":
            for x in s[1]:
                self.stmt(x, out, ind, loopctx)
        else:
            self.stmt(s, out, ind, loopctx)
        if len(out) == n0:
            out.append(ind + "pass")

    def for_native(self, s, out, ind):
        _, init, cond, step, body = s
        if init is not None:
            self.expr_stmt(init, out, ind)
        if step is not None and self.has_continue(body):
            raise ValueError("continue inside native for loop in %s" % self.fname)
        out.append(ind + "while %s:" % (self.ex(cond, True) if cond else "True"))
        self.sub(body, out, ind + "    ", "loop")
        if step is not None:
            self.expr_stmt(step, out, ind + "    ")

    @staticmethod
    def has_continue(s):
        if isinstance(s, tuple):
            if s and s[0] == "continue":
                return True
            if s and s[0] in ("for", "while", "do"):
                return False
            return any(Gen.has_continue(x) for x in s[1:])
        if isinstance(s, list):
            return any(Gen.has_continue(x) for x in s)
        return False

    def switch_native(self, s, out, ind, loopctx):
        """A switch used purely as a computed goto: case N: goto L; ..."""
        _, expr, body = s
        items = body[1] if body[0] == "block" else [body]
        tmpv = "_swn%d" % len(out)
        out.append(ind + "%s = %s" % (tmpv, self.ex(expr)))
        i = 0
        while i < len(items):
            it = items[i]
            if it[0] == "case" and i + 1 < len(items) and items[i + 1][0] == "goto":
                out.append(ind + "if %s == %s:" % (tmpv, self.ex(it[1])))
                out.append(ind + "    _pc = %s" % self.label_id(items[i + 1][1]))
                out.append(ind + "    continue")
                i += 2
            else:
                raise ValueError("complex nested switch in %s" % self.fname)

    # ---- block (goto) emission
    def label_id(self, name):
        if name not in self.label_ids:
            self.label_ids[name] = None
        return ("L:" + name)

    def blocks(self, stmts, out, ind):
        """Lower to basic blocks. Block ids are assigned in textual order."""
        self.nblocks = 0
        blocks = []        # list of (id, lines)
        self.label_map = {}
        cur = [self.new_block(blocks)]

        def fresh():
            cur[0] = self.new_block(blocks)
            return cur[0]

        def emit(line):
            blocks[cur[0]][1].append(line)

        def goto(bid):
            emit("_pc = %s" % bid)
            emit("continue")

        counter = [0]

        def newlabel(prefix):
            counter[0] += 1
            return "%s%d" % (prefix, counter[0])

        def label_block(name):
            """Start a new block bound to symbolic label `name`."""
            b = fresh()
            self.label_map[name] = b
            return b

        def lower(ss, brk, cont):
            for s in ss:
                k = s[0]
                if k == "block":
                    lower(s[1], brk, cont)
                elif k == "label":
                    label_block("L:" + s[1])
                elif k in ("for", "while", "do") and self.has_goto(s):
                    lower_loop(s, brk, cont)
                elif k == "switch":
                    lower_switch(s, brk, cont)
                elif k == "break":
                    if brk is None:
                        raise ValueError("break outside loop/switch in " + self.fname)
                    emit("_pc = %s" % ("L:" + brk))
                    emit("continue")
                elif k == "continue":
                    emit("_pc = %s" % ("L:" + cont))
                    emit("continue")
                elif k in ("case", "default"):
                    raise ValueError("stray case in " + self.fname)
                else:
                    tmp = []
                    self.stmt(s, tmp, "", ("blk", brk, cont) if brk else None)
                    blocks[cur[0]][1].extend(tmp)

        def lower_loop(s, brk, cont):
            k = s[0]
            top = newlabel("top")
            step = newlabel("step")
            end = newlabel("end")
            if k == "for":
                _, init, cond, stp, body = s
                if init is not None:
                    tmp = []
                    self.expr_stmt(init, tmp, "")
                    blocks[cur[0]][1].extend(tmp)
                label_block("L:" + top)
                if cond is not None:
                    emit("if not %s:" % self.ex(cond, True))
                    emit("    _pc = L:%s" % end)
                    emit("    continue")
                lower([body], end, step)
                label_block("L:" + step)
                if stp is not None:
                    tmp = []
                    self.expr_stmt(stp, tmp, "")
                    blocks[cur[0]][1].extend(tmp)
                goto("L:" + top)
            elif k == "while":
                label_block("L:" + top)
                emit("if not %s:" % self.ex(s[1], True))
                emit("    _pc = L:%s" % end)
                emit("    continue")
                lower([s[2]], end, top)
                goto("L:" + top)
            else:                                    # do-while
                label_block("L:" + top)
                lower([s[1]], end, step)
                label_block("L:" + step)
                emit("if %s:" % self.ex(s[2], True))
                emit("    _pc = L:%s" % top)
                emit("    continue")
            label_block("L:" + end)

        def lower_switch(s, brk, cont):
            _, expr, body = s
            end = newlabel("swend")
            tmpv = "_sw%d" % counter[0]
            counter[0] += 1
            emit("%s = %s" % (tmpv, self.ex(expr)))
            items = body[1] if body[0] == "block" else [body]
            case_labels = []
            default_label = None
            # dispatch
            for it in items:
                if it[0] == "case":
                    lab = newlabel("case")
                    case_labels.append((it, lab))
                    emit("if %s == %s:" % (tmpv, self.ex(it[1])))
                    emit("    _pc = L:%s" % lab)
                    emit("    continue")
                elif it[0] == "default":
                    default_label = newlabel("default")
            emit("_pc = L:%s" % (default_label or end))
            emit("continue")
            lab_iter = iter(case_labels)
            flat = []
            for it in items:
                if it[0] == "case":
                    flat.append(("label", next(lab_iter)[1]))
                elif it[0] == "default":
                    flat.append(("label", default_label))
                else:
                    flat.append(it)
            lower(flat, end, cont)
            label_block("L:" + end)

        # top-level label statements carry their C names
        lower(stmts, None, None)
        # emit
        out.append(ind + "_pc = 0")
        out.append(ind + "while True:")
        bi = ind + "    "
        body_lines = []
        for bid, lines in blocks:
            body_lines.append(bi + "if _pc <= %d:" % bid)
            if not lines:
                body_lines.append(bi + "    pass")
            for ln in lines:
                body_lines.append(bi + "    " + ln)
        body_lines.append(bi + "return")
        # resolve symbolic labels
        text = "\n".join(body_lines)

        def sub_label(m):
            name = "L:" + m.group(1)
            if name not in self.label_map:
                raise ValueError("undefined label %s in %s" % (name, self.fname))
            return str(self.label_map[name])
        text = re.sub(r"L:([A-Za-z0-9_]+)", sub_label, text)
        out.extend(text.split("\n"))

    def new_block(self, blocks):
        bid = len(blocks)
        blocks.append((bid, []))
        return bid


def preprocess(cc, src, flags):
    cmd = cc + ["-E", "-std=gnu89"] + flags + [src]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode:
        raise RuntimeError(res.stderr)
    keep, out = False, []
    srcdir = os.path.dirname(os.path.abspath(src))
    for ln in res.stdout.split("\n"):
        m = re.match(r'# \d+ "([^"]*)"', ln)
        if m:
            f = m.group(1)
            # Keep only the game's own files (relative names that exist in srcdir).
            f = re.sub(r"[\\/]+", "/", f)
            keep = (not f.startswith("<")
                    and os.path.dirname(f).lower() == srcdir.replace("\\", "/").lower())
            continue
        if keep and ln.strip():
            out.append(ln)
    return "\n".join(out)


# ---------------------------------------------------------------- driver
LOGIC_FILES = ["actors", "ballop", "clockr", "demons", "dgame", "dinit", "dso1", "dso2",
               "dso3", "dso4", "dso5", "dso6", "dso7", "dsub", "dverb1", "dverb2",
               "lightp", "nobjs", "np", "np1", "np2", "np3", "nrooms", "objcts",
               "rooms", "sobjs", "supp", "sverbs", "verbs", "villns"]
# Functions written by hand in zork/rt.py instead of being translated.
HAND = {"rdline_", "exit_", "more_output", "itime_", "rnd_", "yesno_", "gdt_", "savegame_",
        "restore_", "litint_", "rspsb2nl_", "rdints", "rdpartialints", "rdflags",
        "more_init", "more_input", "savegm_", "rstrgm_", "protected", "wizard"}
# C library calls -> helpers in zork/rt.py (they take the game `g` first).
LIB = {"printf": "c_printf", "fflush": "c_noop", "putchar": "c_putchar",
       "strlen": "c_strlen", "strcmp": "c_strcmp", "strcpy": "c_strcpy",
       "fopen": "c_fopen", "fclose": "c_noop", "getc": "c_getc", "ftell": "c_ftell",
       "fseek": "c_fseek", "isalpha": "c_isalpha", "toupper": "c_toupper",
       "islower": "c_islower", "isupper": "c_isupper", "tolower": "c_tolower",
       "isspace": "c_isspace", "isdigit": "c_isdigit", "memset": "c_memset",
       "c_open_text": "c_open_text", "c_rdint": "c_rdint", "c_resuming": "c_resuming"}


# (file, regex, replacement). The rdint macro assigns inside an expression; the
# replacement is a plain call implemented in zork/rt.py.
PATCHES = {
    "dinit.c": [
        (r"#define rdint\(indxfile\).*?getc\(indxfile\)\)",
         "#define rdint(indxfile) c_rdint(indxfile)"),
        (r"#ifdef __AMOS__\n    if \(\(dbfile = fdopen.*?goto L1950;",
         "    dbfile = c_open_text();\n    if (dbfile == NULL)\n\tgoto L1950;"),
    ],
    # When a saved game is resumed the welcome message and room are not repeated.
    "dgame.c": [
        (r"    rspeak_\(1\);", "    if (! c_resuming()) rspeak_(1);"),
        (r"    f = rmdesc_\(3\);", "    if (! c_resuming()) f = rmdesc_(3);"),
    ],
}


def patched_copy(src_dir):
    """Copy the sources to a temp dir, applying PATCHES (see zork/rt.py)."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="dungeon_src_")
    for f in os.listdir(src_dir):
        if f.endswith((".c", ".h")):
            shutil.copy(os.path.join(src_dir, f), tmp)
    for fname, pairs in PATCHES.items():
        path = os.path.join(tmp, fname)
        text = open(path, encoding="latin-1", newline="").read().replace("\r\n", "\n")
        for pattern, new in pairs:
            text, n = re.subn(pattern, lambda m, new=new: new, text, flags=re.S)
            if n != 1:
                raise RuntimeError("patch does not apply to %s: %r" % (fname, pattern[:40]))
        open(path, "w", encoding="latin-1", newline="").write(text)
    return tmp


def collect(src_dir, cc, flags):
    """Parse every file; return (data_items, funcs)."""
    data = None
    funcs = {}
    # dmain.c defines the shared data (vars.h); np1.c defines the parser tables (parse.h).
    for name in ["dmain", "np1"] + LOGIC_FILES:
        path = os.path.join(src_dir, name + ".c")
        defining = name in ("dmain", "np1") and not (name == "np1" and funcs)
        extra = ["-DEXTERN=", "-DINIT"] if name == "dmain" else ["-DEXTERN=extern"]
        text = preprocess(cc, path, flags + extra)
        text = text.replace("__extension__", "")
        try:
            items = Parser(lex(text)).parse_top()
        except SyntaxError as e:
            raise SyntaxError("%s.c: %s" % (name, e))
        if defining:
            seen = {i[1] for i in (data or [])}
            data = (data or []) + [i for i in items if i[0] == "data" and i[1] not in seen]
            if name == "dmain":
                continue
        if True:
            for it in items:
                if it[0] == "func":
                    funcs.setdefault(it[1], (name, it))
    return data, funcs


def emit_value(v, member):
    """Python source for an initialiser item for struct member `member`."""
    return None


def py_init(init, g):
    if init[0] == "list":
        return "[" + ", ".join(py_init(x, g) for x in init[1]) + "]"
    return g.ex(init)


def struct_class(name, members, g):
    lines = ["class %s_t(Struct):" % name]
    lines.append("    _fields = (%s)" % "".join("(%r, %s), " % (
        m["name"], (g.const_dim(m["dims"][0]) if m["dims"] else "None")) for m in members))
    lines.append("")
    lines.append("    def __init__(self):")
    for m in members:
        mn = pyname(m["name"])
        if m["dims"]:
            size = g.const_dim(m["dims"][0])
            if m["init"] is not None:
                lines.append("        self.%s = CBuf(%s)" % (mn, py_init(m["init"], g)))
            else:
                lines.append("        self.%s = CBuf([0] * %s)" % (mn, size))
        else:
            lines.append("        self.%s = %s" % (mn, py_init(m["init"], g) if m["init"] else "0"))
    return lines


def fill_struct_init(members, init):
    """Distribute a (possibly flat / brace-elided) initialiser over members."""
    if init is None:
        return
    items = list(init[1])
    pos = 0
    for m in members:
        if m["dims"]:
            size = m["dims"][0][1] if m["dims"][0] and m["dims"][0][0] == "num" else None
            if pos < len(items) and items[pos][0] == "list":
                vals = items[pos][1]
                pos += 1
            else:
                vals = items[pos:pos + size]
                pos += size
            if size is not None:
                vals = list(vals) + [("num", 0)] * (size - len(vals))
            m["init"] = ("list", vals)
        else:
            if pos < len(items):
                m["init"] = items[pos]
                pos += 1


def main():
    src_dir, out_path = sys.argv[1], sys.argv[2]
    cc = [sys.executable, "-m", "ziglang", "cc"]
    src_dir = patched_copy(src_dir)
    flags = ["-DMORE_NONE", '-DTEXTFILE="dtextc.dat"', "-w"]
    data, funcs = collect(src_dir, cc, flags)
    glob = {}
    for it in data:
        _, name, dims, ty, init = it
        const = ty["const"]
        glob[name] = "const" if const else "state"
    # structs defined with const qualifier on the struct itself vs members:
    glob["dbfile"] = "state"          # FILE *dbfile, defined in dinit.c
    g = Gen(glob, set(funcs) | HAND, HAND, LIB)
    g.locals = set()
    out = []
    header = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen_header.txt"),
                  encoding="utf-8").read()
    out.append(header)
    out.append("from .rt import *   # noqa: F401,F403\n\n")
    state_names = []
    for it in data:
        _, name, dims, ty, init = it
        if ty["struct"]:
            members = ty["struct"]
            fill_struct_init(members, init)
            out.extend(struct_class(name, members, g))
            out.append("")
            if glob[name] == "const":
                out.append("%s = %s_t()" % (pyname(name), name))
            else:
                state_names.append(name)
        else:
            if init is None:
                size = g.const_dim(dims[0]) if dims and dims[0] else "0"
                out.append("%s = CBuf([0] * %s)" % (pyname(name), size))
            else:
                out.append("%s = %s" % (pyname(name), ("CBuf(%s)" if dims else "%s") % py_init(init, g)))
    out.append("")
    out.append("class State:")
    out.append("    def __init__(self):")
    out.append("        self.dbfile = None")
    out.append("        self.io = None")
    for n in state_names:
        out.append("        self.%s = %s_t()" % (pyname(n), n))
    out.append("")
    out.append("STATE_NAMES = %r" % (tuple(state_names),))
    out.append("")
    # functions, grouped by source file
    by_file = {}
    for fname, (src, it) in funcs.items():
        by_file.setdefault(src, []).append(it)
    errors = []
    for src in LOGIC_FILES:
        its = by_file.get(src, [])
        if not its:
            continue
        out.append("")
        out.append("# " + "=" * 70)
        out.append("# from %s.c" % src)
        out.append("# " + "=" * 70)
        for it in its:
            _, name, pnames, ptypes, body, ty = it
            if name in HAND:
                continue
            g.label_ids = {}
            try:
                fl = g.function(name, pnames, ptypes, body, ty)
            except Exception as e:      # noqa: BLE001
                errors.append("%s.c %s: %s" % (src, name, e))
                continue
            out.append("")
            out.extend(fl)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")
    print("wrote %s (%d lines), %d functions, %d errors" % (
        out_path, len(out), len(funcs), len(errors)))
    for e in errors:
        print("  ERROR", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
