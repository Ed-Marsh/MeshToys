"""Host-level helpers for /status and /reboot: a tiny .env loader, ping, host
uptime and a delayed, cancellable reboot."""

import logging
import os
import re
import subprocess
import sys
import threading
import time

log = logging.getLogger("mesh_bridge.hostops")

DEFAULT_REBOOT_DELAY_S = 15
PING_RE = re.compile(r"time\s*([=<])\s*([\d.]+)\s*ms", re.I)


def load_env(path):
    """Read KEY=VALUE lines from a .env file into os.environ. Variables that
    are already set win, so the real environment can override the file.
    Returns True if the file existed."""
    try:
        fh = open(path, encoding="utf-8")
    except FileNotFoundError:
        return False
    with fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                os.environ.setdefault(key, value)
    return True


def ping(host, timeout_s=2, run=subprocess.run):
    """Send one ping. Returns (ok, milliseconds or None)."""
    if sys.platform.startswith("win"):
        cmd = ["ping", "-n", "1", "-w", str(int(timeout_s * 1000)), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(int(timeout_s)), host]
    try:
        r = run(cmd, capture_output=True, text=True, timeout=timeout_s + 3)
    except (OSError, subprocess.SubprocessError):
        return False, None
    if r.returncode != 0:
        return False, None
    m = PING_RE.search(r.stdout or "")
    return True, (float(m.group(2)) if m else None)


def ping_text(label, host, **kw):
    ok, ms = ping(host, **kw)
    if not ok:
        return f"{label}: DOWN"
    return f"{label}: ok" + (f" {ms:.0f}ms" if ms is not None else "")


def host_uptime():
    """Seconds since the host booted, or None if unknown."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/uptime") as fh:
                return float(fh.read().split()[0])
        if sys.platform.startswith("win"):
            import ctypes
            return ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except (OSError, ValueError, AttributeError):
        pass
    return None


def fmt_duration(seconds):
    s = int(seconds)
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def status_lines(bridge_started, router, site, now=None, pinger=ping_text):
    """The /status report. The two pings run side by side to keep it quick."""
    now = time.time() if now is None else now
    out = {}
    threads = [threading.Thread(target=lambda k=k, label=label, host=host:
                                out.__setitem__(k, pinger(label, host)))
               for k, label, host in (("r", "Router", router), ("s", "Internet", site))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    up = host_uptime()
    first = "Bridge up " + fmt_duration(now - bridge_started)
    if up is not None:
        first += " | Host up " + fmt_duration(up)
    return [first, out["r"], out["s"]]


class Rebooter:
    """Schedules one reboot a few seconds ahead, so the confirmation reply can
    go out first, and lets it be cancelled in the meantime."""

    def __init__(self, delay_s=DEFAULT_REBOOT_DELAY_S, password=None, before=None,
                 runner=subprocess.run, clock=time.time, platform=None, is_root=None):
        self.delay_s = delay_s
        self.password = password
        self.before = before             # called just before rebooting (flush stats)
        self.runner = runner
        self.clock = clock
        self.platform = platform or sys.platform
        self.is_root = (hasattr(os, "geteuid") and os.geteuid() == 0) if is_root is None else is_root
        self.lock = threading.Lock()
        self.timer = None
        self.when = None

    def _command(self):
        """(argv, stdin text) for the reboot."""
        if self.is_root:
            return ["reboot"], None
        if self.password:
            return ["sudo", "-S", "-p", "", "reboot"], self.password + "\n"
        return ["sudo", "-n", "reboot"], None

    def pending(self):
        return self.timer is not None

    def schedule(self, on_failure):
        """Arm the reboot. Returns the reply text; on_failure(text) is called
        if the reboot command turns out not to work."""
        if not self.platform.startswith("linux"):
            return "Reboot is only supported on Linux"
        with self.lock:
            if self.timer is not None:
                return "Reboot already set for %s. /reboot cancel to stop it" % self._at()
            self.when = self.clock() + self.delay_s
            self.timer = threading.Timer(self.delay_s, self._fire, args=(on_failure,))
            self.timer.daemon = True
            self.timer.start()
            return "Rebooting at %s" % self._at()

    def _at(self):
        return time.strftime("%H:%M:%S", time.localtime(self.when))

    def cancel(self):
        with self.lock:
            if self.timer is None:
                return "No reboot pending"
            self.timer.cancel()
            self.timer = None
            return "Reboot cancelled"

    def _fire(self, on_failure):
        with self.lock:
            self.timer = None
        log.warning("Rebooting now")
        try:
            if self.before:
                self.before()
            argv, stdin = self._command()
            r = self.runner(argv, input=stdin, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                tail = ((r.stderr or "").strip().splitlines() or ["no detail"])[-1][:80]
                log.error("Reboot command failed (%s): %s", r.returncode, tail)
                on_failure("Reboot failed: " + tail)
        except Exception as e:  # noqa: BLE001
            log.exception("Reboot failed")
            on_failure("Reboot failed: " + type(e).__name__)
