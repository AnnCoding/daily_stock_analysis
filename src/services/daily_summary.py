# -*- coding: utf-8 -*-
"""
===================================
每日分析汇总服务
===================================

职责：
1. 收集当日通过 Bot 分析过的所有股票
2. 生成 Markdown 格式汇总报告
3. 定时（每天 17:00）推送汇总通知
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 汇总报告生成
# ---------------------------------------------------------------------------

def generate_daily_summary() -> Optional[str]:
    """
    生成今日分析汇总报告

    Returns:
        Markdown 格式的汇总文本，无记录时返回 None
    """
    from src.storage import get_db

    db = get_db()
    today = datetime.now().date()
    start = datetime.combine(today, datetime.min.time())
    end = datetime.combine(today, datetime.max.time())

    records = db.get_analysis_history(days=1, limit=100)
    # 只保留今天的记录
    records = [r for r in records if r.created_at and start <= r.created_at <= end]

    if not records:
        return None

    # 去重：同一股票取最新一条
    seen = {}
    for r in records:
        seen[r.code] = r  # records already sorted by created_at desc

    unique = list(seen.values())

    date_str = today.strftime('%Y-%m-%d')
    lines = [
        f"# 📊 每日分析汇总 ({date_str})",
        "",
        f"今日共分析 **{len(unique)}** 只股票：",
        "",
    ]

    for r in unique:
        score = r.sentiment_score or 0
        # 评分等级
        if score >= 80:
            level = "🟢 强烈买入"
        elif score >= 60:
            level = "🔵 建议买入"
        elif score >= 40:
            level = "🟡 观望持有"
        else:
            level = "🔴 建议卖出"

        advice = r.operation_advice or "-"
        trend = r.trend_prediction or "-"
        summary = (r.analysis_summary or "")[:80]

        lines.append(f"**{r.name or r.code}** (`{r.code}`)")
        lines.append(f"  评分: {score} {level} | 操作: {advice} | 趋势: {trend}")
        if summary:
            lines.append(f"  > {summary}...")
        lines.append("")

    lines.append("---")
    lines.append(f"⏰ 汇总时间: {datetime.now().strftime('%H:%M')}")

    return "\n".join(lines)


def send_daily_summary() -> bool:
    """
    生成并发送今日分析汇总

    Returns:
        是否发送成功
    """
    try:
        content = generate_daily_summary()
        if not content:
            logger.info("[DailySummary] 今日暂无分析记录，跳过汇总推送")
            return False

        from src.notification import NotificationService
        notifier = NotificationService()

        if not notifier.is_available():
            logger.warning("[DailySummary] 通知服务不可用，跳过推送")
            return False

        success = notifier.send(content, route_type="report")
        if success:
            logger.info("[DailySummary] 每日汇总推送成功")
        else:
            logger.warning("[DailySummary] 每日汇总推送失败")
        return success

    except Exception as e:
        logger.error(f"[DailySummary] 汇总推送异常: {e}")
        return False


# ---------------------------------------------------------------------------
# 定时调度器
# ---------------------------------------------------------------------------

class DailySummaryScheduler:
    """后台定时调度：每天指定时间推送分析汇总"""

    def __init__(self, hour: int = 17, minute: int = 0):
        self._hour = hour
        self._minute = minute
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_sent_date: Optional[str] = None

    def start(self):
        """启动后台调度线程"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="daily_summary")
        self._thread.start()
        logger.info(f"[DailySummary] 调度器已启动，将于每天 {self._hour:02d}:{self._minute:02d} 推送汇总")

    def stop(self):
        """停止调度线程"""
        self._stop_event.set()

    def _run_loop(self):
        while not self._stop_event.is_set():
            now = datetime.now()
            target_time = now.replace(hour=self._hour, minute=self._minute, second=0, microsecond=0)

            # 如果当前时间已过目标时间且今天还没发过
            today_str = now.strftime('%Y-%m-%d')
            if now >= target_time and self._last_sent_date != today_str:
                try:
                    send_daily_summary()
                    self._last_sent_date = today_str
                except Exception as e:
                    logger.error(f"[DailySummary] 定时推送失败: {e}")

            # 每分钟检查一次
            self._stop_event.wait(60)
