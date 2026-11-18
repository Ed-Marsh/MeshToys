"""Runtime support for the translated Dungeon engine (engine_gen.py).

Part of the Python port of Dungeon (the mainframe Zork); see NOTICE at the top of
the repository for the origin and original copyright notice.

Contains
  * small helpers the generated code uses to model C (integer division, pointers
    to variables, char buffers, struct-as-flat-array casts);
  * the C library calls the game needs, and
  * the functions that were written by hand rather than translated: terminal and
    file I/O, the random number generator, and the "legal moves" entry points.

Differences from the original C program, on purpose:
  * the game never runs shell commands (the C `rdline_` passed any line that
    starts with "!" to system());
  * SAVE / RESTORE are handled by the host program (the engine saves itself
    after every move), so the in-game verbs just say so;
  * the GDT debugging tool is not available.
"""

import queue
import time

# ----------------------------------------------------------------- C helpers


def cdiv(a, b):
    """C integer division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def cmod(a, b):
    return a - cdiv(a, b) * b


class Cell:
    """A local variable whose address is taken (&x)."""
    __slots__ = ("v",)

    def __init__(self, v=0):
        self.v = v


class AttrRef:
    """&struct.field"""
    __slots__ = ("obj", "name")

    def __init__(self, obj, name):
        self.obj, self.name = obj, name

    @property
    def v(self):
        return getattr(self.obj, self.name)

    @v.setter
    def v(self, value):
        setattr(self.obj, self.name, value)


class ItemRef:
    """&array[i]: also serves as a pointer that can be indexed and stepped."""
    __slots__ = ("lst", "idx")

    def __init__(self, lst, idx=0):
        self.lst, self.idx = lst, idx

    @property
    def v(self):
        return self.lst[self.idx]

    @v.setter
    def v(self, value):
        self.lst[self.idx] = value

    def __getitem__(self, i):
        return self.lst[self.idx + i]

    def __setitem__(self, i, value):
        self.lst[self.idx + i] = value

    def __add__(self, n):
        return ItemRef(self.lst, self.idx + n)

    def __sub__(self, other):
        if isinstance(other, ItemRef):
            return self.idx - other.idx
        return ItemRef(self.lst, self.idx - other)

    def __lt__(self, other):
        return self.idx < other.idx

    def __le__(self, other):
        return self.idx <= other.idx

    def __gt__(self, other):
        return self.idx > other.idx

    def __ge__(self, other):
        return self.idx >= other.idx

    def __eq__(self, other):
        return (isinstance(other, ItemRef) and self.lst is other.lst
                and self.idx == other.idx)

    def __hash__(self):
        return id(self.lst) + self.idx


class CBuf(list):
    """A char array (list of ints). `buf + n` is a pointer into it."""

    def __add__(self, n):
        return ItemRef(self, n)

    __iadd__ = __add__

    def __sub__(self, n):
        return ItemRef(self, -n)

    __isub__ = __sub__

    @property
    def v(self):
        return self[0]

    @v.setter
    def v(self, value):
        self[0] = value


def cstr(s):
    """A C string literal as a pointer to a NUL-terminated char buffer."""
    return ItemRef(CBuf([ord(c) for c in s] + [0]), 0)


def pystr(p):
    """Read a C string (pointer, buffer or Python str) as a Python str."""
    if p is None or isinstance(p, int):
        return ""
    if isinstance(p, str):
        return p
    lst, i = (p.lst, p.idx) if isinstance(p, ItemRef) else (p, 0)
    out = []
    while i < len(lst) and lst[i] != 0:
        out.append(chr(lst[i]))
        i += 1
    return "".join(out)


class Struct:
    """Base class of the generated C-struct classes."""
    _fields = ()

    def to_dict(self):
        d = {}
        for name, n in self._fields:
            v = getattr(self, name + "_k" if name in _KW else name)
            d[name] = [int(x) for x in v] if n is not None else int(v)
        return d

    def load_dict(self, d):
        for name, n in self._fields:
            attr = name + "_k" if name in _KW else name
            if n is not None:
                getattr(self, attr)[:] = d[name]
            else:
                setattr(self, attr, d[name])


_KW = {"g", "in", "is", "not", "or", "and", "del", "from", "pass", "lambda", "global", "def",
       "class", "yield", "with", "as", "try", "except", "raise", "None", "True", "False",
       "print", "exec", "assert", "async", "await", "nonlocal", "elif", "import",
       "finally", "while", "for", "if", "else", "return", "break", "continue"}


class flat:
    """`(integer *)&some_struct + k`: the struct's integers as one flat array."""

    def __init__(self, struct, off=0):
        self.s, self.off = struct, off

    def _loc(self, i):
        i += self.off
        for name, n in self.s._fields:
            attr = name + "_k" if name in _KW else name
            size = 1 if n is None else n
            if i < size:
                return attr, (None if n is None else i)
            i -= size
        raise IndexError(i)

    def __getitem__(self, i):
        attr, j = self._loc(i)
        v = getattr(self.s, attr)
        return v if j is None else v[j]

    def __setitem__(self, i, value):
        attr, j = self._loc(i)
        if j is None:
            setattr(self.s, attr, value)
        else:
            getattr(self.s, attr)[j] = value

    def __add__(self, n):
        return flat(self.s, self.off + n)

    @property
    def v(self):
        return self[0]

    @v.setter
    def v(self, value):
        self[0] = value


