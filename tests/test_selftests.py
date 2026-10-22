"""The server's own end-to-end self-tests (scripted players, no console)."""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SelfTests(unittest.TestCase):
    def run_module(self, name):
        r = subprocess.run([sys.executable, "-m", name], cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        fails = [l for l in r.stdout.splitlines() if l.startswith("FAIL")]
        self.assertEqual((r.returncode, fails), (0, []), r.stdout[-3000:] + r.stderr[-3000:])

    def test_rooms(self):
        self.run_module("bomb.selftest_rooms")

    def test_adminport(self):
        self.run_module("bomb.selftest_adminport")


if __name__ == "__main__":
    unittest.main()
