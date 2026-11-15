"""收线静默的权限门（close.trigger_mode / close.trigger_whitelist）。

「什么话算结束语」和「谁能让它停下」是两件事。这里只测第二个：
只要权限没过，别人说一百句晚安，它也该醒着。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.night.close import (  # noqa: E402
    close_minutes,
    is_closing,
    trigger_allowed,
)

OWNER = "3190568934"
STRANGER = "10001"
GROUP = "577084969"


class Cfg:
    """只实现 close 相关读取的最小配置桩。"""

    def __init__(self, **kv) -> None:
        self.data = {"close.trigger_mode": "whitelist", "close.trigger_whitelist": []}
        self.data.update(kv)

    def str(self, key, default=""):
        return str(self.data.get(key, default))

    def list(self, key, default=None):
        value = self.data.get(key, default if default is not None else [])
        return list(value or [])

    def int(self, key, default=0):
        return int(self.data.get(key, default))

    def bool(self, key, default=False):
        return bool(self.data.get(key, default))


class TriggerWhitelistTest(unittest.TestCase):
    def test_stranger_refused_when_whitelist_given(self):
        cfg = Cfg(**{"close.trigger_whitelist": [OWNER]})
        ok, why = trigger_allowed(STRANGER, cfg, admins=[])
        self.assertFalse(ok)
        self.assertEqual(why, "not_whitelisted")

    def test_whitelist_member_allowed(self):
        cfg = Cfg(**{"close.trigger_whitelist": [OWNER]})
        ok, why = trigger_allowed(OWNER, cfg, admins=[])
        self.assertTrue(ok)
        self.assertEqual(why, "whitelist")

    def test_admin_always_allowed(self):
        cfg = Cfg(**{"close.trigger_whitelist": [OWNER]})
        ok, why = trigger_allowed(STRANGER, cfg, admins=[STRANGER])
        self.assertTrue(ok)
        self.assertEqual(why, "admin")

    def test_empty_whitelist_falls_back_to_admin_only(self):
        cfg = Cfg()
        self.assertFalse(trigger_allowed(STRANGER, cfg, admins=[])[0])
        self.assertTrue(trigger_allowed(STRANGER, cfg, admins=[STRANGER])[0])

    def test_group_id_in_whitelist_opens_whole_group(self):
        cfg = Cfg(**{"close.trigger_whitelist": [GROUP]})
        ok, why = trigger_allowed(STRANGER, cfg, admins=[], session_id=GROUP)
        self.assertTrue(ok)
        self.assertEqual(why, "group")

    def test_group_id_does_not_leak_to_private(self):
        cfg = Cfg(**{"close.trigger_whitelist": [GROUP]})
        ok, why = trigger_allowed(STRANGER, cfg, admins=[], session_id=STRANGER)
        self.assertFalse(ok)

    def test_admin_mode_ignores_whitelist_member(self):
        cfg = Cfg(**{"close.trigger_mode": "admin", "close.trigger_whitelist": [OWNER]})
        self.assertFalse(trigger_allowed(OWNER, cfg, admins=[])[0])
        self.assertTrue(trigger_allowed(OWNER, cfg, admins=[OWNER])[0])

    def test_all_mode_lets_anyone(self):
        cfg = Cfg(**{"close.trigger_mode": "all"})
        ok, why = trigger_allowed(STRANGER, cfg, admins=[])
        self.assertTrue(ok)
        self.assertEqual(why, "all")

    def test_unknown_mode_is_treated_as_whitelist(self):
        cfg = Cfg(**{"close.trigger_mode": "whatever", "close.trigger_whitelist": []})
        self.assertFalse(trigger_allowed(STRANGER, cfg, admins=[])[0])

    def test_blank_entries_do_not_match_blank_sender(self):
        cfg = Cfg(**{"close.trigger_whitelist": ["", "  "]})
        self.assertFalse(trigger_allowed("", cfg, admins=[])[0])


class TriggerKeepsClosingDetectionTest(unittest.TestCase):
    """权限和语义判定互不干扰：白名单里的人照样能靠一句话停下。"""

    def test_closing_still_detected_for_whitelisted(self):
        cfg = Cfg(**{"close.trigger_whitelist": [OWNER], "close.strong_words": [], "close.medium_words": [], "close.weak_words": []})
        closing, strength, word = is_closing("晚安", cfg)
        self.assertTrue(closing)
        self.assertEqual(word, "晚安")
        self.assertTrue(trigger_allowed(OWNER, cfg, admins=[])[0])

    def test_close_minutes_unchanged(self):
        cfg = Cfg(**{"close.noon_mute_minutes": 75, "close.bye_mute_minutes": 30})
        self.assertEqual(close_minutes(cfg, "strong", "晚安"), 0)
        self.assertEqual(close_minutes(cfg, "medium", "午安"), 75)
        self.assertEqual(close_minutes(cfg, "medium", "拜拜"), 30)


class WordMergeTest(unittest.TestCase):
    """词表是「默认 + 配置」合并，不是配置整表覆盖。

    原来 `config.list(...) or list(STRONG)` 只要配置非空就把默认词全废掉，
    「睡觉去」就是这么无声失效的。
    """

    def test_new_sleep_words_work(self):
        cfg = Cfg()
        for word in ("睡觉吧", "去睡觉", "睡吧", "该睡了"):
            closing, strength, hit = is_closing(word, cfg)
            self.assertTrue(closing, f"{word} 没被认成收线")
            self.assertEqual(strength, "strong")

    def test_default_survives_user_word_list(self):
        """用户在配置里只写了几个词，默认的「晚安」不能因此失效。"""
        cfg = Cfg(**{"close.strong_words": ["打烊了"], "close.medium_words": [], "close.weak_words": []})
        self.assertTrue(is_closing("晚安", cfg)[0])
        self.assertTrue(is_closing("打烊了", cfg)[0])

    def test_removed_words_blocks_default(self):
        cfg = Cfg(**{"close.removed_words": ["晚安"]})
        self.assertFalse(is_closing("晚安", cfg)[0])
        # 其余默认词不受影响
        self.assertTrue(is_closing("睡啦", cfg)[0])

    def test_narration_still_blocked(self):
        cfg = Cfg()
        self.assertFalse(is_closing("他说睡觉吧", cfg)[0])

    def test_question_still_blocked(self):
        cfg = Cfg()
        self.assertFalse(is_closing("睡觉吧？", cfg)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
