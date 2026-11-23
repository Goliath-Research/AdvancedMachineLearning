"""Security spec for the public following page.

A visitor chooses the screen name in ``/following/<name>``. One friends-id
page can contain 5000 ids. The page must show at most one bulk user-lookup
page of those accounts (100), must resolve them with ``lookup_users`` rather
than one ``get_user`` per id, and must answer within a few seconds even when
a Twitter call never returns. The documented server handles requests on four
worker threads, so a request that waits on Twitter occupies one of those
threads for the whole wait.

These tests fail while that fan-out is unbounded.
"""

import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# One users/lookup request accepts 100 ids. That is the cap the page must keep.
MAX_ACCOUNTS = 100
MAX_IDS_PER_LOOKUP = 100
# A stalled Twitter call must not hold the request longer than this.
REQUEST_DEADLINE_SECONDS = 3.0
# gunicorn --threads 4 uses a pool of this size (gthread.ThreadWorker).
WORKER_THREADS = 4


def _placeholder_keyfile():
    handle = tempfile.NamedTemporaryFile(
        mode="w", prefix="twitter-keys-", suffix=".csv", delete=False
    )
    # Names only. These are not real credentials.
    handle.write(
        "placeholder_consumer_key, placeholder_consumer_secret, "
        "placeholder_access_token, placeholder_access_token_secret\n"
    )
    handle.close()
    return handle.name


_KEYFILE = _placeholder_keyfile()
_ARGV = sys.argv
sys.argv = ["gunicorn", "server:app", _KEYFILE]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import server
    import tweetie
finally:
    sys.argv = _ARGV


class FakeUser(object):
    """Fields ``fetch_following`` reads off a tweepy user model."""

    def __init__(self, user_id):
        self.id = user_id
        self.name = "User {0}".format(user_id)
        self.screen_name = "user{0}".format(user_id)
        self.followers_count = int(user_id)
        self.created_at = datetime(2010, 1, 1)
        self.profile_image_url_https = "https://example.test/{0}.png".format(user_id)


class FakeAPI(object):
    """Stand-in for tweepy.API at the boundary ``fetch_following`` calls.

    ``friends_ids`` returns one page of ids, which is what tweepy's
    ``IDModel.parse`` returns from a single ``/friends/ids.json`` response.
    ``get_user`` is one ``/users/show.json`` call. ``lookup_users`` is one
    ``/users/lookup.json`` call. Neither method caps the list; the page under
    test has to do that.
    """

    def __init__(self, ids, stall_names=None, stall_event=None):
        self.ids = list(ids)
        self.stall_names = set(stall_names or [])
        self.stall_event = stall_event or threading.Event()
        self.calls = []
        self.friends_names = []
        self._lock = threading.Lock()

    def friends_ids(self, name):
        with self._lock:
            self.friends_names.append(name)
            self.calls.append(("friends_ids", name))
        if name in self.stall_names:
            self.stall_event.wait(30)
        return list(self.ids)

    def get_user(self, user_id):
        with self._lock:
            self.calls.append(("get_user", user_id))
        return FakeUser(user_id)

    def lookup_users(self, user_ids=None, screen_names=None, **kwargs):
        batch = list(user_ids or [])
        with self._lock:
            self.calls.append(("lookup_users", batch))
        return [FakeUser(user_id) for user_id in batch]


def _rows(html):
    return html.count("<tr>")


def _lookup_batches(fake):
    with fake._lock:
        calls = list(fake.calls)
    batches = []
    per_id = 0
    for kind, payload in calls:
        if kind == "get_user":
            per_id += 1
            batches.append([payload])
        elif kind == "lookup_users":
            batches.append(list(payload))
    return per_id, batches


def _issue(path):
    client = server.app.test_client()
    try:
        response = client.get(path)
    except Exception as exc:
        return ("error", type(exc).__name__, str(exc))
    return ("response", response.status_code, response.get_data(as_text=True))


