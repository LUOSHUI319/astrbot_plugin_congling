"""空间客户端端到端：起一个本地假空间服务，真实发 HTTP。

验的是"请求有没有按空间的规矩发出去"：URL、g_tk、hostuin、cookie 头、
以及返回被正确解析。不碰真网络。
"""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from astrbot_plugin_congling.qqext import qzone_client
from astrbot_plugin_congling.qqext.qzone_client import QzoneAuth, QzoneClient, gtk_of, hash33


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.feed_payload: dict = {"code": 0, "data": {"data": []}}
        self.publish_payload: dict = {"code": 0, "subcode": 0}


def _make_handler(recorder: _Recorder):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静音
            return

        def _record(self, method: str, body: str = "") -> None:
            parsed = urlparse(self.path)
            recorder.requests.append(
                {
                    "method": method,
                    "path": parsed.path,
                    "query": {k: v[0] for k, v in parse_qs(parsed.query).items()},
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
            )

        def _send(self, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._record("GET")
            self._send(recorder.feed_payload)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
            self._record("POST", body)
            self._send(recorder.publish_payload)

    return Handler


class TestQzoneClientHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recorder = _Recorder()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(cls.recorder))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls._old_feed = qzone_client.FEED_URL
        cls._old_publish = qzone_client.PUBLISH_URL
        cls._old_user = qzone_client.USER_FEEDS_URL
        qzone_client.FEED_URL = f"http://127.0.0.1:{cls.port}/feeds"
        qzone_client.PUBLISH_URL = f"http://127.0.0.1:{cls.port}/publish"
        qzone_client.USER_FEEDS_URL = f"http://127.0.0.1:{cls.port}/msglist"

    @classmethod
    def tearDownClass(cls):
        qzone_client.FEED_URL = cls._old_feed
        qzone_client.PUBLISH_URL = cls._old_publish
        qzone_client.USER_FEEDS_URL = cls._old_user
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.recorder.requests.clear()
        self.recorder.feed_payload = {"code": 0, "data": {"data": []}}
        self.recorder.publish_payload = {"code": 0, "subcode": 0}
        self.auth = QzoneAuth({"uin": "o123456", "skey": "abc", "p_skey": "def"})
        self.client = QzoneClient(self.auth, timeout=5.0)

    def test_feed_request_shape(self):
        items = asyncio.run(self.client.feed(page=2, limit=5))
        self.assertEqual(items, [])

        req = self.recorder.requests[0]
        self.assertEqual(req["method"], "GET")
        self.assertTrue(req["path"].endswith("/feeds"))
        self.assertEqual(req["query"]["uin"], "123456")
        self.assertEqual(req["query"]["pagenum"], "2")
        self.assertEqual(req["query"]["format"], "json")
        self.assertEqual(req["query"]["g_tk"], str(hash33("def")))
        # cookie 必须带上登录态
        cookie_header = req["headers"].get("cookie", "")
        self.assertIn("skey=abc", cookie_header)
        self.assertIn("p_skey=def", cookie_header)
        self.assertIn("user.qzone.qq.com", req["headers"].get("referer", ""))
        self.assertEqual(req["headers"].get("origin"), "https://user.qzone.qq.com")

    def test_feed_parses_response(self):
        self.recorder.feed_payload = {
            "code": 0,
            "data": {
                "data": [
                    {
                        "content": "今天出门忘了带伞",
                        "fid": "f1",
                        "uin": 123456,
                        "nickname": "望山",
                        "created_time": 1700000000,
                        "cmtnum": 2,
                        "like": {"cnt": 5},
                    }
                ]
            },
        }
        items = asyncio.run(self.client.feed(limit=5))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["content"], "今天出门忘了带伞")
        self.assertEqual(items[0]["like_count"], 5)
        self.assertEqual(items[0]["comment_count"], 2)

    def test_publish_form(self):
        result = asyncio.run(self.client.publish("今晚月色不错"))
        self.assertTrue(result["ok"])
        req = self.recorder.requests[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["query"]["g_tk"], str(hash33("def")))
        form = parse_qs(req["body"])
        self.assertEqual(form["con"], ["今晚月色不错"])
        self.assertEqual(form["hostuin"], ["123456"])
        self.assertEqual(form["format"], ["json"])
        self.assertEqual(form["issyncweibo"], ["0"])
        self.assertIn("skey=abc", req["headers"].get("cookie", ""))

    def test_publish_failure_code(self):
        self.recorder.publish_payload = {"code": -4001, "subcode": 0, "message": "内容包含敏感词"}
        with self.assertRaises(Exception) as ctx:
            asyncio.run(self.client.publish("测试内容"))
        self.assertIn("敏感词", str(ctx.exception))


    def test_user_feeds_request_targets_other_uin(self):
        self.recorder.feed_payload = {
            "code": 0,
            "msglist": [
                {
                    "tid": "t1",
                    "content": "在改插件的设置页",
                    "created_time": 1700000100,
                    "name": "望山",
                    "uin": 10001,
                    "cmtnum": 1,
                    "likenum": 4,
                    "pic": [{"url1": "https://example.com/a.jpg"}],
                }
            ],
        }
        items = asyncio.run(self.client.user_feeds(10001, page=1, limit=5))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["content"], "在改插件的设置页")
        self.assertEqual(items[0]["like_count"], 4)
        self.assertEqual(items[0]["images"], ["https://example.com/a.jpg"])

        req = self.recorder.requests[0]
        self.assertTrue(req["path"].endswith("/msglist"))
        # 目标 uin 必须是我们问的那个人，不是登录账号
        self.assertEqual(req["query"]["uin"], "10001")
        self.assertEqual(req["query"]["hostUin"], "10001")
        self.assertEqual(req["query"]["pos"], "0")
        self.assertEqual(req["query"]["num"], "5")
        self.assertEqual(req["query"]["format"], "json")
        self.assertIn(f"user.qzone.qq.com/10001", req["headers"].get("referer", ""))
        self.assertIn("skey=abc", req["headers"].get("cookie", ""))

    def test_user_feeds_paging(self):
        self.recorder.feed_payload = {"code": 0, "msglist": []}
        asyncio.run(self.client.user_feeds(10001, page=3, limit=10))
        req = self.recorder.requests[0]
        self.assertEqual(req["query"]["pos"], "20")
        self.assertEqual(req["query"]["uin"], "10001")


class TestGtkConsistency(unittest.TestCase):
    def test_auth_gtk_uses_secret(self):
        auth = QzoneAuth({"uin": "o1", "skey": "s1", "p_skey": "s2"})
        self.assertEqual(auth.gtk, hash33("s2"))
        self.assertEqual(gtk_of(auth.cookies), auth.gtk)


if __name__ == "__main__":
    unittest.main()
