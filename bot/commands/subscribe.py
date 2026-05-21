# -*- coding: utf-8 -*-
"""
===================================
自选股订阅命令
===================================

用户订阅 / 取消订阅 / 查看自选股定时推送。
"""

import re
import logging
from typing import List, Optional

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse, ChatType

logger = logging.getLogger(__name__)

MAX_STOCKS_PER_SUB = 15


class SubscribeCommand(BotCommand):
    """
    订阅自选股定时推送

    用法：
        /subscribe 600519,hk00700,AAPL [18:00]
        订阅 600519,hk00700 18:00
    """

    @property
    def name(self) -> str:
        return "subscribe"

    @property
    def aliases(self) -> List[str]:
        return ["sub", "订阅"]

    @property
    def description(self) -> str:
        return "订阅自选股定时推送"

    @property
    def usage(self) -> str:
        return "/subscribe <股票代码1,股票代码2,...> [HH:MM] [simple|full|brief]"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        if not args:
            return BotResponse.markdown_response(
                "**用法：** `/subscribe 600519,hk00700,AAPL [18:00] [full]`\n\n"
                "用逗号或空格分隔股票代码，最多 15 只。\n"
                "可选指定推送时间（默认 18:00）。\n"
                "可选指定报告类型：`simple`（精简）/ `full`（完整）/ `brief`（简洁），默认 simple。"
            )

        # 解析股票代码、可选时间参数和报告类型
        raw_items = []
        time_arg = None
        report_type = None
        valid_report_types = {"simple", "full", "brief"}
        for arg in args:
            # 匹配 HH:MM 格式
            if re.match(r'^\d{1,2}:\d{2}$', arg):
                time_arg = arg
                continue
            # 匹配报告类型
            if arg.lower() in valid_report_types:
                report_type = arg.lower()
                continue
            for part in re.split(r'[,，、\s]+', arg):
                part = part.strip()
                if part:
                    raw_items.append(part)

        if not raw_items:
            return BotResponse.error_response("请输入至少一个股票代码")

        # 解析股票代码
        codes = []
        failed = []
        for item in raw_items:
            code = self._resolve_stock_code(item)
            if code:
                codes.append(code)
            else:
                failed.append(item)

        if not codes:
            return BotResponse.error_response(
                f"无法识别股票代码: {', '.join(failed)}"
            )

        if len(codes) > MAX_STOCKS_PER_SUB:
            return BotResponse.error_response(
                f"单份订阅最多 {MAX_STOCKS_PER_SUB} 只股票，当前 {len(codes)} 只"
            )

        # 解析推送时间
        push_hour, push_minute = 18, 0
        if time_arg:
            parts = time_arg.split(':')
            push_hour = int(parts[0])
            push_minute = int(parts[1])
            if not (0 <= push_hour <= 23 and 0 <= push_minute <= 59):
                return BotResponse.error_response("时间格式无效，请使用 HH:MM（如 18:00）")

        # 保存订阅
        try:
            from src.repositories.subscription_repo import SubscriptionRepository

            repo = SubscriptionRepository()
            chat_type_str = "private" if message.chat_type == ChatType.PRIVATE else "group"
            sub = repo.upsert(
                platform=message.platform,
                user_id=message.user_id,
                chat_id=message.chat_id,
                chat_type=chat_type_str,
                stock_codes=",".join(codes),
                push_hour=push_hour,
                push_minute=push_minute,
                report_type=report_type or "simple",
                message_id=message.message_id,
            )
        except Exception as e:
            logger.error(f"[SubscribeCommand] 保存订阅失败: {e}")
            return BotResponse.error_response("订阅保存失败，请稍后重试")

        # 构建确认回复
        codes_display = "、".join(codes)
        time_display = f"{push_hour:02d}:{push_minute:02d}"
        rt_display = report_type or "simple"
        rt_labels = {"simple": "精简", "full": "完整", "brief": "简洁"}
        failed_info = ""
        if failed:
            failed_info = f"\n\n⚠️ 未识别: {', '.join(failed)}"

        return BotResponse.markdown_response(
            f"✅ 订阅成功！\n\n"
            f"**股票：** {codes_display}\n"
            f"**推送时间：** {time_display}\n"
            f"**报告类型：** {rt_labels.get(rt_display, rt_display)}\n"
            f"**推送方式：** {'私聊' if chat_type_str == 'private' else '群内回复'}"
            f"{failed_info}"
        )

    def _resolve_stock_code(self, raw_input: str) -> Optional[str]:
        """解析股票代码"""
        from data_provider.base import canonical_stock_code

        code = canonical_stock_code(raw_input)
        if code:
            upper = code.upper()
            if re.match(r'^\d{6}$', upper) or \
               re.match(r'^JJ\d{6}$', upper) or \
               re.match(r'^HK\d{5}$', upper) or \
               re.match(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$', upper):
                return code

        from src.services.name_to_code_resolver import resolve_name_to_code
        resolved = resolve_name_to_code(raw_input)
        if resolved:
            return canonical_stock_code(resolved)

        return None


class UnsubscribeCommand(BotCommand):
    """
    取消订阅

    用法：
        /unsubscribe
        取消订阅
    """

    @property
    def name(self) -> str:
        return "unsubscribe"

    @property
    def aliases(self) -> List[str]:
        return ["unsub", "取消订阅"]

    @property
    def description(self) -> str:
        return "取消自选股订阅"

    @property
    def usage(self) -> str:
        return "/unsubscribe"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        try:
            from src.repositories.subscription_repo import SubscriptionRepository

            repo = SubscriptionRepository()
            ok = repo.disable(user_id=message.user_id, chat_id=message.chat_id)
            if ok:
                return BotResponse.text_response("✅ 已取消订阅")
            else:
                return BotResponse.text_response("当前没有活跃订阅")
        except Exception as e:
            logger.error(f"[UnsubscribeCommand] 取消订阅失败: {e}")
            return BotResponse.error_response("操作失败，请稍后重试")


class MySubsCommand(BotCommand):
    """
    查看我的订阅

    用法：
        /subs
        /mysubs
        我的订阅
    """

    @property
    def name(self) -> str:
        return "subs"

    @property
    def aliases(self) -> List[str]:
        return ["mysubs", "我的订阅"]

    @property
    def description(self) -> str:
        return "查看我的订阅"

    @property
    def usage(self) -> str:
        return "/subs"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        try:
            from src.repositories.subscription_repo import SubscriptionRepository

            repo = SubscriptionRepository()
            subs = repo.list_by_user(user_id=message.user_id)
        except Exception as e:
            logger.error(f"[MySubsCommand] 查询订阅失败: {e}")
            return BotResponse.error_response("查询失败，请稍后重试")

        if not subs:
            return BotResponse.text_response("你还没有订阅。\n使用 `/subscribe 600519,hk00700` 开始订阅。")

        lines = ["📋 **我的订阅**", ""]
        rt_labels = {"simple": "精简", "full": "完整", "brief": "简洁"}
        for i, sub in enumerate(subs, 1):
            status = "✅" if sub.enabled else "❌"
            time_str = f"{sub.push_hour:02d}:{sub.push_minute:02d}"
            codes = sub.stock_codes.replace(",", "、")
            chat_type_label = "私聊" if sub.chat_type == "private" else "群聊"
            rt = getattr(sub, "report_type", "simple") or "simple"
            lines.append(
                f"{i}. {status} {codes}\n"
                f"   推送时间 {time_str} · {rt_labels.get(rt, rt)}报告 · {chat_type_label}"
            )
            lines.append("")

        return BotResponse.markdown_response("\n".join(lines))
