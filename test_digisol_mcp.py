"""Offline tests: no ONT needed. Run: python3 -m unittest -v"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import digisol_mcp as m

REAL_HELP = ("command error!\n\nCommands after \"show\" are:\n\n"
             "    acl                       Access control list            \n"
             "    status                    System status                  \n"
             "    wan                       WAN settings                   \n")

FAKE_SSH = textwrap.dedent("""\
    #!%s
    # Pretends to be ssh + a Digisol CLI. Checks the password via $SSH_ASKPASS like real ssh does.
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
        self.assertEqual(m.handle({"method": "initialize", "params": {}})["serverInfo"]["name"], "digisol-mcp")
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


if __name__ == "__main__":
    unittest.main()
