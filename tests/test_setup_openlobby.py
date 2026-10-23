"""hostsetup/openlobby.py and setup's first start, with Docker replaced by a fake."""
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import setup
from hostsetup import openlobby

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeDocker:
    """Answers `docker ...` from a small model: running containers with labels."""

    def __init__(self, running=None, names_exit=0):
        self.running = dict(running or {})     # container -> {label: value}
        self.calls = []
        self.names_exit = names_exit
        self.networks = set()

    def __call__(self, *args, capture=True, cwd=None, timeout=600):
        self.calls.append(args)
        if args[:1] == ("ps",):
            want = args[args.index("--filter") + 1][len("label="):]
            key, _, value = want.partition("=")
            return 0, "".join(n + "\n" for n, labels in self.running.items()
                              if key in labels and (not value or labels[key] == value))
        if args[:1] == ("inspect",):
            key = args[2].split('"')[1]
            return 0, self.running.get(args[3], {}).get(key, "<no value>") + "\n"
        if args[:2] == ("network", "inspect"):
            return (0, "[]") if args[2] in self.networks else (1, "no such network")
        if args[:2] == ("network", "create"):
            self.networks.add(args[2])
            return 0, ""
        if args[:1] == ("compose",) and "up" in args:
            project = args[args.index("-p") + 1]
            if project == openlobby.DNS_PROJECT:
                self.running["openlobby-dns-dns-1"] = {"org.openlobby.role": "dns"}
            elif project == openlobby.HTTP_PROJECT:
                self.running["openlobby-http-http-1"] = {"org.openlobby.role": "http"}
            elif project == openlobby.FEEGA_PROJECT:
                self.running["freega-feega-1"] = {"org.openlobby.game.key": "feega"}
            return 0, "started\n"
        if args[:1] == ("exec",) and ("dns-names" in args or "http-hosts" in args):
            return self.names_exit, "" if not self.names_exit else "bad name\n"
        return 0, ""

    def names_added(self):
        return [a[a.index("add") + 1:] for a in self.calls if a[:1] == ("exec",) and "dns-names" in a and "add" in a]

    def hosts_added(self):
        return [a[a.index("add") + 1:] for a in self.calls if a[:1] == ("exec",) and "http-hosts" in a and "add" in a]


class Temp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bomb-ol-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class DnsTest(Temp):
    def test_an_existing_dns_gets_the_names_and_nothing_starts(self):
        fake = FakeDocker({"lobby-dns-1": {"org.openlobby.role": "dns"}})
        with mock.patch.object(openlobby, "docker", fake):
            name = openlobby.dns_container()
            openlobby.dns_names(name, "bomb", setup.HOST_NAMES)
        self.assertEqual(name, "lobby-dns-1")
        self.assertEqual(fake.names_added(), [("bomb",) + setup.HOST_NAMES])
        self.assertFalse([a for a in fake.calls if a[:1] == ("compose",)])

    def test_no_dns_starts_one_from_the_openlobby_image(self):
        fake = FakeDocker()
        with mock.patch.object(openlobby, "docker", fake):
            self.assertIsNone(openlobby.dns_container())
            name = openlobby.start_dns(self.dir, "192.168.0.10")
        self.assertEqual(name, "openlobby-dns-dns-1")
        with open(os.path.join(self.dir, "compose.yml"), encoding="utf-8") as f:
            text = f.read()
        for want in ('command: ["dns"]', "org.openlobby.role: dns", 'POL_ADVERTISE: "192.168.0.10"',
                     '"0.0.0.0:53:53/udp"', "dns-data:/data", openlobby.DNS_IMAGE):
            self.assertIn(want, text)

    def test_a_refused_name_is_an_error(self):
        fake = FakeDocker({"d": {"org.openlobby.role": "dns"}}, names_exit=2)
        with mock.patch.object(openlobby, "docker", fake):
            with self.assertRaises(openlobby.Failed):
                openlobby.dns_names("d", "bomb", ["bad name"])

    def test_the_network_is_made_once(self):
        fake = FakeDocker()
        with mock.patch.object(openlobby, "docker", fake):
            openlobby.ensure_network()
            openlobby.ensure_network()
        self.assertEqual([a for a in fake.calls if a[:2] == ("network", "create")], [("network", "create", "openlobby")])