class GameExit(Exception):
    """The game ended (the C program called exit_())."""


# --------------------------------------------------------- the text data file
_DATA = {}


def text_data():
    if "d" not in _DATA:
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dtextc.dat")
        with open(path, "rb") as fh:
            _DATA["d"] = fh.read()
    return _DATA["d"]


class DBFile:
    """The few stdio calls the game makes on its data file."""

    def __init__(self, data):
        self.data, self.pos = data, 0

    def getc(self):
        if self.pos >= len(self.data):
            return -1
        b = self.data[self.pos]
        self.pos += 1
        return b


# ------------------------------------------------------------- game I/O object
class GameIO:
    """Connects one running game to its host.

    The game runs in its own thread; whenever it needs a line of input it marks
    itself idle and blocks until the host supplies one.
    """

    def __init__(self, rand=None):
        self.out = []
        self.inq = queue.Queue()
        self.idle = __import__("threading").Event()
        self.closed = False
        self.at_main = False
        self.snapshot = None
        self.on_main_prompt = None
        self.rand = rand or _default_rand()
        self.error = None
        self.resuming = False

    def write(self, text):
        self.out.append(text)

    def take(self):
        text = "".join(self.out)
        self.out.clear()
        return text

    def read_line(self):
        self.idle.set()
        line = self.inq.get()
        self.idle.clear()
        if line is None or self.closed:
            raise GameExit()
        return line


def _default_rand():
    import random
    r = random.Random()
    return lambda: r.randrange(32768)


def lcg_rand(seed=1):
    """The generator used by the cross-check against the C program (see tests)."""
    state = [seed & 0x7fffffff]

    def rand():
        state[0] = (state[0] * 1103515245 + 12345) & 0x7fffffff
        return (state[0] >> 16) & 0x7fff
    return rand


# ------------------------------------------------------------- C library shims
def c_printf(g, fmt, *args):
    args = tuple(pystr(a) if isinstance(a, (ItemRef, CBuf)) else a for a in args)
    g.io.write(fmt % args if args else fmt.replace("%%", "%"))


def c_putchar(g, c):
    g.io.write(chr(c))
    return c


def c_noop(g, *a):
    return 0


def c_strlen(g, p):
    return len(pystr(p))


def c_strcmp(g, a, b):
    x, y = pystr(a), pystr(b)
    return (x > y) - (x < y)


def c_strcpy(g, dst, src):
    s = pystr(src)
    for k, ch in enumerate(s):
        dst[k] = ord(ch)
    dst[len(s)] = 0
    return dst


def c_isalpha(g, c):
    return 65 <= c <= 90 or 97 <= c <= 122


def c_islower(g, c):
    return 97 <= c <= 122


def c_isupper(g, c):
    return 65 <= c <= 90


def c_isspace(g, c):
    return c in (32, 9, 10, 11, 12, 13)


def c_isdigit(g, c):
    return 48 <= c <= 57


def c_toupper(g, c):
    return c - 32 if 97 <= c <= 122 else c


def c_tolower(g, c):
    return c + 32 if 65 <= c <= 90 else c


def c_memset(g, p, v, n):
    for k in range(n):
        p[k] = v
    return p


def c_fopen(g, *a):
    return None


def c_getc(g, f):
    return f.getc()


def c_ftell(g, f):
    return f.pos


def c_fseek(g, f, off, whence):
    f.pos = off if whence == 0 else f.pos + off
    return 0


def c_open_text(g):
    return DBFile(text_data())


def c_rdint(g, f):
    """Two-byte signed big-endian integer from the data file."""
    hi = f.getc()
    if hi > 127:
        hi -= 256
    return hi * 256 + f.getc()


def c_resuming(g):
    return 1 if g.io.resuming else 0


