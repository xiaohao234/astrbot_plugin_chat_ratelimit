# -*- coding: utf-8 -*-
"""chat_ratelimit v1.4.0 单元测试：桩掉 astrbot 模块，直接测试 main.py 真实代码"""
import sys
import types
import time

# ---------- 桩掉 astrbot 模块 ----------
pkg = types.ModuleType("astrbot"); sys.modules["astrbot"] = pkg
api = types.ModuleType("astrbot.api"); sys.modules["astrbot.api"] = api
api_event = types.ModuleType("astrbot.api.event"); sys.modules["astrbot.api.event"] = api_event
api_star = types.ModuleType("astrbot.api.star"); sys.modules["astrbot.api.star"] = api_star
api_comp = types.ModuleType("astrbot.api.message_components"); sys.modules["astrbot.api.message_components"] = api_comp


class _Logger:
    def info(self, m): print("  [log]", m)
    def error(self, m): print("  [log-err]", m)
logger = _Logger()
AstrBotConfig = dict
api.logger = logger
api.AstrBotConfig = AstrBotConfig


class At:
    def __init__(self, qq, name=""):
        self.qq = qq
        self.name = name


class Reply:
    def __init__(self, sender_id):
        self.sender_id = sender_id


api_comp.At = At


class EventMessageType:
    GROUP_MESSAGE = 1
    PRIVATE_MESSAGE = 2
    ALL = 3


class PermissionType:
    ADMIN = 1


class _filter:
    EventMessageType = EventMessageType
    PermissionType = PermissionType
    @staticmethod
    def event_message_type(t, priority=0):
        def deco(fn):
            return fn
        return deco
    @staticmethod
    def command(name, permission_type=None):
        def deco(fn):
            return fn
        return deco


api_event.filter = _filter
api_event.AstrMessageEvent = object


class Star:
    def __init__(self, context):
        pass


def register(*a, **k):
    def deco(cls):
        return cls
    return deco


class Context:
    pass


api_star.Context = Context
api_star.Star = Star
api_star.register = register

# ---------- 加载真实 main.py ----------
import importlib.util
spec = importlib.util.spec_from_file_location(
    "main",
    r"G:\WBwork\astr-rest\astrbot_plugin_chat_ratelimit\main.py",
)
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)
print("1. main.py 加载成功（含 astrbot 导入桩）")

# ---------- Mock 事件 ----------
class MockEvent:
    def __init__(self, chain, self_id="10086", raw_self_id=None,
                 message_str="", session="groupA", wake=False,
                 parsed_params=None):
        self.message_obj = types.SimpleNamespace(
            self_id=self_id,
            message=chain,
            raw_message={"self_id": raw_self_id} if raw_self_id is not None else None,
        )
        self.unified_msg_origin = session
        self.message_str = message_str
        self.is_wake = wake
        self.is_at_or_wake_command = wake  # 模拟新版AstrBot：@bot时该标志也为True
        self._extras = {"handlers_parsed_params": parsed_params or {}}
        self.stopped = False
        self.results = []

    def get_self_id(self):
        return self.message_obj.self_id

    def get_messages(self):
        return self.message_obj.message

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def plain_result(self, text):
        return ("text", text)

    def stop_event(self):
        self.stopped = True


def run_handle(plugin, ev, need_at=True):
    out = list(plugin._handle(ev, need_at=need_at))
    return out, ev.stopped


# ---------- 场景测试 ----------
cfg = {
    "enabled": True, "limit_per_minute": 2, "limit_per_half_hour": 0,
    "limit_per_hour": 0, "limit_per_day": 0,
    "reply_texts": ["额度用完啦"], "notice_cooldown": 0,
}
plugin = main.ChatRateLimitPlugin(Context(), cfg)

# 场景1：@bot 正常对话（关键回归：is_at_or_wake_command=True 也不能绕过限流）
ev1 = MockEvent([At("10086")], wake=True)
out, stopped = run_handle(plugin, ev1)
assert not out and not stopped and len(plugin.records["groupA"]["minute"]) == 1
ev1b = MockEvent([At("10086")], wake=True)
out, stopped = run_handle(plugin, ev1b)
assert not out and not stopped
# 第3次应被拦截
ev1c = MockEvent([At("10086")], wake=True)
out, stopped = run_handle(plugin, ev1c)
assert out and stopped, "第3次@bot应被拦截"
print("2. @bot 限流触发 OK（is_at_or_wake_command=True 也不影响）")

# 场景2：@别人 忽略
ev2 = MockEvent([At("99999")], wake=False, session="groupB")
out, stopped = run_handle(plugin, ev2)
assert not out and not stopped and "groupB" not in plugin.records
print("3. @别人完全忽略 OK（不计数、不拦截、不报错）")

# 场景3：引用bot不@ 忽略
ev3 = MockEvent([Reply("10086"), ("text", "你好")], wake=True, session="groupC")
out, stopped = run_handle(plugin, ev3)
assert not out and not stopped
print("4. 引用bot但不@ 忽略 OK")

# 场景4：旧版AstrBot self_id为空，raw_message兜底识别
ev4 = MockEvent([At("10001")], self_id="", raw_self_id="10001", wake=True, session="groupD")
assert plugin._is_at_bot(ev4) is True
out, stopped = run_handle(plugin, ev4)
assert not stopped and "groupD" in plugin.records
print("5. raw_message.self_id 兜底识别机器人ID OK")

# 场景5：命令不计数不拦截（/@bot /help）——用全新会话 groupF 避开已满额的 groupA
ev5 = MockEvent([At("10086")], message_str="/help", wake=True, session="groupF")
out, stopped = run_handle(plugin, ev5)
assert not out and not stopped
ev5b = MockEvent([At("10086")], message_str="你好呀", wake=True, session="groupF")
n_before = len(plugin._get_records("groupF")["minute"])
run_handle(plugin, ev5b)
assert len(plugin.records["groupF"]["minute"]) == n_before + 1
# handlers_parsed_params 命中命令时也跳过
ev5c = MockEvent([At("10086")], message_str="签到", parsed_params={"x": 1}, wake=True, session="groupF")
run_handle(plugin, ev5c)
assert len(plugin.records["groupF"]["minute"]) == n_before + 1
print("6. 命令跳过 OK（/前缀兜底 + handlers_parsed_params 双通道）")

# 场景6：每个群独立配额
for _ in range(3):
    run_handle(plugin, MockEvent([At("10086")], wake=True, session="groupE"))
assert plugin.records["groupE"]["minute"].__len__() >= 2
ev_groupA = MockEvent([At("10086")], wake=True, session="groupA")
out, stopped = run_handle(plugin, ev_groupA)  # groupA 早已满
assert stopped
print("7. 各群独立计数 OK（groupA满额被拦，groupE独立）")

# 场景7：冷却静默（notice_cooldown=30）
cfg2 = dict(cfg); cfg2["notice_cooldown"] = 30
p2 = main.ChatRateLimitPlugin(Context(), cfg2)
run_handle(p2, MockEvent([At("10086")], wake=True, session="g"))
run_handle(p2, MockEvent([At("10086")], wake=True, session="g"))
out, stopped = run_handle(p2, MockEvent([At("10086")], wake=True, session="g"))
assert stopped and out, "首次超限应回复提示"
out2, stopped2 = run_handle(p2, MockEvent([At("10086")], wake=True, session="g"))
assert stopped2 and not out2, "冷却期内应静默拦截"
print("8. 提示冷却 OK（首次回复，冷却内静默）")

print("\n全部 8 组场景测试通过 ✓")
