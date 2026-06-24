# -*- coding: utf-8 -*-
"""
信号监控命令

用户订阅 / 取消 / 查看信号监控（买入/卖出点触发提醒）。
"""

import re
import logging
from typing import List, Optional

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse, ChatType

logger = logging.getLogger(__name__)

MAX_STOCKS_PER_ALERT = 15


class AlertCommand(BotCommand):
    """
    订阅信号监控

    用法：
        /alert 600519,hk00700,AAPL
        监控 中兴通讯,比亚迪
    """

    @property
    def name(self) -> str:
        return "alert"

    @property
    def aliases(self) -> List[str]:
        return ["监控", "信号监控"]

    @property
    def description(self) -> str:
        return "订阅股票信号监控（买入/卖出点提醒）"

    @property
    def usage(self) -> str:
        return "/alert <股票列表>"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        if not args:
            return BotResponse.markdown_response(
                "**用法：** `/alert 600519,hk00700,AAPL`\n\n"
                "用逗号或空格分隔股票代码，最多 15 只。\n"
                "系统将在 9:10（盘前）、12:40（午盘前）、18:00（盘后）自动检查买入/卖出点。"
            )

        raw_items = []
        for arg in args:
            for part in re.split(r'[,，、\s]+', arg):
                part = part.strip()
                if part:
                    raw_items.append(part)

        if not raw_items:
            return BotResponse.error_response("请输入至少一个股票代码")

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

        if len(codes) > MAX_STOCKS_PER_ALERT:
            return BotResponse.error_response(
                f"单份监控最多 {MAX_STOCKS_PER_ALERT} 只股票，当前 {len(codes)} 只"
            )

        try:
            from src.repositories.alert_subscription_repo import AlertSubscriptionRepo

            repo = AlertSubscriptionRepo()
            chat_type_str = "private" if message.chat_type == ChatType.PRIVATE else "group"
            sub = repo.upsert(
                platform=message.platform,
                user_id=message.user_id,
                chat_id=message.chat_id,
                chat_type=chat_type_str,
                stock_codes=",".join(codes),
                message_id=message.message_id,
            )
        except Exception as e:
            logger.error(f"[AlertCommand] 保存监控失败: {e}")
            return BotResponse.error_response("监控保存失败，请稍后重试")

        codes_display = "、".join(codes)
        all_codes = [c.strip() for c in sub.stock_codes.split(",") if c.strip()]
        all_display = "、".join(all_codes)
        failed_info = ""
        if failed:
            failed_info = f"\n\n⚠️ 未识别: {', '.join(failed)}"
        added_info = ""
        if len(all_codes) > len(codes):
            added_info = f"\n\n📋 当前监控共 {len(all_codes)} 只：{all_display}"

        return BotResponse.markdown_response(
            f"✅ 已追加到信号监控！\n\n"
            f"**新增：** {codes_display}\n"
            f"**触发时间：** 9:10（盘前）、12:40（午盘前）、18:00（盘后）\n"
            f"**分析策略：** 🌀缠论 × 💰情绪周期 × 🌊波浪理论 × 📊均线金叉（四策略投票）\n"
            f"**推送方式：** {'私聊' if chat_type_str == 'private' else '群内回复'}"
            f"{added_info}"
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


class UnalertCommand(BotCommand):
    """
    取消信号监控

    用法：
        /unalert
        取消监控
    """

    @property
    def name(self) -> str:
        return "unalert"

    @property
    def aliases(self) -> List[str]:
        return ["取消监控"]

    @property
    def description(self) -> str:
        return "取消信号监控"

    @property
    def usage(self) -> str:
        return "/unalert"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        try:
            from src.repositories.alert_subscription_repo import AlertSubscriptionRepo

            repo = AlertSubscriptionRepo()
            ok = repo.disable(user_id=message.user_id, chat_id=message.chat_id)
            if ok:
                return BotResponse.text_response("✅ 已取消信号监控")
            else:
                return BotResponse.text_response("当前没有活跃的信号监控")
        except Exception as e:
            logger.error(f"[UnalertCommand] 取消监控失败: {e}")
            return BotResponse.error_response("操作失败，请稍后重试")


class MyAlertsCommand(BotCommand):
    """
    查看我的信号监控

    用法：
        /myalerts
        我的监控
    """

    @property
    def name(self) -> str:
        return "myalerts"

    @property
    def aliases(self) -> List[str]:
        return ["我的监控"]

    @property
    def description(self) -> str:
        return "查看我的信号监控"

    @property
    def usage(self) -> str:
        return "/myalerts"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        try:
            from src.repositories.alert_subscription_repo import AlertSubscriptionRepo

            repo = AlertSubscriptionRepo()
            subs = repo.list_by_user(user_id=message.user_id)
        except Exception as e:
            logger.error(f"[MyAlertsCommand] 查询监控失败: {e}")
            return BotResponse.error_response("查询失败，请稍后重试")

        if not subs:
            return BotResponse.text_response(
                "你还没有信号监控。\n使用 `/alert 600519,hk00700` 开始监控。"
            )

        lines = ["📋 **我的信号监控**", ""]
        for i, sub in enumerate(subs, 1):
            status = "✅" if sub.enabled else "❌"
            codes = sub.stock_codes.replace(",", "、")
            chat_type_label = "私聊" if sub.chat_type == "private" else "群聊"
            lines.append(
                f"{i}. {status} {codes}\n"
                f"   盘前 9:10 / 午盘前 12:40 / 盘后 18:00 · {chat_type_label}"
            )
            lines.append("")

        return BotResponse.markdown_response("\n".join(lines))


class TryAlertCommand(BotCommand):
    """
    立即试看信号分析结果

    用法：
        /tryalert 中兴通讯,比亚迪
        试看信号 中兴通讯,比亚迪
    """

    @property
    def name(self) -> str:
        return "tryalert"

    @property
    def aliases(self) -> List[str]:
        return ["试看信号", "试监控"]

    @property
    def description(self) -> str:
        return "立即试看信号分析结果（不订阅）"

    @property
    def usage(self) -> str:
        return "/tryalert <股票列表>"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        if not args:
            return BotResponse.markdown_response(
                "**用法：** `/tryalert 600519,hk00700`\n\n"
                "立即查看信号分析结果，不会自动订阅。"
            )

        raw_items = []
        for arg in args:
            for part in re.split(r'[,，、\s]+', arg):
                part = part.strip()
                if part:
                    raw_items.append(part)

        if not raw_items:
            return BotResponse.error_response("请输入至少一个股票代码")

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

        if len(codes) > MAX_STOCKS_PER_ALERT:
            return BotResponse.error_response(
                f"单次试看最多 {MAX_STOCKS_PER_ALERT} 只股票，当前 {len(codes)} 只"
            )

        try:
            from src.services.signal_alert_service import run_signal_analysis

            result = run_signal_analysis(codes)
        except Exception as e:
            logger.error(f"[TryAlertCommand] 分析失败: {e}")
            return BotResponse.error_response("分析失败，请稍后重试")

        if not result:
            return BotResponse.error_response("未能获取分析结果，请稍后重试")

        return BotResponse.markdown_response(result)

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
