"""Offline tests: no ONT needed. Run: python3 -m unittest -v"""

import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

import ont_mcp as m

REAL_HELP = ("command error!\n\nCommands after \"show\" are:\n\n"
             "    acl                       Access control list            \n"
             "    status                    System status                  \n"
             "    wan                       WAN settings                   \n")

FAKE_SSH = textwrap.dedent("""\
    #!%s
    # Pretends to be ssh + an ONT CLI. Checks the password via $SSH_ASKPASS like real ssh does.
    import os, subprocess, sys
    pw = subprocess.run([os.environ["SSH_ASKPASS"], "Password:"], capture_output=True, text=True).stdout
    if pw != os.environ["FAKE_ONT_PASSWORD"]:
        sys.stderr.write("admin@ont: Permission denied (password).\\n"); sys.exit(255)
    answers = {"?": "command error!\\n\\n    reboot        Reboot system   \\n    show          Show system information   \\n",
               "show ?": "command error!\\n\\n    status        System status   \\n",
               "show status": "Uptime: 3 days\\nFirmware: V1.2"}
    sys.stdout.write("Welcome to the ONT\\n>"); sys.stdout.flush()
    for line in sys.stdin:
        c = line.strip()
        sys.stdout.write(c + "\\r\\n" + answers.get(c, "command error!") + "\\r\\n>"); sys.stdout.flush()
    """) % sys.executable

DEV = m.make_device("home", {"host": "10.0.0.1", "password": "pw"})


class Config(unittest.TestCase):
    def test_env_single_device(self):
        devs, default = m.load_config({"ONT_HOST": "192.168.1.1", "ONT_PASSWORD": "x", "ONT_READ_ONLY": "true"})
        self.assertEqual(default, "ont")
        self.assertEqual((devs["ont"]["host"], devs["ont"]["user"], devs["ont"]["read_only"]), ("192.168.1.1", "admin", True))

    def test_json_several_devices(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"default": "b", "devices": {"a": {"host": "1.1.1.1"}, "b": {"host": "2.2.2.2", "port": 2222}}}, f)
        devs, default = m.load_config({"ONT_CONFIG": f.name})
        self.assertEqual((default, devs["b"]["port"]), ("b", 2222))

    def test_typo_and_missing_host_are_explained(self):
        with self.assertRaisesRegex(m.OntError, "unknown field.*pasword"):
            m.make_device("x", {"host": "1.1.1.1", "pasword": "oops"})
        with self.assertRaisesRegex(m.OntError, "needs a host"):
            m.make_device("x", {})

    def test_nothing_configured(self):
        with mock.patch.object(m.os.path, "isfile", return_value=False):
            with self.assertRaisesRegex(m.OntError, "no ONT configured"):
                m.load_config({})

    def test_password_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("s3cret pass\n")
        self.assertEqual(m.get_password(m.make_device("x", {"host": "h", "password_file": f.name})), "s3cret pass")


class Classify(unittest.TestCase):
    def setUp(self):
        words = {"": ["backup", "config", "reboot", "show"], "show": ["status", "wan"]}
        p = mock.patch.object(m, "cli_words", side_effect=lambda d, path: words[path])
        p.start()
        self.addCleanup(p.stop)

    def level(self, cmd, dev=DEV):
        return m.classify(cmd, dev)[0]

    def test_levels(self):
        for c in ("show status", "show wan", "?", "config ?", "config wan ?", "help"):
            self.assertEqual(self.level(c), "read", c)
        self.assertEqual(m.classify("config dmz enable", DEV), ("change", "changes a setting on the ONT", "YES"))
        for c, word in (("reboot", "REBOOT"), ("config reset", "RESET"), ("config wan add x", "WAN"),
                        ("config remoteaccess http lan deny", "REMOTEACCESS"), ("backup", "BACKUP")):
            level, _, w = m.classify(c, DEV)
            self.assertEqual((level, w), ("critical", word), c)

    def test_refused(self):
        for bad in ("show nonsense", "rm -rf /", "exit", "show"):
            with self.assertRaises(m.OntError, msg=bad):
                self.level(bad)
        with self.assertRaises(m.OntError):
            m.check_text("show status\nreboot")

    def test_read_only_device(self):
        ro = m.make_device("ro", {"host": "h", "read_only": True})
        self.assertEqual(self.level("show status", ro), "read")
        with self.assertRaisesRegex(m.OntError, "read_only"):
            self.level("config save", ro)