class OldImageTest(Temp):
    def test_an_image_without_the_commands_says_to_update(self):
        class Old(FakeDocker):
            def __call__(self, *args, **kw):
                if args[:1] == ("exec",):
                    return 1, "usage: stub.py [-h] {serve,dns,http} ...\n"
                return super().__call__(*args, **kw)
        with mock.patch.object(openlobby, "docker", Old()):
            for call in (lambda: openlobby.dns_names("d", "bomb", ["a.example"]),
                         lambda: openlobby.http_hosts("h", "bomb", "http://bomb-web:80", ["a.example"])):
                with self.assertRaises(openlobby.Failed) as e:
                    call()
                self.assertIn("The OpenLobby image is too old for this, update it", str(e.exception))


class HttpTest(Temp):
    def test_an_existing_web_server_gets_the_hosts(self):
        fake = FakeDocker({"lobby-http-1": {"org.openlobby.role": "http"}})
        with mock.patch.object(openlobby, "docker", fake):
            name = openlobby.http_container()
            openlobby.http_hosts(name, "bomb", setup.WEB_URL, setup.HTTP_HOSTS)
        self.assertEqual(fake.hosts_added(), [("bomb", "http://bomb-web:80") + setup.HTTP_HOSTS])
        self.assertFalse([a for a in fake.calls if a[:1] == ("compose",)])

    def test_no_web_server_starts_one_on_port_80(self):
        fake = FakeDocker()
        with mock.patch.object(openlobby, "docker", fake):
            self.assertEqual(openlobby.start_http(self.dir), "openlobby-http-http-1")
        with open(os.path.join(self.dir, "compose.yml"), encoding="utf-8") as f:
            text = f.read()
        for want in ('command: ["http"]', "org.openlobby.role: http", '"80:80"', "http-data:/data",
                     "external: true", openlobby.DNS_IMAGE):
            self.assertIn(want, text)

    def test_a_refused_host_is_an_error(self):
        fake = FakeDocker({"h": {"org.openlobby.role": "http"}}, names_exit=2)
        with mock.patch.object(openlobby, "docker", fake):
            with self.assertRaises(openlobby.Failed):
                openlobby.http_hosts("h", "bomb", setup.WEB_URL, ["*.bad"])


class FeegaTest(Temp):
    def fake_download(self, url, path):
        self.url = url
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("freega-1.0.0/compose.yml", "name: freega\n")
            z.writestr("freega-1.0.0/.env.example", "# admin token\nFEEGA_ADMIN_TOKEN=\nFEEGA_ADVERTISE=\n")

    def test_feega_is_fetched_configured_started_and_named(self):
        fake = FakeDocker({"d": {"org.openlobby.role": "dns"}})
        folder = os.path.join(self.dir, "Freega")
        with mock.patch.object(openlobby, "docker", fake):
            self.assertFalse(openlobby.feega_running())
            url = openlobby.install_feega(folder, "192.168.0.10", download=self.fake_download)
            self.assertTrue(openlobby.feega_running())
        self.assertEqual(url, "http://feega:24030")
        self.assertIn("PSChuze/freega/archive/refs/tags/", self.url)
        env = openlobby.env_lines(os.path.join(folder, ".env"), {})
        self.assertIn("FEEGA_ADVERTISE=192.168.0.10", env)
        token = [l for l in env if l.startswith("FEEGA_ADMIN_TOKEN=")][0].split("=", 1)[1]
        self.assertEqual(len(token), 32)
        up = [a for a in fake.calls if a[:1] == ("compose",)][0]
        self.assertEqual(up[:5], ("compose", "-p", "freega", "--env-file", ".env"))
        self.assertEqual(fake.names_added(), [("feega", "lib.feega.sonyfinance.co.jp")])
        self.assertFalse(os.path.exists(os.path.join(folder, "freega.zip")))


class FakePanel:
    def __init__(self):
        self.seen = []
        owner = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.seen.append((self.path, body))
                if self.path == "/api/pair":
                    code, doc = (200, {"ok": True, "pairing": "secret"}) if body["code"] == "GOODCODE" \
                        else (403, {"error": "bad code"})
                else:
                    code, doc = (200, {"ok": True}) if body["pairing"] == "secret" else (403, {"error": "no"})
                if code == 200 and "updater" in body:
                    doc["updater"] = "requested"
                out = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]


