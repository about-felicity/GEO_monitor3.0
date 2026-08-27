import concurrent.futures
import threading
import time
import unittest

from monitor_core.http_cache import JSONResponseCache


class JSONResponseCacheTests(unittest.TestCase):
    def test_concurrent_miss_is_built_once(self):
        cache = JSONResponseCache()
        calls = 0
        call_lock = threading.Lock()

        def build():
            nonlocal calls
            with call_lock:
                calls += 1
            time.sleep(0.04)
            return {"value": "同一份数据"}

        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
            results = list(executor.map(
                lambda _: cache.get_or_build("same-filter", build, ttl=5, stale_ttl=30),
                range(64),
            ))

        self.assertEqual(calls, 1)
        self.assertEqual(len({item[0].body for item in results}), 1)
        self.assertEqual(len({item[0].etag for item in results}), 1)
        self.assertTrue(all(item[0].gzip_body for item in results))

    def test_stale_response_is_served_while_one_refresh_runs(self):
        cache = JSONResponseCache()
        refresh_finished = threading.Event()
        calls = 0

        def build():
            nonlocal calls
            calls += 1
            if calls > 1:
                time.sleep(0.04)
                refresh_finished.set()
            return {"generation": calls}

        first, status = cache.get_or_build("history", build, ttl=0.1, stale_ttl=2)
        self.assertEqual(status, "MISS")
        time.sleep(0.12)
        stale, status = cache.get_or_build("history", build, ttl=0.1, stale_ttl=2)
        self.assertEqual(status, "STALE")
        self.assertEqual(stale.body, first.body)
        self.assertTrue(refresh_finished.wait(1))
        deadline = time.monotonic() + 1
        refreshed = stale
        while refreshed.body == first.body and time.monotonic() < deadline:
            time.sleep(0.01)
            refreshed, _ = cache.get_or_build("history", build, ttl=1, stale_ttl=2)
        self.assertNotEqual(refreshed.body, first.body)
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