class Approval(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(m, "cli_words", return_value=[]), mock.patch.object(m.sys, "platform", "darwin"),
                  mock.patch.object(m, "resolve", return_value=DEV)):
            p.start()
            self.addCleanup(p.stop)

    def dialog(self, stdout, code=0):
        return mock.patch.object(m.subprocess, "run", return_value=subprocess.CompletedProcess([], code, stdout, ""))

    def test_reads_never_ask(self):
        with mock.patch.object(m, "run_on_ont", return_value="ok"), mock.patch.object(m, "ask_consent") as ask:
            self.assertIn("ok", m.execute("show status"))
        ask.assert_not_called()

    def test_changes_need_the_typed_word(self):
        for out, code in (("button returned:Run, text returned:yes please, gave up:false", 0),
                          ("button returned:Cancel, text returned:REBOOT, gave up:false", 0),
                          ("button returned:, text returned:, gave up:true", 0),
                          ("", 1)):
            with self.dialog(out, code), mock.patch.object(m, "run_on_ont") as run:
                with self.assertRaisesRegex(m.OntError, "not run"):
                    m.execute("reboot", "test")
            run.assert_not_called()

    def test_no_dialog_means_no_change(self):
        with mock.patch.object(m.subprocess, "run", side_effect=OSError("headless")), \
                mock.patch.object(m, "run_on_ont") as run:
            with self.assertRaises(m.OntError):
                m.execute("config save")
        run.assert_not_called()

    def test_approved_change_runs(self):
        with self.dialog("button returned:Run, text returned:REBOOT, gave up:false"), \
                mock.patch.object(m, "run_on_ont", return_value="") as run:
            self.assertIn("approved", m.execute("reboot"))
        self.assertEqual(run.call_args[0][0], "reboot")


class Parsing(unittest.TestCase):
    def test_parse_real_help(self):
        self.assertEqual(m.parse_help(REAL_HELP), ["acl", "status", "wan"])

    def test_clean(self):
        raw = "config save\r\nip6tables v1.4.18: unknown option\r\narg_name:lan\r\nDone\r\n>"
        self.assertEqual(m.clean(raw, "config save", DEV["prompt"]), "Done")
        self.assertEqual(m.clean("show ?\ncommand error!\n\n    a   b\n>", "show ?", DEV["prompt"]), "    a   b")
        self.assertEqual(m.clean("config x\ncommand error!\n>", "config x", DEV["prompt"]), "command error!")


class Protocol(unittest.TestCase):
    def test_handshake_and_tools(self):
        self.assertEqual(m.handle({"method": "initialize", "params": {}})["serverInfo"]["name"], "ont-mcp")
        names = [t["name"] for t in m.handle({"method": "tools/list"})["tools"]]
        self.assertEqual(names, ["ont_show", "ont_help", "ont_run"])

    def test_config_error_reaches_the_client(self):
        with mock.patch.object(m, "CONFIG_ERROR", "no ONT configured"):
            r = m.handle({"method": "tools/call", "params": {"name": "ont_show", "arguments": {"item": "status"}}})
        self.assertTrue(r["isError"])
        self.assertIn("no ONT configured", r["content"][0]["text"])