class PairTest(unittest.TestCase):
    def test_pairing(self):
        panel = FakePanel()
        self.assertEqual(openlobby.pair(panel.url, "GOODCODE", "bomb", "Bomberman Online",
                                        "http://bomb:24090", "tok")["pairing"], "secret")
        self.assertEqual(panel.seen[0], ("/api/pair", {"code": "GOODCODE", "key": "bomb", "title": "Bomberman Online",
                                                       "url": "http://bomb:24090", "token": "tok"}))
        with self.assertRaises(openlobby.Failed):
            openlobby.pair(panel.url, "WRONG", "bomb", "t", "u", "tok")
        openlobby.pair_update(panel.url, "bomb", "secret", "tok2")
        self.assertEqual(panel.seen[-1], ("/api/pair/update", {"key": "bomb", "pairing": "secret", "token": "tok2"}))
        panel.srv.shutdown()

    def test_setup_sends_the_update_block_and_passes_on_the_answer(self):
        panel = FakePanel()
        root = tempfile.mkdtemp(prefix="bomb-pair-")
        shutil.copy(os.path.join(HERE, ".env.example"), root)
        setup.use_root(root)
        try:
            setup.write_env({"BOMB_ADMIN_TOKEN": "tok"})
            out = io.StringIO()
            with mock.patch.object(openlobby, "panel_url", return_value=panel.url), \
                    mock.patch("builtins.input", lambda *_: "GOODCODE"), mock.patch("sys.stdout", out):
                setup.offer_panel(setup.read_env(), ask_first=False)
            path, body = panel.seen[-1]
            self.assertEqual(path, "/api/pair")
            self.assertEqual(body["updater"], {"folder": root, "command": ["python", "setup.py", "--update", "--yes"],
                                               "timeout_minutes": 60})
            self.assertTrue(os.path.isabs(body["updater"]["folder"]))
            self.assertIn("must approve one-click updates", out.getvalue())
            self.assertEqual(setup.read_env()["OPENLOBBY_PAIRING"], "secret")
            self.assertEqual(setup.read_choices()["updater_folder"], root)

            # the same folder is not sent again; a moved one goes through /api/pair/update
            seen = len(panel.seen)
            with mock.patch.object(openlobby, "panel_url", return_value=panel.url), mock.patch("sys.stdout", out):
                setup.resend_updater()
                self.assertEqual(len(panel.seen), seen)
                choices = setup.read_choices()
                choices["updater_folder"] = os.path.join(root, "old place")
                setup.write_choices(choices)
                setup.resend_updater()
            path, body = panel.seen[-1]
            self.assertEqual(path, "/api/pair/update")
            self.assertEqual((body["pairing"], body["token"], body["updater"]["folder"]), ("secret", "tok", root))
            self.assertEqual(setup.read_choices()["updater_folder"], root)
        finally:
            setup.use_root(HERE)
            shutil.rmtree(root, ignore_errors=True)
            panel.srv.shutdown()

    def test_the_panel_is_found_by_its_label(self):
        fake = FakeDocker({"ol-panel-1": {"org.openlobby.role": "panel",
                                          "org.openlobby.panel.url": "http://127.0.0.1:8090/"}})
        with mock.patch.object(openlobby, "docker", fake):
            self.assertEqual(openlobby.panel_url(), "http://127.0.0.1:8090")
            self.assertEqual(openlobby.panel_url(choose=lambda found: self.fail("asked")), "http://127.0.0.1:8090")

    TWO = {"ol-panel-1": {"org.openlobby.role": "panel", "org.openlobby.panel.url": "http://127.0.0.1:8090"},
           "lab-panel-1": {"org.openlobby.role": "panel", "org.openlobby.panel.url": "http://127.0.0.1:8091/"}}

    def test_with_two_panels_it_never_guesses(self):
        with mock.patch.object(openlobby, "docker", FakeDocker(self.TWO)):
            with self.assertRaises(openlobby.Failed) as e:
                openlobby.panel_url()
            self.assertIn("ol-panel-1 at http://127.0.0.1:8090", str(e.exception))
            self.assertIn("lab-panel-1 at http://127.0.0.1:8091", str(e.exception))
            self.assertIn("run Setup and choose the panel", str(e.exception))
            self.assertEqual(openlobby.panel_url(choose=lambda found: found[1][1]), "http://127.0.0.1:8091")
            self.assertEqual(openlobby.panel_url(prefer="http://127.0.0.1:8091/"), "http://127.0.0.1:8091")

    def test_setup_asks_which_panel_at_a_terminal(self):
        out, asked = io.StringIO(), []
        with mock.patch.object(openlobby, "docker", FakeDocker(self.TWO)),                 mock.patch.object(setup, "interactive", return_value=True),                 mock.patch.object(setup, "read_choices", return_value={}),                 mock.patch("builtins.input", lambda q: asked.append(q) or "2"), mock.patch("sys.stdout", out):
            self.assertEqual(setup.find_panel(), "http://127.0.0.1:8091")
        self.assertIn("Which one should manage", out.getvalue())
        self.assertIn("ol-panel-1 (http://127.0.0.1:8090)", out.getvalue())
        self.assertIn("lab-panel-1 (http://127.0.0.1:8091)", out.getvalue())
        self.assertEqual(len(asked), 1)

    def test_setup_without_a_terminal_stops_pairing(self):
        out = io.StringIO()
        with mock.patch.object(openlobby, "docker", FakeDocker(self.TWO)),                 mock.patch.object(setup, "interactive", return_value=False),                 mock.patch.object(setup, "read_choices", return_value={}),                 mock.patch("builtins.input", lambda *_: self.fail("asked")),                 mock.patch.object(openlobby, "pair", side_effect=AssertionError("paired")),                 mock.patch("sys.stdout", out):
            self.assertIsNone(setup.find_panel())
            setup.offer_panel({"BOMB_ADMIN_TOKEN": "tok"}, ask_first=False)
        lines = [l for l in out.getvalue().splitlines() if l]
        self.assertEqual(len(lines), 2)
        self.assertIn("run Setup and choose the panel", lines[0])

    def test_setup_keeps_the_panel_it_paired_with(self):
        with mock.patch.object(openlobby, "docker", FakeDocker(self.TWO)),                 mock.patch.object(setup, "interactive", return_value=False),                 mock.patch.object(setup, "read_choices", return_value={"panel_url": "http://127.0.0.1:8091"}):
            self.assertEqual(setup.find_panel(), "http://127.0.0.1:8091")


