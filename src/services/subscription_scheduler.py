# -*- coding: utf-8 -*-
"""
===================================
用户自选股订阅调度器
===================================

职责：
1. 后台线程每 60 秒轮询到期订阅
2. 交易日检查，过滤休市市场的股票
3. 逐个执行分析并通过飞书推送给订阅用户
4. 失败隔离：单个订阅失败不影响其他订阅
"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional, Dict, List, Set

logger = logging.getLogger(__name__)


class SubscriptionScheduler:
    """后台定时调度：按用户订阅配置推送分析报告"""

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # key = f"{user_id}:{chat_id}", value = today_str — 同一天同一订阅只推一次
        self._sent_today: Dict[str, str] = {}

    def start(self):
        """启动后台调度线程"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="subscription_scheduler"
        )
        self._thread.start()
        logger.info("[SubscriptionScheduler] 调度器已启动")

    def stop(self):
        """停止调度线程"""
        self._stop_event.set()

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error(f"[SubscriptionScheduler] tick 异常: {e}")
            self._stop_event.wait(60)

    def _tick(self):
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        hour = now.hour
        minute = now.minute

        # 清理昨天的去重记录
        expired_keys = [k for k, v in self._sent_today.items() if v != today_str]
        for k in expired_keys:
            del self._sent_today[k]

        # 查询到期订阅
        from src.repositories.subscription_repo import SubscriptionRepository

        repo = SubscriptionRepository()
        due_subs = repo.list_due(hour, minute)

        if not due_subs:
            return

        # 交易日检查
        from src.core.trading_calendar import get_open_markets_today

        open_markets: Set[str] = get_open_markets_today()
        if not open_markets:
            logger.info("[SubscriptionScheduler] 今日所有市场休市，跳过推送")
            for sub in due_subs:
                self._sent_today[f"{sub.user_id}:{sub.chat_id}"] = today_str
            return

        # 获取飞书客户端
        reply_client = self._get_feishu_client()
        if not reply_client:
            logger.warning("[SubscriptionScheduler] 飞书客户端不可用，跳过推送")
            return

        for sub in due_subs:
            dedup_key = f"{sub.user_id}:{sub.chat_id}"
            if self._sent_today.get(dedup_key) == today_str:
                continue
            try:
                self._process_subscription(sub, open_markets, reply_client)
                self._sent_today[dedup_key] = today_str
            except Exception as e:
                logger.error(
                    f"[SubscriptionScheduler] 处理订阅失败 "
                    f"user={sub.user_id} chat={sub.chat_id}: {e}"
                )

    def _process_subscription(self, sub, open_markets: Set[str], reply_client):
        """处理单个订阅：过滤股票 -> 分析 -> 推送"""
        from src.core.trading_calendar import get_market_for_stock

        all_codes = [c.strip() for c in sub.stock_codes.split(",") if c.strip()]

        # 过滤休市市场的股票
        active_codes: List[str] = []
        for code in all_codes:
            market = get_market_for_stock(code)
            if market is None or market in open_markets:
                active_codes.append(code)

        if not active_codes:
            logger.info(
                f"[SubscriptionScheduler] 订阅 user={sub.user_id} "
                f"chat={sub.chat_id} 的所有股票今日休市，跳过"
            )
            return

        logger.info(
            f"[SubscriptionScheduler] 开始处理订阅 user={sub.user_id} "
            f"chat={sub.chat_id} stocks={active_codes}"
        )

        # 运行分析流程
        report_type = getattr(sub, "report_type", "simple") or "simple"
        report = self._run_analysis(active_codes, report_type=report_type)
        if not report:
            logger.warning(
                f"[SubscriptionScheduler] 分析结果为空 "
                f"user={sub.user_id} chat={sub.chat_id}"
            )
            return

        # 发送推送
        if sub.chat_type == "group" and sub.message_id:
            success = reply_client.reply_text(
                sub.message_id, report, at_user=True, user_id=sub.user_id
            )
        else:
            success = reply_client.send_to_chat(sub.chat_id, report)

        if success:
            logger.info(
                f"[SubscriptionScheduler] 推送成功 "
                f"user={sub.user_id} chat={sub.chat_id}"
            )
        else:
            logger.warning(
                f"[SubscriptionScheduler] 推送失败 "
                f"user={sub.user_id} chat={sub.chat_id}"
            )

    def _run_analysis(self, stock_codes: List[str], report_type: str = "simple") -> Optional[str]:
        """运行分析流程并格式化报告"""
        try:
            from src.core.pipeline import StockAnalysisPipeline
            from src.enums import ReportType

            pipeline = StockAnalysisPipeline()
            results = pipeline.run(
                stock_codes=stock_codes,
                send_notification=False,
            )
            if not results:
                return None

            # 使用 notifier 的报告格式化能力生成对应类型的报告
            try:
                rt = ReportType.from_str(report_type)
                generator = getattr(pipeline.notifier, "generate_aggregate_report", None)
                if callable(generator):
                    return generator(results, rt)
            except Exception as e:
                logger.warning(f"[SubscriptionScheduler] 报告格式化降级为默认: {e}")

            return self._format_report(results)
        except Exception as e:
            logger.error(f"[SubscriptionScheduler] 分析流程失败: {e}")
            return None

    def _format_report(self, results) -> str:
        """将分析结果格式化为 Markdown 报告"""
        lines = [
            "# 📊 自选股分析推送",
            "",
            f"共分析 **{len(results)}** 只股票：",
            "",
        ]

        for idx, r in enumerate(results, 1):
            score = getattr(r, "sentiment_score", 0) or 0
            if score >= 80:
                level = "🟢 强烈买入"
            elif score >= 60:
                level = "🔵 建议买入"
            elif score >= 40:
                level = "🟡 观望持有"
            else:
                level = "🔴 建议卖出"

            name = getattr(r, "name", "") or getattr(r, "code", "")
            code = getattr(r, "code", "")
            advice = getattr(r, "operation_advice", "-")
            trend = getattr(r, "trend_prediction", "-")
            summary = (getattr(r, "analysis_summary", "") or "")[:80]

            lines.append(f"{idx}. **{name}** (`{code}`)")
            lines.append(
                f"   评分: {score} {level} | 操作: {advice} | 趋势: {trend}"
            )
            if summary:
                lines.append(f"   > {summary}...")
            lines.append("")

        lines.append("---")
        lines.append(f"⏰ 推送时间: {datetime.now().strftime('%H:%M')}")

        return "\n".join(lines)

    def _get_feishu_client(self):
        """获取飞书回复客户端"""
        try:
            from bot.platforms.feishu_stream import FeishuReplyClient, FEISHU_SDK_AVAILABLE
            if not FEISHU_SDK_AVAILABLE:
                return None

            from src.config import get_config
            config = get_config()
            app_id = getattr(config, "feishu_app_id", None)
            app_secret = getattr(config, "feishu_app_secret", None)
            if not app_id or not app_secret:
                return None

            return FeishuReplyClient(app_id, app_secret)
        except Exception as e:
            logger.warning(f"[SubscriptionScheduler] 创建飞书客户端失败: {e}")
            return None
