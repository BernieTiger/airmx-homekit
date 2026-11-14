"""针对"踩过的坑"的回归护栏。

这些都是真实发生过的故障，修好之后必须防止再次引入：
1. 在 async_start() 之前调 config_changed() —— 会用默认状态覆盖磁盘上的配对信息，
   表现为每次重启 Home App 全部"无响应"、必须重新配对。
2. 服务名纠偏把主服务也纳入 —— 用户在家庭 App 里改名会被每 8 秒改回去。
"""
from __future__ import annotations

import inspect

from hap import __main__ as entry


def test_attach_must_not_call_config_changed() -> None:
    """attach() 里绝不能再出现 config_changed()。

    它会把内存里的默认 state（含随机 MAC）写盘，而 async_start() 才读盘，
    等于每次启动都清空配对。
    """
    src = inspect.getsource(entry.attach)
    code = "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )
    assert "config_changed" not in code, (
        "attach() 里不能调 config_changed()，否则会覆盖已保存的配对信息"
    )


def test_config_changed_called_after_start() -> None:
    """main() 里 config_changed() 必须在 async_start() 之后。"""
    src = inspect.getsource(entry.main)
    assert "async_start" in src and "config_changed" in src
    assert src.index("async_start") < src.index("config_changed"), (
        "config_changed() 必须晚于 async_start()，否则会破坏状态文件"
    )


def test_unknown_profile_does_not_promise_temperature() -> None:
    """未知机型不建温度服务：宁可不显示，也不要挂一个永远不可用的传感器。"""
    from hap.profiles import UNKNOWN

    assert UNKNOWN.can_report_temperature is False
