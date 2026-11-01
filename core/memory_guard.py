"""清除期间的异步业务闸门；正在生成的旧内容不能在删除提交后回写。"""
from functools import wraps


def memory_work(method):
    @wraps(method)
    async def guarded(self, *args, **kwargs):
        store = getattr(self, "store", None) or getattr(self.plugin, "store", None)
        guard = getattr(store, "memory_work", None)
        if guard is None:  # 兼容第三方存储/离线测试替身
            return await method(self, *args, **kwargs)
        async with guard():
            return await method(self, *args, **kwargs)
    return guarded