class FollowingLookupBoundsTest(unittest.TestCase):
    def setUp(self):
        self._previous_api = server.api

    def tearDown(self):
        server.api = self._previous_api

    def _bind(self, fake):
        server.api = fake
        return fake

    def test_fetch_following_resolves_at_most_one_bulk_page(self):
        friend_ids = list(range(5000))
        fake = FakeAPI(friend_ids)
        accounts = tweetie.fetch_following(fake, "busy_account")
        per_id, batches = _lookup_batches(fake)

        self.assertEqual(fake.friends_names, ["busy_account"])
        self.assertEqual(
            per_id,
            0,
            "fetch_following called get_user once per friend id ({0} calls)".format(per_id),
        )
        self.assertTrue(batches, "expected one bulk lookup_users call")
        for batch in batches:
            self.assertLessEqual(len(batch), MAX_IDS_PER_LOOKUP)
        resolved = [user_id for batch in batches for user_id in batch]
        self.assertEqual(len(resolved), min(len(friend_ids), MAX_ACCOUNTS))
        self.assertEqual(len(accounts), min(len(friend_ids), MAX_ACCOUNTS))
        self.assertEqual(
            [account["screen_name"] for account in accounts],
            ["user{0}".format(user_id) for user_id in resolved],
        )

    def test_following_route_resolves_at_most_one_bulk_page(self):
        friend_ids = list(range(5000))
        fake = self._bind(FakeAPI(friend_ids))
        kind, status, body = _issue("/following/busy_account")

        self.assertEqual(kind, "response")
        self.assertEqual(status, 200)
        per_id, batches = _lookup_batches(fake)
        self.assertEqual(fake.friends_names, ["busy_account"])
        self.assertEqual(
            per_id,
            0,
            "GET /following/<name> called get_user once per friend id ({0} calls)".format(
                per_id
            ),
        )
        self.assertTrue(batches, "expected one bulk lookup_users call")
        for batch in batches:
            self.assertLessEqual(len(batch), MAX_IDS_PER_LOOKUP)
        resolved = [user_id for batch in batches for user_id in batch]
        self.assertEqual(len(resolved), min(len(friend_ids), MAX_ACCOUNTS))
        self.assertEqual(_rows(body), min(len(friend_ids), MAX_ACCOUNTS))
        for user_id in resolved:
            self.assertIn("user{0}".format(user_id), body)

    def test_stalled_lookup_returns_before_deadline(self):
        stall = threading.Event()
        fake = self._bind(
            FakeAPI([], stall_names={"stalled_account"}, stall_event=stall)
        )
        outcome = {}

        def run():
            outcome["result"] = _issue("/following/stalled_account")

        worker = threading.Thread(target=run)
        worker.daemon = True
        try:
            worker.start()
            worker.join(REQUEST_DEADLINE_SECONDS + 0.5)
            self.assertFalse(
                worker.is_alive(),
                "the following page was still waiting on Twitter after "
                "{0} seconds".format(REQUEST_DEADLINE_SECONDS),
            )
            self.assertIn("result", outcome)
        finally:
            stall.set()
            worker.join(2)

        with fake._lock:
            kinds = [kind for kind, _payload in fake.calls]
        self.assertNotIn("get_user", kinds)

    def test_four_workers_still_serve_another_visitor(self):
        stall = threading.Event()
        stall_names = {"stall{0}".format(i) for i in range(WORKER_THREADS)}
        fake = self._bind(
            FakeAPI([1, 2, 3], stall_names=stall_names, stall_event=stall)
        )
        pool = ThreadPoolExecutor(max_workers=WORKER_THREADS)
        try:
            for name in sorted(stall_names):
                pool.submit(_issue, "/following/{0}".format(name))
            entered = threading.Event()

            def _watch():
                while True:
                    with fake._lock:
                        seen = set(fake.friends_names)
                    if stall_names <= seen:
                        entered.set()
                        return
                    if stall.is_set():
                        return

            watcher = threading.Thread(target=_watch)
            watcher.daemon = True
            watcher.start()
            self.assertTrue(
                entered.wait(2),
                "the four worker requests never reached friends_ids",
            )
            quick = pool.submit(_issue, "/following/quick_visitor")
            quick_done = quick.done()
            if not quick_done:
                try:
                    quick.result(timeout=REQUEST_DEADLINE_SECONDS + 2)
                    quick_done = True
                except Exception:
                    quick_done = quick.done()
            self.assertTrue(
                quick_done,
                "a fifth visitor got no response while four following "
                "requests occupied the worker threads",
            )
            kind, status, body = quick.result(timeout=1)
            self.assertEqual(kind, "response")
            self.assertEqual(status, 200)
            self.assertIn("quick_visitor", body)
            per_id, batches = _lookup_batches(fake)
            quick_batches = [
                batch
                for batch in batches
                if batch and all(user_id in (1, 2, 3) for user_id in batch)
            ]
            resolved = [user_id for batch in quick_batches for user_id in batch]
            self.assertEqual(per_id, 0)
            self.assertEqual(sorted(resolved), [1, 2, 3])
        finally:
            stall.set()
            pool.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    unittest.main()