class FirstStartTest(Temp):
    """complete_setup: network, Feega, DNS names, build, start, check, in that order."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.dir, "Bomberman Server")
        os.makedirs(self.root)
        shutil.copy(os.path.join(HERE, ".env.example"), self.root)
        setup.use_root(self.root)

    def tearDown(self):
        setup.use_root(HERE)
        super().tearDown()

    def run_setup(self, fake):
        commands = []

        def run_logged(cmd, log, **kw):
            commands.append(cmd)
            return 0, []

        out = io.StringIO()
        with mock.patch.object(openlobby, "docker", fake), \
                mock.patch.object(openlobby, "install_feega", wraps=lambda folder, adv, log=None: fake(
                    "compose", "-p", "freega", "up", "-d")) as inst, \
                mock.patch.object(setup.proglib, "run_logged", run_logged), \
                mock.patch.object(setup, "smoke", return_value=True), \
                mock.patch.object(setup, "lan_address", return_value="192.168.0.10"), \
                mock.patch("sys.stdout", out):
            setup.write_env({"BOMB_GAME_ADDR": "192.168.0.10", "BOMB_ADMIN_TOKEN": "t"})
            ok = setup.complete_setup(setup.read_env(), {})
        return ok, commands, inst, out.getvalue()

    def test_nothing_running_starts_feega_and_dns(self):
        fake = FakeDocker()
        ok, commands, inst, out = self.run_setup(fake)
        self.assertTrue(ok, out)
        self.assertEqual(inst.call_args[0][0], os.path.join(self.dir, "Freega"))
        self.assertIn("openlobby-dns-dns-1", fake.running)
        self.assertEqual(fake.names_added(), [("bomb",) + setup.HOST_NAMES,
                                              ("feega", "lib.feega.sonyfinance.co.jp")])
        self.assertIn("openlobby-http-http-1", fake.running)
        self.assertEqual(fake.hosts_added(), [("bomb", "http://bomb-web:80") + setup.HTTP_HOSTS])
        self.assertEqual(commands, [["docker", "compose", "build"], ["docker", "compose", "up", "-d"]])
        self.assertIn("set the game's DNS server to 192.168.0.10", out)

    def test_running_feega_and_dns_are_used_as_they_are(self):
        fake = FakeDocker({"other-feega": {"org.openlobby.game.key": "feega"},
                           "other-dns": {"org.openlobby.role": "dns"},
                           "other-http": {"org.openlobby.role": "http"}})
        fake.networks.add("openlobby")
        ok, commands, inst, out = self.run_setup(fake)
        self.assertTrue(ok, out)
        inst.assert_not_called()
        self.assertEqual(fake.names_added(), [("bomb",) + setup.HOST_NAMES])
        self.assertEqual(fake.hosts_added(), [("bomb", "http://bomb-web:80") + setup.HTTP_HOSTS])
        self.assertFalse([a for a in fake.calls if a[:1] == ("compose",)])


if __name__ == "__main__":
    unittest.main()
