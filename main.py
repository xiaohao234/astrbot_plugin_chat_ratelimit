# -*- coding: utf-8 -*-
"""
astrbot_plugin_chat_ratelimit
群聊 LLM 对话总量限流插件：
每个群（会话）独立配额，群内所有用户共享。只有消息中显式 @机器人 时才计数；
达到限额后再 @Bot 不会触发 LLM 对话，而是随机回复一条配置的提示语。
引用/回复消息但不 @机器人、@其他用户、普通聊天均不处理。
"""
import random
import time
from collections import deque

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

try:
    from astrbot.api.message_components import At
except ImportError:  # 兼容旧版本 AstrBot
    from astrbot.core.message.components import At


@register(
    "astrbot_plugin_chat_ratelimit",
    "xiaohao234",
    "限制 Bot 群聊 LLM 对话总频率（每个群独立配额），超出限额后 @Bot 只回复自定义随机提示语",
    "1.4.0",
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
        # 每个会话（群/私聊）独立记账：会话 -> {窗口名: 时间戳队列}
        self.records = {}
        # 提示语冷却：会话 -> 上次发送提示语的时间戳
        self._last_notice = {}
        logger.info("[chat_ratelimit] 插件已加载 v1.4.0")

    # ---------- 限流核心 ----------

    def _get_limit(self, name: str) -> int:
        """读取某窗口的限额，0 表示不限制"""
        try:
            return int(self.config.get(f"limit_per_{name}", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _get_notice_cooldown(self) -> int:
        try:
            return max(0, int(self.config.get("notice_cooldown", 30) or 0))
        except (TypeError, ValueError):
            return 30

    def _get_records(self, session: str) -> dict:
        """获取某会话（群）独立的计数记录，不存在则初始化"""
        if session not in self.records:
            self.records[session] = {name: deque() for name in self.WINDOW_DEFS}
        return self.records[session]

    def _cleanup(self, recs: dict, now: float):
        """清理某会话所有窗口的过期时间戳"""
        for name, window in self.WINDOW_DEFS.items():
            dq = recs[name]
            while dq and now - dq[0] > window:
                dq.popleft()

    def _try_acquire(self, session: str):
        """
        尝试为某会话（群）获取一次对话额度（群内所有用户合计）。
        全部窗口未超限 -> 记录本次并返回 (True, None)
        任一窗口已满   -> 不记录，返回 (False, 窗口名)
        """
        now = time.time()
        recs = self._get_records(session)
        self._cleanup(recs, now)
        for name in self.WINDOW_DEFS:
            limit = self._get_limit(name)
            if limit > 0 and len(recs[name]) >= limit:
                return False, name
        for dq in recs.values():
            dq.append(now)
        return True, None

    # ---------- 消息判定 ----------

    def _get_bot_ids(self, event: AstrMessageEvent) -> set:
        """
        多渠道收集机器人自身 ID，兼容不同版本 AstrBot：
        1. event.get_self_id() / message_obj.self_id
        2. napcat/aiocqhttp 原始报文 raw_message 里的 self_id
        """
        ids = set()
        try:
            sid = str(event.get_self_id() or "").strip()
            if sid:
                ids.add(sid)
        except Exception:
            pass
        try:
            sid = str(getattr(event.message_obj, "self_id", "") or "").strip()
            if sid:
                ids.add(sid)
        except Exception:
            pass
        # aiocqhttp(napcat) 的原始 OneBot 事件里必带 self_id
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            if raw is not None:
                sid = None
                if isinstance(raw, dict):
                    sid = raw.get("self_id")
                else:
                    sid = getattr(raw, "self_id", None)
                if sid is not None:
                    sid = str(sid).strip()
                    if sid:
                        ids.add(sid)
        except Exception:
            pass
        return ids

    def _get_chain(self, event: AstrMessageEvent) -> list:
        """获取消息链组件列表"""
        try:
            chain = event.get_messages()
            if chain:
                return chain
        except Exception:
            pass
        try:
            return getattr(event.message_obj, "message", []) or []
        except Exception:
            return []

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        """
        严格判断消息链中是否存在显式 @机器人 的 At 组件。
        注意：不能使用 is_wake（引用/回复 bot 消息也会唤醒），
        也不能使用 is_at_or_wake_command（@机器人 时它也为 True）。
        """
        bot_ids = self._get_bot_ids(event)
        if not bot_ids:
            return False
        for comp in self._get_chain(event):
            if isinstance(comp, At):
                # At.qq 兼容 str/int；@全体成员为 AtAll 组件，不会误匹配
                qq = str(getattr(comp, "qq", "")).strip()
                if qq and qq in bot_ids:
                    return True
        return False

    def _is_command(self, event: AstrMessageEvent) -> bool:
        """
        判断消息是否命中了命令（如 /help）。
        命令不触发 LLM 对话：不计数、也不拦截（超限时命令仍可用）。
        """
        # 方式1：唤醒阶段匹配到命令时，AstrBot 会记录命令解析参数
        try:
            if event.get_extra("handlers_parsed_params"):
                return True
        except Exception:
            pass
        # 方式2：兜底——消息以默认命令前缀 / 开头
        try:
            msg = str(getattr(event, "message_str", "") or "").strip()
            if msg.startswith("/"):
                return True
        except Exception:
            pass
        return False

    def _get_reply(self) -> str:
        texts = self.config.get("reply_texts", None) or []
        texts = [t for t in texts if str(t).strip()]
        if not texts:
            return "当前对话太频繁啦，稍后再试试吧~"
        return random.choice(texts)

    # ---------- 拦截逻辑 ----------

    def _handle(self, event: AstrMessageEvent, need_at: bool = True):
        """统一的拦截逻辑（生成器）"""
        if not self.config.get("enabled", True):
            return
        # 群聊：只有显式 @机器人 才计数/拦截；引用不@、@别人、普通聊天一概不管
        if need_at and not self._is_at_bot(event):
            return
        # 命令不触发 LLM 对话：不计数，也不拦截
        if self._is_command(event):
            return

        session = str(getattr(event, "unified_msg_origin", "") or "")
        allowed, window = self._try_acquire(session)
        if allowed:
            return

        logger.info(
            f"[chat_ratelimit] 会话 {session} 已达到 {window} 窗口限额，拦截本次 LLM 对话"
        )
        # 无论是否发送提示语，都要拦截本次 LLM 对话
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
        try:
            for result in self._handle(event, need_at=True):
                yield result
        except Exception as e:
            # 插件自身绝不向 AstrBot 抛出异常
            logger.error(f"[chat_ratelimit] 处理群消息异常: {e}")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=20)
    async def on_private_message(self, event: AstrMessageEvent):
        if not self.config.get("also_limit_private", False):
            return
        # 私聊没有 @ 概念，开启限制私聊后对所有消息生效
        try:
            for result in self._handle(event, need_at=False):
                yield result
        except Exception as e:
            logger.error(f"[chat_ratelimit] 处理私聊消息异常: {e}")

    # ---------- 管理员命令 ----------

    def _usage_lines(self, session: str) -> list:
        now = time.time()
        name_map = {
            "minute": "每分钟",
            "half_hour": "每半小时",
            "hour": "每小时",
            "day": "每天",
        }
        recs = self._get_records(session)
        self._cleanup(recs, now)
        lines = []
        for name in self.WINDOW_DEFS:
            limit = self._get_limit(name)
            limit_str = f"上限 {limit}" if limit > 0 else "不限制"
            lines.append(f"- {name_map[name]}：已用 {len(recs[name])} 次（{limit_str}）")
        return lines

    @filter.command("ratelimit", permission_type=filter.PermissionType.ADMIN)
    async def ratelimit_status(self, event: AstrMessageEvent):
        """管理员发送 /ratelimit 查看当前会话（群）各窗口用量"""
        session = str(getattr(event, "unified_msg_origin", "") or "")
        lines = ["本会话 LLM 对话用量（群内所有用户合计，每个群独立计数）："]
        lines += self._usage_lines(session)
        lines.append(f"- 当前共有 {len(self.records)} 个会话在被统计")
        yield event.plain_result("\n".join(lines))

    @filter.command("ratelimit_debug", permission_type=filter.PermissionType.ADMIN)
    async def ratelimit_debug(self, event: AstrMessageEvent):
        """
        诊断命令：建议在群里 @机器人 并发送 /ratelimit_debug，
        检查"判定为@机器人"是否为 True。
        """
        bot_ids = self._get_bot_ids(event)
        chain = self._get_chain(event)
        comp_desc = (
            ", ".join(
                f"{type(c).__name__}({getattr(c, 'qq', '')})"
                for c in chain
            )
            or "空"
        )
        lines = [
            "== chat_ratelimit 诊断 ==",
            f"机器人ID识别: {sorted(bot_ids) if bot_ids else '未识别(为空!)'}",
            f"is_wake: {getattr(event, 'is_wake', '无此属性')}",
            f"is_at_or_wake_command: {getattr(event, 'is_at_or_wake_command', '无此属性')}",
            f"本消息组件链: {comp_desc}",
            f"判定为@机器人: {self._is_at_bot(event)}",
            f"判定为命令: {self._is_command(event)}",
            f"各窗口限额: "
            + ", ".join(f"{n}={self._get_limit(n)}" for n in self.WINDOW_DEFS),
            f"正在统计的会话数: {len(self.records)}",
        ]
        yield event.plain_result("\n".join(lines))
