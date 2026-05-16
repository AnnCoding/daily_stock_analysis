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
# 市场行情区块
# ---------------------------------------------------------------------------

def _build_market_section(unique: list) -> list:
    """
    构建市场行情和风险提示区块。

    Args:
        unique: 已去重的分析记录列表（已排序）

    Returns:
        Markdown 行列表；获取失败时仅跳过行情，仍返回风险提示
    """
    indices = None
    stats = None

    try:
        from data_provider.base import DataFetcherManager

        manager = DataFetcherManager()
        indices = manager.get_main_indices(region="cn")
        stats = manager.get_market_stats()
    except Exception as e:
        logger.warning(f"[DailySummary] 获取市场行情异常: {e}")

    lines = []

    # --- 指数行情 ---
    if indices:
        lines.append("## 📈 市场行情")
        for idx in indices:
            name = idx.get("name", "")
            current = idx.get("current", 0)
            change_pct = idx.get("change_pct", 0)
            direction = "↑" if change_pct > 0 else "↓" if change_pct < 0 else "-"
            lines.append(f"- {name}: {current:,.2f} ({direction}{abs(change_pct):.2f}%)")
    else:
        logger.info("[DailySummary] 未获取到指数行情数据")

    # --- 涨跌家数 + 市场温度 ---
    if stats:
        up = stats.get("up_count", 0)
        down = stats.get("down_count", 0)
        limit_up = stats.get("limit_up_count", 0)
        limit_down = stats.get("limit_down_count", 0)
        lines.append(
            f"- 涨跌家数: {up}涨/{down}跌 | 涨停 {limit_up} | 跌停 {limit_down}"
        )

        # --- 市场温度 ---
        if indices and up + down > 0:
            participants = up + down
            breadth_score = int(up / participants * 100)

            index_changes = [
                i.get("change_pct") for i in indices if i.get("change_pct") is not None
            ]
            index_score = 50
            if index_changes:
                avg_change = sum(index_changes) / len(index_changes)
                index_score = int(max(0, min(100, 50 + avg_change * 12)))

            limit_total = limit_up + limit_down
            limit_score = 50
            if limit_total:
                limit_score = int(limit_up / limit_total * 100)

            score = int(
                round(breadth_score * 0.45 + index_score * 0.35 + limit_score * 0.20)
            )
            if score >= 70:
                label = "强势"
            elif score >= 55:
                label = "偏暖"
            elif score >= 40:
                label = "震荡"
            else:
                label = "偏弱"

            lines.append(f"- 市场温度: {score} 分 ({label})")
    else:
        logger.info("[DailySummary] 未获取到市场统计数据")

    # --- 风险提示（始终计算，不依赖市场数据） ---
    low_score_count = sum(1 for r in unique if (r.sentiment_score or 0) < 40)
    if low_score_count > 0:
        lines.append("")
        lines.append("## ⚠️ 风险提示")
        lines.append(f"- 低评分股票 {low_score_count} 只（评分<40），需重点关注")

    return lines


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

    # 按评分降序排序
    unique.sort(key=lambda r: r.sentiment_score or 0, reverse=True)

    # 排序日志：输出前几只股票的评分，便于排查
    top_scores = [(r.name or r.code, r.sentiment_score) for r in unique[:5]]
    logger.info(f"[DailySummary] 排序后前5: {top_scores}")

    date_str = today.strftime('%Y-%m-%d')
    lines = [
        f"# 📊 每日分析汇总 ({date_str})",
        "",
        f"今日共分析 **{len(unique)}** 只股票：",
        "",
    ]

    # 市场行情区块
    market_section = _build_market_section(unique)
    if market_section:
        lines.extend(market_section)
        lines.append("")

    for idx, r in enumerate(unique, 1):
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

        lines.append(f"{idx}. **{r.name or r.code}** (`{r.code}`)")
        lines.append(f"   评分: {score} {level} | 操作: {advice} | 趋势: {trend}")
        if summary:
            lines.append(f"   > {summary}...")
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
                # 非交易日跳过
                from src.core.trading_calendar import get_open_markets_today
                open_markets = get_open_markets_today()
                if not open_markets:
                    logger.info("[DailySummary] 今日所有市场休市，跳过汇总推送")
                    self._last_sent_date = today_str
                    self._stop_event.wait(60)
                    continue
                try:
                    send_daily_summary()
                    self._last_sent_date = today_str
                except Exception as e:
                    logger.error(f"[DailySummary] 定时推送失败: {e}")

            # 每分钟检查一次
            self._stop_event.wait(60)
