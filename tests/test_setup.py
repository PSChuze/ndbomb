"""setup.py's settings, addresses and file handling (no Docker)."""
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

import setup

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Folder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bomb-setup-")
        shutil.copy(os.path.join(HERE, ".env.example"), self.dir)
        setup.use_root(self.dir)

    def tearDown(self):
        setup.use_root(HERE)
        shutil.rmtree(self.dir, ignore_errors=True)


class EnvTest(Folder):
    def test_write_env_fills_the_template_and_keeps_comments(self):
        setup.write_env({"BOMB_GAME_ADDR": "192.0.2.10", "NEW_KEY": "x"})
        with open(setup.ENV, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("BOMB_GAME_ADDR=192.0.2.10\n", text)
        self.assertIn("# This server's address", text)
        self.assertTrue(text.endswith("NEW_KEY=x\n"))
        self.assertEqual(setup.read_env()["BOMB_RELAY_PORT"], "10081")

    def test_write_env_changes_an_existing_file(self):
        setup.write_env({"BOMB_GAME_ADDR": "192.0.2.10"})
        setup.write_env({"BOMB_GAME_ADDR": "192.0.2.11"})
        self.assertEqual(setup.read_env()["BOMB_GAME_ADDR"], "192.0.2.11")

    def test_first_settings_make_a_token_once(self):
        with mock.patch.object(setup, "lan_address", return_value="192.168.0.10"):
            first = setup.first_settings("home", "192.168.0.10", "", {}, False)
            again = setup.first_settings("home", "192.168.0.10", "", {"BOMB_ADMIN_TOKEN": "t"}, False)
        self.assertEqual(len(first["BOMB_ADMIN_TOKEN"]), 32)
        self.assertNotIn("BOMB_ADMIN_TOKEN", again)
        self.assertEqual(first["BOMB_ADVERTISE_MAP"], "")

    def test_news_file(self):
        setup.write_news({}, "My Server", "Hello")
        with open(os.path.join(self.dir, "state", "data", "news.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), [{"title": "Welcome to My Server", "body": "Hello"}])


class AddressTest(unittest.TestCase):
    def test_advertise_map_for_internet_play(self):
        self.assertEqual(setup.advertise_map("internet", "203.0.113.5", "192.168.0.10", False),
                         "192.168.0.0/16=192.168.0.10,0.0.0.0/0=203.0.113.5")
        self.assertEqual(setup.advertise_map("internet", "203.0.113.5", "10.1.2.3", False),
                         "10.0.0.0/8=10.1.2.3,0.0.0.0/0=203.0.113.5")

    def test_no_map_at_home_or_on_docker_desktop(self):
        self.assertEqual(setup.advertise_map("home", "192.168.0.10", "192.168.0.10", False), "")
        self.assertEqual(setup.advertise_map("internet", "203.0.113.5", "192.168.0.10", True), "")

    def test_the_game_wants_a_dotted_ip(self):
        self.assertTrue(setup.is_ip("203.0.113.5"))
        for bad in ("bomb.example.net", "203.0.113", "300.1.1.1", ""):
            self.assertFalse(setup.is_ip(bad), bad)

    def test_ports(self):
        self.assertEqual(setup.port_ranges([80, 9901, 10020, 10081]), [(80, 80), (9901, 9901), (10020, 10020),
                                                                       (10081, 10081)])
        self.assertIn((10020, "UDP"), setup.ports_to_open(False))
        self.assertIn((14020, "TCP"), setup.ports_to_open(True))
        self.assertNotIn((14020, "TCP"), setup.ports_to_open(False))


class UnpackTest(Folder):
    def test_unpack_keeps_the_hosts_files(self):
        with open(os.path.join(self.dir, ".env"), "w") as f:
            f.write("BOMB_GAME_ADDR=192.0.2.1\n")
        archive = os.path.join(self.dir, "s.zip")
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("ndbomb/compose.yml", "name: bomb\n")
            z.writestr("ndbomb/.env", "overwritten\n")
            z.writestr("ndbomb/bomb/game.py", "# game\n")
        written = setup.unpack_server(archive, self.dir, "v9")
        self.assertEqual(sorted(written), ["bomb/game.py", "compose.yml"])
        with open(os.path.join(self.dir, ".env")) as f:
            self.assertEqual(f.read(), "BOMB_GAME_ADDR=192.0.2.1\n")
        self.assertEqual(setup.installed_version(), "v9")


class DryRunTest(Folder):
    def test_dry_run_writes_nothing(self):
        answers = iter(["2", "203.0.113.5", "Test Server", ""])
        out = io.StringIO()
        with mock.patch("builtins.input", lambda *_: next(answers)), \
                mock.patch.object(setup, "public_address", return_value="203.0.113.5"), \
                mock.patch.object(setup, "lan_address", return_value="192.168.0.10"), \
                mock.patch("sys.stdout", out):
            setup.first_run(dry_run=True)
        self.assertFalse(os.path.exists(setup.ENV))
        self.assertIn("BOMB_GAME_ADDR=203.0.113.5", out.getvalue())


if __name__ == "__main__":
    unittest.main()
