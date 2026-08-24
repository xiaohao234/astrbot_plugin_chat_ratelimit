# -*- coding: utf-8 -*-
"""
astrbot_plugin_chat_ratelimit
群聊 LLM 对话总量限流插件：
所有用户共享滑动窗口配额。只有消息中显式 @机器人 时才计数；
达到限额后再 @Bot 不会触发 LLM 对话，
而是随机回复一条在插件管理页配置的提示语。
引用/回复消息但不 @机器人、@其他用户、普通聊天均不处理。
"""
import random
import time
from collections import deque

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import At


@register(
    "astrbot_plugin_chat_ratelimit",
    "xiaohao234",
    "限制 Bot 群聊 LLM 对话总频率，超出限额后 @Bot 只回复自定义随机提示语",
    "1.2.0",
    "https://github.com/xiaohao234/astrbot_plugin_chat_ratelimit",
)
class ChatRateLimitPlugin(Star):
    # 各时间窗定义：名称 -> 窗口秒数
    WINDOW_DEFS = {
        "minute": 60,
        "half_hour": 1800,
        "hour": 3600,
        "day": 86400,
    }

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 每个窗口一份时间戳队列（所有用户共享）
        self.records = {name: deque() for name in self.WINDOW_DEFS}
        # 提示语冷却：会话 -> 上次发送提示语的时间戳
        self._last_notice = {}
        logger.info("[chat_ratelimit] 插件已加载")

    # ---------- 限流核心 ----------

    def _get_limit(self, name: str) -> int:
        """读取某窗口的限额，0 表示不限制"""
        try:
            return int(self.config.get(f"limit_per_{name}", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _try_acquire(self):
        """
        尝试获取一次对话额度（所有用户合计）。
        全部窗口未超限 -> 记录本次并返回 (True, None)
        任一窗口已满   -> 不记录，返回 (False, 窗口名)
        """
        now = time.time()
        # 先清理过期记录并检查
        for name, window in self.WINDOW_DEFS.items():
            dq = self.records[name]
            while dq and now - dq[0] > window:
                dq.popleft()
            limit = self._get_limit(name)
            if limit > 0 and len(dq) >= limit:
                return False, name
        # 全部通过，记录本次
        for dq in self.records.values():
            dq.append(now)
        return True, None

    def _get_reply(self) -> str:
        texts = self.config.get("reply_texts", None) or []
        texts = [t for t in texts if str(t).strip()]
        if not texts:
            return "当前对话太频繁啦，稍后再试试吧~"
        return random.choice(texts)

    def _get_notice_cooldown(self) -> int:
        try:
            return max(0, int(self.config.get("notice_cooldown", 30) or 0))
        except (TypeError, ValueError):
            return 30

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        """
        严格判断消息链中是否存在显式 @机器人 的 At 组件。
        不使用 is_wake（它会把"回复/引用 bot 消息"也算作唤醒）。
        """
        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            self_id = ""
        if not self_id:
            self_id = str(getattr(event.message_obj, "self_id", "") or "").strip()
        if not self_id:
            return False
        try:
            chain = event.get_messages() or []
        except Exception:
            chain = getattr(event.message_obj, "message", []) or []
        for comp in chain:
            # At.qq 兼容 str / int；@全体成员的 qq 为 "all"，不会误匹配
            if isinstance(comp, At) and str(getattr(comp, "qq", "")) == self_id:
                return True
        return False

    def _handle(self, event: AstrMessageEvent, need_at: bool = True):
        """统一的拦截逻辑（生成器）"""
        if not self.config.get("enabled", True):
            return
        # 群聊：只有显式 @机器人 才计数/拦截；引用不@、@别人、普通聊天一概不管
        if need_at and not self._is_at_bot(event):
            return
        # 命令（/help、/ratelimit 等）不触发 LLM 对话：不计数，也不拦截
        if getattr(event, "is_at_or_wake_command", False):
            return

        allowed, window = self._try_acquire()
        if allowed:
            return

        logger.info(
            f"[chat_ratelimit] 已达到 {window} 窗口限额，拦截本次 LLM 对话"
        )
        # 无论是否发送提示语，都要拦截本次 LLM 对话
        session = getattr(event, "unified_msg_origin", "") or id(event)
        now = time.time()
        cooldown = self._get_notice_cooldown()
        last = self._last_notice.get(session, 0)
        if cooldown > 0 and now - last < cooldown:
            # 冷却期内：静默拦截，避免提示语刷屏
            event.stop_event()
            return
        self._last_notice[session] = now
        yield event.plain_result(self._get_reply())
        # 终止事件传播，后续默认 LLM 处理不再执行
        event.stop_event()

    # ---------- 事件监听 ----------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=20)
    async def on_group_message(self, event: AstrMessageEvent):
        for result in self._handle(event, need_at=True):
            yield result

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=20)
    async def on_private_message(self, event: AstrMessageEvent):
        if not self.config.get("also_limit_private", False):
            return
        # 私聊没有 @ 概念，开启限制私聊后对所有消息生效
        for result in self._handle(event, need_at=False):
            yield result

    # ---------- 管理员命令 ----------

    @filter.command("ratelimit", permission_type=filter.PermissionType.ADMIN)
    async def ratelimit_status(self, event: AstrMessageEvent):
        """管理员发送 /ratelimit 查看当前各窗口用量"""
        now = time.time()
        name_map = {
            "minute": "每分钟",
            "half_hour": "每半小时",
            "hour": "每小时",
            "day": "每天",
        }
        lines = ["LLM 对话用量（所有用户合计，滑动窗口）："]
        for name, window in self.WINDOW_DEFS.items():
            dq = self.records[name]
            while dq and now - dq[0] > window:
                dq.popleft()
            limit = self._get_limit(name)
            limit_str = f"上限 {limit}" if limit > 0 else "不限制"
            lines.append(f"- {name_map[name]}：已用 {len(dq)} 次（{limit_str}）")
        yield event.plain_result("\n".join(lines))
