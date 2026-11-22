"""Tests for hostops.py and the /status and /reboot commands
(run: python -m unittest tests.test_hostops -v)."""

import os
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import hostops
import trek
from tests.test_zork import import_bridge


def done(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class EnvTests(unittest.TestCase):
    def test_load_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w") as fh:
                fh.write("# comment\nHOSTOPS_A=one\nHOSTOPS_B = \"two words\"\n"
                         "HOSTOPS_C='x'\nHOSTOPS_KEEP=file\nnonsense\n")
            with mock.patch.dict(os.environ, {"HOSTOPS_KEEP": "real"}):
                self.assertTrue(hostops.load_env(path))
                self.assertEqual(os.environ["HOSTOPS_A"], "one")
                self.assertEqual(os.environ["HOSTOPS_B"], "two words")
                self.assertEqual(os.environ["HOSTOPS_C"], "x")
                self.assertEqual(os.environ["HOSTOPS_KEEP"], "real")   # real env wins
                for k in ("HOSTOPS_A", "HOSTOPS_B", "HOSTOPS_C"):
                    os.environ.pop(k)

    def test_missing_env_is_fine(self):
        self.assertFalse(hostops.load_env(os.path.join("no", "such", ".env")))


class PingTests(unittest.TestCase):
    def test_ok_with_time(self):
        run = mock.Mock(return_value=done(0, "64 bytes from x: time=12.6 ms"))
        self.assertEqual(hostops.ping("h", run=run), (True, 12.6))

    def test_windows_style_less_than(self):
        run = mock.Mock(return_value=done(0, "Reply from x: bytes=32 time<1ms TTL=64"))
        self.assertEqual(hostops.ping("h", run=run), (True, 1.0))

    def test_nonzero_exit_is_down(self):
        self.assertEqual(hostops.ping("h", run=mock.Mock(return_value=done(1))), (False, None))

    def test_timeout_and_missing_binary_are_down(self):
        for exc in (subprocess.TimeoutExpired("ping", 5), FileNotFoundError()):
            self.assertEqual(hostops.ping("h", run=mock.Mock(side_effect=exc)), (False, None))

    def test_ping_text(self):
        with mock.patch.object(hostops, "ping", return_value=(True, 3.2)):
            self.assertEqual(hostops.ping_text("Router", "h"), "Router: ok 3ms")
        with mock.patch.object(hostops, "ping", return_value=(False, None)):
            self.assertEqual(hostops.ping_text("Router", "h"), "Router: DOWN")


class StatusTests(unittest.TestCase):
    def test_lines(self):
        fake = lambda label, host: "%s: ok (%s)" % (label, host)
        with mock.patch.object(hostops, "host_uptime", return_value=90000):
            lines = hostops.status_lines(1000, "192.168.55.1", "google.com", now=1000 + 7300,
                                         pinger=fake)
        self.assertEqual(lines, ["Bridge up 2h 1m | Host up 1d 1h",
                                 "Router: ok (192.168.55.1)", "Internet: ok (google.com)"])

    def test_unknown_host_uptime_is_left_out(self):
        fake = lambda label, host: label
        with mock.patch.object(hostops, "host_uptime", return_value=None):
            self.assertEqual(hostops.status_lines(0, "r", "s", now=300, pinger=fake)[0],
                             "Bridge up 5m")

    def test_fmt_duration(self):
        self.assertEqual(hostops.fmt_duration(59), "0m")
        self.assertEqual(hostops.fmt_duration(3 * 86400 + 5 * 3600), "3d 5h")


class RebooterTests(unittest.TestCase):
    def make(self, **kw):
        self.calls = []
        self.failures = []
        self.ran = threading.Event()
        result = kw.pop("result", None) or done()

        def runner(argv, **k):
            self.calls.append((argv, k.get("input")))
            self.ran.set()
            return result

        opts = dict(delay_s=0.05, platform="linux", is_root=False, runner=runner,
                    before=lambda: self.calls.append("before"))
        opts.update(kw)
        return hostops.Rebooter(**opts)

    def test_reboots_after_delay_flushing_first(self):
        r = self.make(password="pw")
        reply = r.schedule(self.failures.append)
        self.assertRegex(reply, r"^Rebooting at \d\d:\d\d:\d\d$")
        self.assertTrue(r.pending())
        self.assertTrue(self.ran.wait(2))
        self.assertEqual(self.calls, ["before", (["sudo", "-S", "-p", "", "reboot"], "pw\n")])
        self.assertEqual(self.failures, [])

    def test_no_password_uses_non_interactive_sudo(self):
        r = self.make()
        r.schedule(self.failures.append)
        self.assertTrue(self.ran.wait(2))
        self.assertEqual(self.calls[-1], (["sudo", "-n", "reboot"], None))

    def test_root_needs_no_sudo(self):
        r = self.make(is_root=True, password="pw")
        r.schedule(self.failures.append)
        self.assertTrue(self.ran.wait(2))
        self.assertEqual(self.calls[-1], (["reboot"], None))

    def test_cancel(self):
        r = self.make(delay_s=0.3)
        r.schedule(self.failures.append)
        self.assertEqual(r.cancel(), "Reboot cancelled")
        time.sleep(0.5)
        self.assertEqual(self.calls, [])
        self.assertEqual(r.cancel(), "No reboot pending")

    def test_second_request_does_not_stack(self):
        r = self.make(delay_s=0.3)
        r.schedule(self.failures.append)
        self.assertIn("already set", r.schedule(self.failures.append))
        r.cancel()

    def test_failure_is_reported(self):
        r = self.make(result=done(1, stderr="sudo: a password is required\n"))
        r.schedule(self.failures.append)
        self.assertTrue(self.ran.wait(2))
        deadline = time.time() + 2
        while not self.failures and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.failures, ["Reboot failed: sudo: a password is required"])
        self.assertFalse(r.pending())

    def test_refuses_off_linux(self):
        r = self.make(platform="win32")
        self.assertEqual(r.schedule(self.failures.append), "Reboot is only supported on Linux")
        self.assertFalse(r.pending())


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.mb = import_bridge()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(self.mb, "TREK", trek.TrekGames(
            os.path.join(self.tmp.name, "trek.json")))
        patch.start()
        self.addCleanup(patch.stop)
        cfg = types.SimpleNamespace(
            allow=None, admin=["!boss"], dm_only=False, reply_gap=0, session_idle_min=30,
            zork_max_chunks=None, zork_save_dir=os.path.join(self.tmp.name, "z"),
            adventure_save_dir=os.path.join(self.tmp.name, "a"),
            wumpus_save_dir=os.path.join(self.tmp.name, "w"),
            stats_file=os.path.join(self.tmp.name, "stats.json"),
            status_router="192.168.55.1", status_host="google.com", reboot_delay=30,
            lat=0, lon=0, near_km=2, announce=None, announce_channel=0)
        self.bridge = self.mb.Bridge(cfg)
        self.bridge.my_id = "!me"
        self.bridge.rebooter.platform = "linux"
        self.bridge.rebooter.runner = mock.Mock(return_value=done())
        self.addCleanup(self.bridge.rebooter.cancel)
        self.sent = []
        self.bridge.send = lambda dest, text: self.sent.append((dest, text))
        self.bridge.sender_position = lambda node: None
        threading.Thread(target=self.bridge.worker, daemon=True).start()

    def msg(self, sender, text, expect=1):
        before = len(self.sent)
        self.bridge.on_receive({"decoded": {"text": text}, "fromId": sender,
                                "toId": "!me"}, None)
        deadline = time.time() + 5
        while len(self.sent) < before + expect and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        return [t for _, t in self.sent[before:]]

    def test_status_is_public_and_hides_addresses(self):
        with mock.patch.object(hostops, "ping", side_effect=[(True, 2.0), (False, None)]):
            out = "\n".join(self.msg("!aaaa", "/status"))
        self.assertIn("Bridge up", out)
        self.assertRegex(out, r"Router: (ok 2ms|DOWN)")
        self.assertRegex(out, r"Internet: (ok 2ms|DOWN)")
        self.assertNotIn("192.168", out)

    def test_reboot_needs_yes(self):
        out = self.msg("!boss", "/reboot")
        self.assertIn("/reboot yes", out[0])
        self.assertFalse(self.bridge.rebooter.pending())
        self.msg("!boss", "/reboot maybe")
        self.assertFalse(self.bridge.rebooter.pending())

    def test_reboot_yes_confirms_with_time_then_cancel(self):
        out = self.msg("!boss", "/reboot yes")
        self.assertRegex(out[0], r"^Rebooting at \d\d:\d\d:\d\d$")
        self.assertTrue(self.bridge.rebooter.pending())
        self.assertEqual(self.msg("!boss", "/reboot cancel"), ["Reboot cancelled"])
        self.bridge.rebooter.runner.assert_not_called()

    def test_reboot_is_admin_only(self):
        self.assertEqual(self.msg("!aaaa", "/reboot yes", expect=0), [])
        self.assertFalse(self.bridge.rebooter.pending())
        self.assertNotIn("reboot", "\n".join(self.msg("!aaaa", "/help")))


if __name__ == "__main__":
    unittest.main()
