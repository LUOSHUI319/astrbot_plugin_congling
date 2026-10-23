"""空间协议层单测。

这些函数的对错不看日志看不出来，只能靠断言。
参考的协议细节来自 astrbot_plugin_qzone_ultra。
"""

from __future__ import annotations

import asyncio
import unittest

from astrbot_plugin_congling.qqext.qzone_client import (
    MAX_POST_CHARS,
    QzoneAuth,
    QzoneAuthError,
    QzoneClient,
    QzoneRequestError,
    extract_cookies,
    gtk_of,
    hash33,
    normalize_cookie_fields,
    parse_cookie_text,
    strip_html,
)


def _reference_hash33(key: str, phash: int = 5381) -> int:
    """独立再写一遍，用来对照主实现有没有写歪。"""
    result = phash
    for ch in key:
        result = result + (result << 5) + ord(ch)
    return result & 0x7FFFFFFF


class TestGtk(unittest.TestCase):
    def test_hash33_matches_reference(self):
        for key in ("", "a", "skey-value", "p_skey_0123456789", "中文key"):
            self.assertEqual(hash33(key), _reference_hash33(key), key)

    def test_known_value(self):
        # 空串时就是初始值
        self.assertEqual(hash33(""), 5381)

    def test_gtk_from_skey(self):
        cookies = normalize_cookie_fields({"skey": "@abcdefg"})
        self.assertEqual(gtk_of(cookies), _reference_hash33("@abcdefg"))

    def test_gtk_prefers_p_skey(self):
        cookies = normalize_cookie_fields({"skey": "aa", "p_skey": "bb"})
        self.assertEqual(gtk_of(cookies), _reference_hash33("bb"))

    def test_gtk_passthrough(self):
        self.assertEqual(gtk_of({"g_tk": "12345"}), 12345)

    def test_gtk_zero_when_missing(self):
        self.assertEqual(gtk_of({}), 0)


class TestCookieParsing(unittest.TestCase):
    def test_cookie_string(self):
        cookies = parse_cookie_text("uin=o123456; skey=abc; p_skey=xyz")
        self.assertEqual(cookies["uin"], "o123456")
        self.assertEqual(cookies["skey"], "abc")
        self.assertEqual(cookies["p_skey"], "xyz")

    def test_cookie_string_with_prefix(self):
        cookies = parse_cookie_text("Cookie: uin=o1; skey=s")
        self.assertEqual(cookies["uin"], "o1")

    def test_pair_list(self):
        payload = [
            {"domain": "user.qzone.qq.com", "name": "uin", "value": "o999"},
            {"domain": "user.qzone.qq.com", "name": "skey", "value": "k"},
        ]
        cookies = extract_cookies(payload)
        self.assertEqual(cookies["uin"], "o999")
        self.assertEqual(cookies["skey"], "k")

    def test_mapping_payload(self):
        cookies = extract_cookies({"uin": "o5", "skey": "s1", "p_skey": "p1"})
        self.assertEqual(cookies["uin"], "o5")
        self.assertEqual(cookies["p_skey"], "p1")

    def test_nested_payload(self):
        cookies = extract_cookies({"retcode": 0, "data": {"cookies": "uin=o7; skey=s7"}})
        self.assertEqual(cookies["uin"], "o7")
        self.assertEqual(cookies["skey"], "s7")

    def test_alias_normalization(self):
        cookies = normalize_cookie_fields({"pskey": "v1", "bkn": "777"})
        self.assertEqual(cookies["p_skey"], "v1")
        self.assertEqual(cookies["g_tk"], "777")

    def test_empty_payload(self):
        self.assertEqual(extract_cookies(None), {})
        self.assertEqual(extract_cookies("no cookies here"), {})