def c_protected(g):
    return 1


def c_wizard(g):
    return 0


# ---------------------------------------------- hand-written game functions
def exit_(g):
    raise GameExit()


def more_output(g, z=None):
    if z is not None and not (isinstance(z, int) and z == 0):   # NULL expands to (0)
        g.io.write(pystr(z) + "\n")


def more_init(g):
    pass


def more_input(g):
    pass


def itime_(g, hr, mn, sc):
    t = time.localtime()
    hr.v, mn.v, sc.v = t.tm_hour, t.tm_min, t.tm_sec


def rnd_(g, maxval):
    return g.io.rand() % maxval


def gdt_(g):
    """The game debugging tool (a cheat console) is not available."""
    g.io.write("No such thing as GDT here.\n")


def protected(g):
    return 1


def wizard(g):
    return 0


def savegm_(g):
    g.io.write("Your game is saved automatically after every move.\n")
    g.play_.telflg = 1
    return 0


def rstrgm_(g):
    g.io.write("Your game is saved automatically; /zork picks it up again.\n")
    g.play_.telflg = 1
    return 0


def litint_(g, obj, ctr, cev, ticks, tickln):
    from . import engine_gen as E
    ctr.v += 1
    g.cevent_.ctick[cev - 1] = ticks[ctr.v - 1]
    if g.cevent_.ctick[cev - 1] == 0:
        g.objcts_.oflag1[obj - 1] &= ~((64) + (8) + (1))
        if g.objcts_.oroom[obj - 1] == g.play_.here or \
                g.objcts_.oadv[obj - 1] == g.play_.winner:
            E.rspsub_(g, 293, g.objcts_.odesc2[obj - 1])
        return
    if g.objcts_.oroom[obj - 1] == g.play_.here or \
            g.objcts_.oadv[obj - 1] == g.play_.winner:
        E.rspeak_(g, ticks[ctr.v - 1 + tickln // 2])


def rdints(g, c, pi, f):
    k = 0
    while c != 0:
        c -= 1
        pi[k] = c_rdint(g, f)
        k += 1


def rdpartialints(g, c, pi, f):
    while True:
        if c < 255:
            i = f.getc()
            if i == 255:
                return
        else:
            i = c_rdint(g, f)
            if i == -1:
                return
        pi[i] = c_rdint(g, f)


def rdflags(g, c, pf, f):
    k = 0
    while c != 0:
        c -= 1
        pf[k] = f.getc()
        k += 1


def rspsb2nl_(g, n, y, z, nl):
    """Print message n; '#' in it is replaced by message y (then z)."""
    from . import engine_gen as E
    zkey = "IanLanceTaylorJr"
    x = n
    if x > 0:
        x = g.rmsg_.rtext[x - 1]
    if x == 0:
        return
    g.play_.telflg = 1
    x = ((-x) - 1) * 8
    f = g.dbfile
    f.pos = x + g.rmsg_.mrloc
    while True:
        i = f.getc()
        if i == -1:
            raise GameExit()
        i ^= ord(zkey[x & 0xf]) ^ (x & 0xff)
        x += 1
        if i == 0:
            break
        elif i == 10:
            g.io.write("\n")
        elif i == 35 and y != 0:        # '#': substitute another message
            iloc = f.pos
            rspsb2nl_(g, y, 0, 0, 0)
            f.pos = iloc
            y = z
            z = 0
        else:
            g.io.write(chr(i))
    if nl:
        g.io.write("\n")


def rdline_(g, buffer, who):
    """Read one line of player input into buffer (upper-cased, trimmed)."""
    io = g.io
    while True:
        if who == 1:
            io.write(">")
            io.at_main = True
            if io.on_main_prompt:
                io.on_main_prompt(g)
        line = io.read_line()
        io.at_main = False
        s = line.replace("\r", "").split("\n")[0][:76]
        s = "".join(ch.upper() if "a" <= ch <= "z" else ch for ch in s if ord(ch) < 128)
        s = s.rstrip(" ")
        if not s:
            continue
        for k, ch in enumerate(s):
            buffer[k] = ord(ch)
        buffer[len(s)] = 0
        g.prsvec_.prscon = 1
        return


def yesno_(g, q, y, n):
    from . import engine_gen as E
    while True:
        E.rspeak_(g, q)
        line = g.io.read_line().strip()
        c = line[:1]
        if c in ("Y", "y"):
            E.rspeak_(g, y)
            return 1
        if c in ("N", "n"):
            E.rspeak_(g, n)
            return 0
        E.rspeak_(g, 6)