@unittest.skipIf(os.name == "nt", "fake ssh is a POSIX script")
class EndToEnd(unittest.TestCase):
    """Real subprocess, real askpass wrapper, fake ssh/ONT."""

    def setUp(self):
        bindir = tempfile.mkdtemp()
        ssh = os.path.join(bindir, "ssh")
        with open(ssh, "w") as f:
            f.write(FAKE_SSH)
        os.chmod(ssh, stat.S_IRWXU)
        env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "FAKE_ONT_PASSWORD": "right pass"}
        for p in (mock.patch.dict(os.environ, env), mock.patch.object(m, "MIN_GAP", 0), mock.patch.dict(m._cli, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def run_show(self, password):
        dev = m.make_device("home", {"host": "10.0.0.1", "password": password})
        with mock.patch.object(m, "resolve", return_value=dev):
            return m.execute("show status")

    def test_learns_cli_and_reads(self):
        out = self.run_show("right pass")
        self.assertIn("Uptime: 3 days", out)
        self.assertEqual(m._cli[("home", "show")], ["status"])

    def test_wrong_password_is_explained(self):
        with self.assertRaisesRegex(m.OntError, "wrong username or password"):
            self.run_show("wrong")


class FakeTelnetOnt:
    """A tiny Telnet ONT: negotiates options, asks for a password (and a username first if asked), then a CLI."""

    ANSWERS = {"?": "command error!\n\n    reboot        Reboot system   \n    show          Show system information   \n",
               "show ?": "command error!\n\n    status        System status   \n",
               "show status": "Uptime: 3 days\nFirmware: V1.2"}

    def __init__(self, ask_user=False):
        self.ask_user, self.attempts, self.user, self.sent = ask_user, 0, None, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.run, args=(conn,), daemon=True).start()

    def run(self, conn):
        try:
            self.session(conn)
        finally:
            conn.close()

    def readline(self, conn, buf):
        while b"\n" not in buf[0]:
            data = conn.recv(4096)
            if not data:
                return None
            buf[0] += data
        line, buf[0] = buf[0].split(b"\n", 1)
        return re.sub(rb"\xff[\xfb-\xfe].", b"", line).strip(b"\r\0").decode()   # drop option replies

    def session(self, conn):
        buf = [b""]
        conn.sendall(bytes([255, 251, 1, 255, 251, 3]))   # IAC WILL ECHO, IAC WILL SUPPRESS-GO-AHEAD
        if self.ask_user:
            conn.sendall(b"login: ")
            self.user = self.readline(conn, buf)
        while True:
            conn.sendall(b"Password: ")
            pw = self.readline(conn, buf)
            if pw is None:
                return
            self.attempts += 1
            if pw == "right":
                break
            conn.sendall(b"Login incorrect\r\n")
        conn.sendall(b"Open /dev/voip/mgr fail, ret = -1, errno = 13\r\n>")
        while True:
            c = self.readline(conn, buf)
            if c is None:
                return
            self.sent.append(c)
            conn.sendall(self.ANSWERS.get(c, "command error!").replace("\n", "\r\n").encode() + b"\r\n>")


class TelnetFallback(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(m, "MIN_GAP", 0), mock.patch.dict(m._cli, clear=True),
                  mock.patch.object(m, "_via", {}), mock.patch.object(m, "_telnet_only", set())):
            p.start()
            self.addCleanup(p.stop)

    def ont(self, **kw):
        fake = FakeTelnetOnt(**kw)
        self.addCleanup(fake.sock.close)
        return fake

    def dev(self, fake, password="right", **extra):
        return m.make_device("t", dict({"host": "127.0.0.1", "password": password, "telnet_port": fake.port,
                                        "protocol": "telnet"}, **extra))

    def test_private_and_public_addresses(self):
        for host in ("10.0.0.1", "192.168.3.1", "172.16.5.5", "127.0.0.1", "169.254.1.1"):
            self.assertTrue(m.telnet_target(m.make_device("x", {"host": host})), host)
        for host in ("8.8.8.8", "1.1.1.1"):
            with self.assertRaisesRegex(m.OntError, "not a private address"):
                m.telnet_target(m.make_device("x", {"host": host}))
        m.telnet_target(m.make_device("x", {"host": "8.8.8.8", "allow_public_telnet": True}))

    def test_protocol_is_validated(self):
        with self.assertRaisesRegex(m.OntError, "protocol must be"):
            m.make_device("x", {"host": "h", "protocol": "rsh"})

    def test_telnet_login_and_command(self):
        fake = self.ont()
        out = m.run_on_ont("show status", self.dev(fake))
        self.assertEqual(out, "Uptime: 3 days\nFirmware: V1.2")
        self.assertEqual(fake.sent, ["show status"])

    def test_username_is_sent_only_when_asked(self):
        fake = self.ont(ask_user=True)
        m.run_on_ont("show status", self.dev(fake, user="operator"))
        self.assertEqual(fake.user, "operator")

    def test_wrong_password_is_one_attempt(self):
        fake = self.ont()
        with self.assertRaisesRegex(m.OntError, "wrong username or password"):
            m.run_on_ont("show status", self.dev(fake, password="nope"))
        self.assertEqual(fake.attempts, 1)   # no retry: no account lockout
        self.assertEqual(fake.sent, [])

    def test_no_prompt_means_no_secret_is_sent(self):
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        self.addCleanup(silent.close)
        dev = m.make_device("t", {"host": "127.0.0.1", "password": "right", "protocol": "telnet",
                                  "telnet_port": silent.getsockname()[1]})
        with mock.patch.object(m, "LOGIN_TIMEOUT", 0.3), self.assertRaisesRegex(m.OntError, "no login prompt"):
            m.run_on_ont("show status", dev)

    def test_auto_falls_back_when_ssh_is_refused_and_remembers(self):
        fake = self.ont()
        dev = self.dev(fake, protocol="auto")
        with mock.patch.object(m, "run_ssh", side_effect=m.SshRefused("refused")) as ssh:
            self.assertIn("Uptime", m.run_on_ont("show status", dev))
            self.assertIn("Uptime", m.run_on_ont("show status", dev))
        self.assertEqual(ssh.call_count, 1)   # the second call went straight to Telnet
        self.assertEqual(m._via["t"], "telnet")

    def test_ssh_only_never_falls_back(self):
        fake = self.ont()
        with mock.patch.object(m, "run_ssh", side_effect=m.SshRefused("refused")):
            with self.assertRaises(m.SshRefused):
                m.run_on_ont("show status", self.dev(fake, protocol="ssh"))
        self.assertEqual(fake.attempts, 0)

    def test_other_ssh_errors_do_not_fall_back(self):
        fake = self.ont()
        with mock.patch.object(m, "run_ssh", side_effect=m.OntError("wrong username or password")):
            with self.assertRaisesRegex(m.OntError, "wrong username"):
                m.run_on_ont("show status", self.dev(fake, protocol="auto"))
        self.assertEqual(fake.attempts, 0)

    def test_public_address_blocks_the_fallback(self):
        dev = m.make_device("t", {"host": "8.8.8.8", "password": "right"})
        with mock.patch.object(m, "run_ssh", side_effect=m.SshRefused("refused")), \
                mock.patch.object(m.socket, "create_connection") as dial:
            with self.assertRaisesRegex(m.OntError, "Telnet fallback is blocked"):
                m.run_on_ont("show status", dev)
        dial.assert_not_called()

    def test_result_says_telnet_was_used(self):
        fake = self.ont()
        dev = self.dev(fake)
        with mock.patch.object(m, "resolve", return_value=dev):
            self.assertIn("Telnet, unencrypted", m.execute("show status"))


if __name__ == "__main__":
    unittest.main()
