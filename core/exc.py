"""异常摘要：给后台失败日志用。

为什么不能直接记 `str(exc)`：后台失败里混着模型调用、用户消息解析、密钥读取，
把原始消息写进日志等于把私聊内容和密钥往日志文件里倒。P1 阶段定的规矩是
「后台失败只记类型名」，测试里也有断言守着这条。

但只记类型名又不够用：真实库里连续报 `OperationalError` 的时候，看不出是哪一列、
哪张表，只能去翻代码猜。

折中：数据库和文件系统的异常单独给简短消息——这两类不带用户内容，
而且排查时少了它基本没法定位。其余一律只给类型名。
"""

from __future__ import annotations

import sqlite3


def exc_brief(exc: BaseException, *, limit: int = 160) -> str:
    """日志里用的异常摘要。"""
    name = type(exc).__name__
    if isinstance(exc, (sqlite3.Error, OSError)):
        text = str(exc).replace("\n", " ").strip()
        if text:
            return f"{name}: {text[:limit]}"
    return name
