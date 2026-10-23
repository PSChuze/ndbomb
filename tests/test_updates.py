"""bomb/updates.py: the admin panel's update notice, with GitHub replaced by a fake."""
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from bomb import updates

RUNNING = "a" * 40
NEWER = "b" * 40
REPO = "PSChuze/ndbomb"


def commit(sha, subject, date="2025-03-01T10:00:00Z", parents=1):
    return {"sha": sha, "commit": {"message": subject + "\n\nbody", "committer": {"date": date}},
            "parents": [{"sha": "p%d" % i} for i in range(parents)]}


class FakeGitHub:
    """Answers the three API calls the check makes from a release tag and its commits."""

    def __init__(self, tag="v1.1.0", head=NEWER, between=(), offline=False, status=200):
        self.tag, self.head, self.between, self.offline, self.status = tag, head, list(between), offline, status
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if self.offline:
            raise OSError("network is unreachable")
        path = url[len(updates.API):]
        if path == "/repos/%s/releases/latest" % REPO:
            return (self.status, {"tag_name": self.tag}) if self.tag else (404, {"message": "Not Found"})
        if path == "/repos/%s/commits/%s" % (REPO, self.tag):
            return 200, commit(self.head, "Release", "2025-03-02T09:00:00+02:00")
        if path.startswith("/repos/%s/compare/" % REPO):
            return 200, {"ahead_by": len(self.between), "commits": list(self.between)}
        return 404, None


def check(gh, revision=RUNNING, repo=REPO, built_at="2025-01-01T00:00:00Z"):
    return updates.UpdateCheck(repo, revision, built_at, fetch=gh).get(wait=5)


def assert_unknown_shape(test, out, why, running=None):
    """The panel's shape for an unknown answer: objects and numbers, never a bare null."""
    test.assertEqual(out["update"], "unknown")
    test.assertIn(why, out["why"])
    test.assertEqual(out["latest"], {"commit": None, "date": None})
    test.assertEqual(out["behind"], 0)
    test.assertEqual(out["changes"], [])
    test.assertEqual(out["running"], running or {"commit": RUNNING, "built_at": "2025-01-01T00:00:00Z"})


class UpdateObjectTest(unittest.TestCase):
    def test_current(self):
        gh = FakeGitHub(head=RUNNING)
        out = check(gh)
        self.assertEqual(out["update"], "current")
        self.assertEqual(out["behind"], 0)
        self.assertIsNone(out["why"])
        self.assertEqual(out["running"], {"commit": RUNNING, "built_at": "2025-01-01T00:00:00Z"})
        self.assertEqual(out["latest"], {"commit": RUNNING, "date": "2025-03-02T07:00:00Z"})
        self.assertEqual(out["how"], "Run Bomberman Server Setup and choose Update.")
        self.assertFalse([u for u in gh.urls if "/compare/" in u])

    def test_available_lists_the_changes_newest_first_without_merges(self):
        between = [commit("c1" * 20, "relay: first", "2025-02-01T00:00:00Z"),
                   commit("c2" * 20, "Merge branch x", "2025-02-02T00:00:00Z", parents=2),
                   commit(NEWER, "game: newest", "2025-02-03T00:00:00Z")]
        out = check(FakeGitHub(between=between))
        self.assertEqual(out["update"], "available")
        self.assertEqual(out["behind"], 3)
        self.assertEqual(out["latest"]["commit"], NEWER)
        self.assertEqual(out["changes"], [
            {"commit": NEWER, "date": "2025-02-03T00:00:00Z", "subject": "game: newest"},
            {"commit": "c1" * 20, "date": "2025-02-01T00:00:00Z", "subject": "relay: first"}])

    def test_changes_are_capped(self):
        between = [commit("%040x" % i, "change %d" % i) for i in range(80)]
        out = check(FakeGitHub(between=between))
        self.assertEqual(len(out["changes"]), updates.MAX_CHANGES)
        self.assertEqual(out["changes"][0]["subject"], "change 79")

    def test_offline_is_unknown_with_a_reason(self):
        assert_unknown_shape(self, check(FakeGitHub(offline=True)), "GitHub did not answer")

    def test_other_unknowns(self):
        assert_unknown_shape(self, check(FakeGitHub(tag="")), "no release")
        assert_unknown_shape(self, check(FakeGitHub(status=403)), "too many requests")
        assert_unknown_shape(self, check(FakeGitHub(), repo="off"), "off")
        assert_unknown_shape(self, check(FakeGitHub(), repo="nonsense"), "owner/name")
        assert_unknown_shape(self, check(FakeGitHub(), revision="", built_at=""), "does not know its commit",
                             running={"commit": None, "built_at": None})

    def test_unexpected_answers_and_a_commit_not_in_the_repo(self):
        def answers(compare_status=200, head_status=200):
            def fetch(url):
                path = url[len(updates.API):]
                if path.endswith("/releases/latest"):
                    return 200, {"tag_name": "v1.1.0"}
                if "/commits/" in path:
                    return head_status, commit(NEWER, "Release") if head_status == 200 else None
                return compare_status, {"ahead_by": 0, "commits": []} if compare_status == 200 else None
            return fetch
        assert_unknown_shape(self, check(answers(head_status=500)), "unexpected answer")
        assert_unknown_shape(self, check(answers(compare_status=404)), "not in")
        assert_unknown_shape(self, check(answers(compare_status=500)), "unexpected answer")

    def test_a_check_that_breaks_is_unknown(self):
        def broken(url):
            raise RuntimeError("bug")
        with self.assertLogs("bomb.updates", "ERROR"):
            assert_unknown_shape(self, check(broken), "The check failed")

    def test_answer_is_cached(self):
        now = [0.0]
        gh = FakeGitHub(head=RUNNING)
        c = updates.UpdateCheck(REPO, RUNNING, fetch=gh, clock=lambda: now[0])
        c.get(wait=5)
        calls = len(gh.urls)
        now[0] = updates.TTL - 1
        c.get(wait=5)
        self.assertEqual(len(gh.urls), calls)
        now[0] = updates.TTL + 1
        c.get(wait=5)
        if c.thread:                    # a stale answer is served while it refreshes
            c.thread.join(5)
        self.assertGreater(len(gh.urls), calls)

    def test_a_slow_check_does_not_hold_status_json(self):
        gate = threading.Event()

        def slow(url):
            gate.wait(5)
            raise OSError("late")
        out = updates.UpdateCheck(REPO, RUNNING, fetch=slow).get(wait=0.05)
        gate.set()
        assert_unknown_shape(self, out, "Still asking", running={"commit": RUNNING, "built_at": None})


class BuildInfoTest(unittest.TestCase):
    def test_build_file_and_env(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "build.json")
            with open(path, "w") as f:
                json.dump({"commit": RUNNING.upper(), "built_at": "2025-01-01T00:00:00Z"}, f)
            with mock.patch.dict(os.environ, {"BOMB_REVISION": "", "BOMB_BUILT_AT": ""}):
                self.assertEqual(updates.build_info(path), (RUNNING, "2025-01-01T00:00:00Z"))
            with mock.patch.dict(os.environ, {"BOMB_REVISION": NEWER, "BOMB_BUILT_AT": "x"}):
                self.assertEqual(updates.build_info(path), (NEWER, "x"))
            with mock.patch.dict(os.environ, {"BOMB_REVISION": "", "BOMB_BUILT_AT": ""}):
                self.assertEqual(updates.build_info(os.path.join(d, "missing.json")), ("", ""))


if __name__ == "__main__":
    unittest.main()
