"""setup.py --update --yes, with Docker and GitHub replaced by fakes."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

import setup

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_COMMIT = "c" * 40


def release_zip(path, tag="v1.1.0", game="# new game\n"):
    """A source zip the way GitHub serves a tag: one top folder, the commit as the comment."""
    with zipfile.ZipFile(path, "w") as z:
        top = "ndbomb-%s/" % tag.lstrip("v")
        z.writestr(top + "compose.yml", "name: bomb\n# new\n")
        z.writestr(top + "bomb/game.py", game)
        z.writestr(top + "setup.py", "# new setup\n")
        z.writestr(top + ".env.example", "BOMB_GAME_ADDR=\n")
        z.comment = NEW_COMMIT.encode()


class FakeCompose:
    """Stands in for proglib.run_logged: records each docker command and where it ran."""

    def __init__(self, build_exit=0):
        self.build_exit = build_exit
        self.calls = []

    def __call__(self, cmd, log, cwd=None, progress=None, **kw):
        self.calls.append((cmd, cwd))
        if cmd[:3] == ["docker", "compose", "build"]:
            return self.build_exit, ["#5 ERROR: failed to solve"] if self.build_exit else []
        return 0, []


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bomb-update-")
        self.root = os.path.join(self.dir, "Bomberman Server")
        os.makedirs(os.path.join(self.root, "bomb"))
        os.makedirs(os.path.join(self.root, "state", "data"))
        files = {"compose.yml": "name: bomb\n# old\n", "bomb/game.py": "# old game\n", "bomb/gone.py": "# old\n",
                 ".env": "BOMB_GAME_ADDR=192.0.2.10\nBOMB_ADMIN_TOKEN=t\n",
                 "state/data/players.json": "{\"k\": 1}\n"}
        for rel, text in files.items():
            with open(os.path.join(self.root, rel), "w", encoding="utf-8") as f:
                f.write(text)
        with open(os.path.join(self.root, ".setup-files"), "w") as f:
            f.write("bomb/game.py\nbomb/gone.py\ncompose.yml\n")
        with open(os.path.join(self.root, ".setup-version"), "w") as f:
            f.write("v1.0.0\n")
        setup.use_root(self.root)
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)
        setup.use_root(HERE)
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return f.read()

    def run_update(self, fake, running=("game", "ticket", "web"), tag="v1.1.0", smoke=True, downloaded=None):
        downloads = []

        def download(url, path, timeout=60):
            downloads.append(url)
            release_zip(path, tag)
            if downloaded:
                downloaded(path)

        out = io.StringIO()
        closed = io.StringIO()
        closed.close()          # like the panel's job: stdin is closed, so any question would fail
        with mock.patch.object(setup, "latest_release",
                               return_value=(tag, "https://github.com/PSChuze/ndbomb/archive/refs/tags/%s.zip" % tag)), \
                mock.patch.object(setup, "download", download), \
                mock.patch.object(setup, "wait_for_docker", return_value=True), \
                mock.patch.object(setup, "running_services", return_value=list(running)), \
                mock.patch.object(setup.proglib, "run_logged", fake), \
                mock.patch.object(setup, "smoke", return_value=smoke), \
                mock.patch("sys.stdin", closed), mock.patch("sys.stdout", out):
            code = setup.update_now(yes=True)
        lines = [l for l in out.getvalue().splitlines() if l.strip()]
        return code, lines, downloads

    def test_already_current(self):
        fake = FakeCompose()
        code, lines, downloads = self.run_update(fake, tag="v1.0.0")
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "Already up to date (v1.0.0).")
        self.assertEqual((fake.calls, downloads), ([], []))
        self.assertEqual(self.read("bomb/game.py"), "# old game\n")

    def test_available_swaps_builds_and_restarts_only_what_ran(self):
        fake = FakeCompose()
        seen_during_build = []

        def compose(cmd, log, cwd=None, **kw):
            if cmd[:3] == ["docker", "compose", "build"]:
                # the build runs on the new files while the old ones are still in place
                seen_during_build.append((self.read("bomb/game.py"),
                                          os.path.exists(os.path.join(cwd, ".env"))))
            return fake(cmd, log, cwd=cwd, **kw)

        code, lines, _ = self.run_update(compose, running=("game", "web"))
        self.assertEqual(code, 0, lines)
        self.assertEqual(seen_during_build, [("# old game\n", True)])
        build, up = fake.calls
        self.assertNotEqual(build[1], self.root)
        self.assertEqual(up, (["docker", "compose", "up", "-d", "--no-build", "game", "web"], self.root))
        self.assertEqual(self.read("bomb/game.py"), "# new game\n")
        self.assertEqual(self.read("compose.yml"), "name: bomb\n# new\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "bomb", "gone.py")))
        self.assertEqual(self.read(".env"), "BOMB_GAME_ADDR=192.0.2.10\nBOMB_ADMIN_TOKEN=t\n")
        self.assertEqual(self.read("state/data/players.json"), "{\"k\": 1}\n")
        self.assertEqual(setup.installed_version(), "v1.1.0")
        build_info = json.loads(self.read("bomb/build.json"))
        self.assertEqual((build_info["commit"], build_info["tag"]), (NEW_COMMIT, "v1.1.0"))
        self.assertFalse([n for n in os.listdir(self.root) if n.startswith(".update-")])
        self.assertEqual(lines[-1], "Updated to v1.1.0: restarted game, web and the check passed.")

    def test_a_stopped_server_stays_stopped(self):
        fake = FakeCompose()
        code, lines, _ = self.run_update(fake, running=())
        self.assertEqual(code, 0, lines)
        self.assertEqual([c[0][:3] for c in fake.calls], [["docker", "compose", "build"]])
        self.assertEqual(lines[-1], "Updated to v1.1.0: the server was stopped and stays stopped.")

    def test_build_failure_keeps_the_old_code(self):
        fake = FakeCompose(build_exit=1)
        code, lines, _ = self.run_update(fake)
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], "Building v1.1.0 failed; the server still runs the old version.")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.read("bomb/game.py"), "# old game\n")
        self.assertEqual(setup.installed_version(), "v1.0.0")
        self.assertFalse(os.path.exists(os.path.join(self.root, "bomb", "build.json")))
        self.assertFalse([n for n in os.listdir(self.root) if n.startswith(".update-")])

    def test_a_failed_check_is_the_last_line(self):
        code, lines, _ = self.run_update(FakeCompose(), smoke=False)
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], "The new version runs but its check failed; see the lines above.")

    def test_a_bad_download_changes_nothing(self):
        def broken(path):
            with open(path, "wb") as f:
                f.write(b"not a zip")
        fake = FakeCompose()
        code, lines, _ = self.run_update(fake, downloaded=broken)
        self.assertEqual(code, 1)
        self.assertTrue(lines[-1].startswith("The download of v1.1.0 failed"), lines[-1])
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.read("bomb/game.py"), "# old game\n")

    def test_github_unreachable(self):
        out = io.StringIO()
        with mock.patch.object(setup, "latest_release", return_value=None), \
                mock.patch.object(setup, "wait_for_docker", return_value=True), mock.patch("sys.stdout", out):
            self.assertEqual(setup.update_now(yes=True), 1)
        self.assertEqual(out.getvalue().splitlines()[-1],
                         "Could not reach GitHub to find the newest release, so nothing was changed.")

    def test_docker_not_running(self):
        out = io.StringIO()
        with mock.patch.object(setup, "wait_for_docker", return_value=False), mock.patch("sys.stdout", out):
            self.assertEqual(setup.update_now(yes=True), 1)
        self.assertEqual(out.getvalue().splitlines()[-1], "Docker is not running, so nothing was changed.")

    def test_safe_to_run_again(self):
        fake = FakeCompose()
        self.assertEqual(self.run_update(fake)[0], 0)
        code, lines, downloads = self.run_update(fake)
        self.assertEqual((code, lines[-1], downloads), (0, "Already up to date (v1.1.0).", []))


class CommandLineTest(unittest.TestCase):
    def test_runs_from_the_server_folder_with_stdin_closed(self):
        """The folder setup unpacks holds setup.py and hostsetup/, so plain Python runs it."""
        with tempfile.TemporaryDirectory(prefix="bomb-cli-") as d:
            shutil.copy(os.path.join(HERE, "setup.py"), d)
            shutil.copytree(os.path.join(HERE, "hostsetup"), os.path.join(d, "hostsetup"),
                            ignore=shutil.ignore_patterns("__pycache__"))
            r = subprocess.run([sys.executable, "setup.py", "--update", "--yes"], cwd=d, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertTrue(r.stdout.strip().splitlines()[-1].endswith(
            "holds no Bomberman server, so there is nothing to update."), r.stdout)


class HeartbeatTest(unittest.TestCase):
    def test_a_long_step_without_a_terminal_still_prints(self):
        now = [0.0]
        out = io.StringIO()
        progress = setup.proglib.Progress(out=out, clock=lambda: now[0])
        progress.begin("Building v1.1.0", "a few minutes")
        for t in range(0, 400, 1):
            now[0] = float(t)
            progress.tick()
        progress.end(True)
        lines = out.getvalue().splitlines()
        self.assertEqual(len([l for l in lines if "so far" in l]), 3)      # at 2, 4 and 6 minutes
        self.assertTrue(lines[-1].startswith("  Done: Building v1.1.0"))


if __name__ == "__main__":
    unittest.main()
