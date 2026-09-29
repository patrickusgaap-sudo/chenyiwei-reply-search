import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import call, patch
from urllib.parse import parse_qs, urlparse

from scripts import refresh_index as refresh


@contextmanager
def api_responses(responses):
    """Exercise the actual curl/status/body boundary against a local HTTP API."""
    requests = []
    queued = list(responses)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            status, body = queued.pop(0) if queued else (500, "Unexpected request")
            if status is None:
                self.close_connection = True
                return
            raw = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        with patch.object(refresh, "API_BASE", f"http://127.0.0.1:{server.server_port}"):
            yield requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def recent_item(tid, reply, timestamp="2026-09-29 12:00:00"):
    return {
        "link": f"https://bbs.esnai.com/thread-{tid}-1-1.html",
        "title": f"Topic {tid}",
        "posts": [{"question_text": "Question", "comment_text": reply, "comment_time": timestamp}],
    }


class RefreshIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.index_path = Path(self.directory.name) / "index.html"
        self.old_record = {
            "tid": "1",
            "title": "Retained topic",
            "category": "Existing category",
            "reply": "Original reply",
            "replySortTime": "2026-09-20 10:00:00",
        }
        self.original = (
            '<!doctype html>\r\n<div class="meta">Previous metadata</div>\r\n'
            '<script id="recordsData" type="application/json">'
            + json.dumps([self.old_record])
            + '</script>\r\n<script id="categoriesData" type="application/json">'
            + json.dumps(["Existing category"])
            + "</script>\r\n"
        ).encode("utf-8")
        self.index_path.write_bytes(self.original)
        index_patch = patch.object(refresh, "INDEX_PATH", self.index_path)
        index_patch.start()
        self.addCleanup(index_patch.stop)
        # Mock this module's clock without changing subprocess/threading's clock.
        sleep_patch = patch.object(refresh, "time")
        self.sleep = sleep_patch.start().sleep
        self.addCleanup(sleep_patch.stop)

    def assert_pages(self, requests, pages):
        self.assertEqual(len(requests), len(pages))
        for request, page in zip(requests, pages):
            parsed = urlparse(request)
            self.assertEqual(parsed.path, "/api/public/recent")
            self.assertEqual(parse_qs(parsed.query), {"days": [str(refresh.MAX_DAYS)], "page": [str(page)]})

    def assert_index_unchanged(self):
        self.assertEqual(self.index_path.read_bytes(), self.original)

    def test_524_then_success_recovers_with_real_curl(self):
        payload = {"results": [recent_item("2", "Recovered reply")], "hasMore": False}
        with api_responses([(524, "<html>Origin timed out</html>"), (200, payload)]) as requests:
            result = refresh.run_curl_json("/api/public/recent", {"days": refresh.MAX_DAYS, "page": 1})
        self.assertEqual(result, payload)
        self.assert_pages(requests, [1, 1])
        self.assertEqual(self.sleep.call_args_list, [call(5)])

    def test_transient_http_statuses_retry(self):
        payload = {"results": [], "hasMore": False}
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.sleep.reset_mock()
                with api_responses([(status, "Temporary error"), (200, payload)]) as requests:
                    result = refresh.run_curl_json("/api/public/recent", {"days": refresh.MAX_DAYS, "page": 1})
                self.assertEqual(result, payload)
                self.assert_pages(requests, [1, 1])
                self.assertEqual(self.sleep.call_args_list, [call(5)])

    def test_dropped_connection_retries_and_recovers(self):
        payload = {"results": [], "hasMore": False}
        with api_responses([(None, ""), (200, payload)]) as requests:
            result = refresh.run_curl_json("/api/public/recent", {"days": refresh.MAX_DAYS, "page": 1})
        self.assertEqual(result, payload)
        self.assert_pages(requests, [1, 1])
        self.assertEqual(self.sleep.call_args_list, [call(5)])

    def test_exhausted_524_retries_preserve_index(self):
        with api_responses([(524, "Origin timed out")] * 4) as requests:
            with self.assertRaises(RuntimeError):
                refresh.update_index()
        self.assert_pages(requests, [1] * 4)
        self.assertEqual(self.sleep.call_args_list, [call(5), call(10), call(20)])
        self.assert_index_unchanged()

    def test_page_two_failure_does_not_save_partial_refresh(self):
        page_one = {"results": [recent_item("2", "Must not be saved")], "hasMore": True}
        with api_responses([(200, page_one)] + [(524, "Origin timed out")] * 4) as requests:
            with self.assertRaises(RuntimeError):
                refresh.update_index()
        self.assert_pages(requests, [1, 2, 2, 2, 2])
        self.assertEqual(self.sleep.call_args_list, [call(5), call(10), call(20)])
        self.assert_index_unchanged()

    def test_permanent_404_fails_without_retry_or_write(self):
        with api_responses([(404, "Not found")]) as requests:
            with self.assertRaises(RuntimeError):
                refresh.update_index()
        self.assert_pages(requests, [1])
        self.sleep.assert_not_called()
        self.assert_index_unchanged()

    def test_malformed_success_payloads_fail_without_writing(self):
        for body in (
            "<html>Not JSON</html>",
            "[]",
            {"hasMore": False},
            {"results": None, "hasMore": False},
            {"results": {}, "hasMore": False},
            {"results": []},
            {"results": [], "hasMore": "false"},
            {"results": [], "hasMore": 0},
        ):
            with self.subTest(body=body):
                self.sleep.reset_mock()
                with api_responses([(200, body)]) as requests:
                    with self.assertRaises(RuntimeError):
                        refresh.update_index()
                self.assert_pages(requests, [1])
                self.sleep.assert_not_called()
                self.assert_index_unchanged()

    def test_successful_multipage_refresh_retains_history_and_category(self):
        page_one = {"results": [recent_item("2", "New reply")], "hasMore": True}
        page_two = {"results": [recent_item("3", "Another reply", "2026-09-28 12:00:00")], "hasMore": False}
        with api_responses([(200, page_one), (200, page_two)]) as requests:
            stats = refresh.update_index()
        self.assert_pages(requests, [1, 2])
        self.sleep.assert_not_called()
        html = self.index_path.read_text(encoding="utf-8")
        records = json.loads(refresh.extract_script_json(html, "recordsData"))
        self.assertEqual([row["tid"] for row in records], ["2", "3", "1"])
        self.assertEqual(records[-1], self.old_record)
        self.assertEqual([row["reply"] for row in records[:2]], ["New reply", "Another reply"])
        self.assertEqual(json.loads(refresh.extract_script_json(html, "categoriesData")), ["Existing category", "最近更新"])
        self.assertEqual(stats.total_records, 3)
        self.assertEqual(stats.added_records, 2)
        self.assertEqual(stats.updated_records, 2)
        self.assertIn("3 条主题", html)


if __name__ == "__main__":
    unittest.main()