class TestAuth(unittest.TestCase):
    def test_usable(self):
        auth = QzoneAuth({"uin": "o123456", "skey": "s"}, source="test")
        self.assertTrue(auth.usable)
        self.assertEqual(auth.uin, 123456)
        self.assertEqual(auth.source, "test")

    def test_not_usable_without_login_cookie(self):
        self.assertFalse(QzoneAuth({"uin": "o1"}).usable)

    def test_not_usable_without_uin(self):
        self.assertFalse(QzoneAuth({"skey": "s"}).usable)

    def test_round_trip(self):
        auth = QzoneAuth({"uin": "o42", "skey": "s"}, source="onebot:get_cookies")
        again = QzoneAuth.from_dict(auth.to_dict())
        self.assertEqual(again.uin, 42)
        self.assertEqual(again.source, "onebot:get_cookies")
        self.assertTrue(again.usable)

    def test_summary_has_fingerprint(self):
        summary = QzoneAuth({"uin": "o42", "skey": "s"}).summary()
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["uin"], 42)
        self.assertIn("42_", summary["fingerprint"])


class TestFeedParsing(unittest.TestCase):
    def test_strip_html(self):
        raw = '<div class="x">今天<b>有点</b>累<br/>早点睡</div>'
        self.assertEqual(strip_html(raw), "今天有点累\n早点睡")

    def test_parse_html_feed(self):
        payload = {
            "code": 0,
            "data": {
                "data": [
                    {
                        "appid": 311,
                        "fid": "abc123",
                        "uin": 42,
                        "nickname": "望山",
                        "content": "",
                        "html": "<li><a>望山</a>：今天下雨了，懒得动</li>",
                        "created_time": 1700000000,
                        "cmtnum": 3,
                        "like": {"cnt": 7},
                    }
                ]
            },
        }
        items = QzoneClient._parse_feed(payload, 10)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIn("今天下雨了", item["content"])
        self.assertEqual(item["nickname"], "望山")
        self.assertEqual(item["fid"], "abc123")
        self.assertEqual(item["comment_count"], 3)
        self.assertEqual(item["like_count"], 7)
        self.assertTrue(item["created_text"])

    def test_parse_respects_limit(self):
        rows = [{"content": f"第{i}条", "fid": str(i)} for i in range(10)]
        items = QzoneClient._parse_feed({"code": 0, "data": {"data": rows}}, 3)
        self.assertEqual(len(items), 3)

    def test_parse_skips_empty(self):
        payload = {"code": 0, "data": {"data": [{"content": "", "html": ""}, {"content": "有用的"}]}}
        items = QzoneClient._parse_feed(payload, 5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["content"], "有用的")


class TestErrorHandling(unittest.TestCase):
    def test_auth_code_raises_auth_error(self):
        client = QzoneClient(QzoneAuth({"uin": "o1", "skey": "s"}))
        with self.assertRaises(QzoneAuthError):
            client._check_code({"code": -3000})

    def test_login_message_raises_auth_error(self):
        client = QzoneClient(QzoneAuth())
        with self.assertRaises(QzoneAuthError):
            client._check_code({"code": 0, "message": "请先登录"})

    def test_other_code_raises_request_error(self):
        client = QzoneClient(QzoneAuth())
        with self.assertRaises(QzoneRequestError):
            client._check_code({"code": -4001, "message": "参数不对"})

    def test_ok_code_passes(self):
        client = QzoneClient(QzoneAuth())
        client._check_code({"code": 0, "subcode": 0})

    def test_publish_requires_content(self):
        client = QzoneClient(QzoneAuth({"uin": "o1", "skey": "s"}))
        with self.assertRaises(QzoneRequestError):
            asyncio.run(client.publish("   "))

    def test_publish_rejects_too_long(self):
        client = QzoneClient(QzoneAuth({"uin": "o1", "skey": "s"}))
        with self.assertRaises(QzoneRequestError):
            asyncio.run(client.publish("字" * (MAX_POST_CHARS + 1)))

    def test_publish_without_auth(self):
        client = QzoneClient(QzoneAuth())
        with self.assertRaises(QzoneAuthError):
            asyncio.run(client.publish("测试"))


if __name__ == "__main__":
    unittest.main()
