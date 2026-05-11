# -*- coding: utf-8 -*-
"""
===================================
合并并发送今日分析报告
===================================

将今天分批分析的结果合并成一条消息发送到指定渠道

使用方式:
    python scripts/send_today_combined_report.py --channel feishu
    python scripts/send_today_combined_report.py --channel wechat --report-type simple
"""
import argparse
import logging
import os
import sys
from datetime import datetime, date
from typing import List, Optional
from dataclasses import dataclass, field
import json

# 添加项目根目录到 Python 路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from src.config import setup_env, get_config
setup_env()

from src.storage import DatabaseManager, AnalysisHistory
from src.notification import NotificationService
from src.analyzer import AnalysisResult


logger = logging.getLogger(__name__)


def history_to_result(history: AnalysisHistory) -> AnalysisResult:
    """
    将 AnalysisHistory 转换为 AnalysisResult

    Args:
        history: 数据库历史记录

    Returns:
        AnalysisResult 对象
    """
    # 解析 raw_result 中的 dashboard 数据
    dashboard = None
    if history.raw_result:
        try:
            raw_data = json.loads(history.raw_result)
            dashboard = raw_data.get('dashboard')
        except (json.JSONDecodeError, TypeError):
            pass

    # 如果 raw_result 中没有 dashboard，尝试从 context_snapshot 解析
    if not dashboard and history.context_snapshot:
        try:
            context = json.loads(history.context_snapshot) if isinstance(history.context_snapshot, str) else history.context_snapshot
            dashboard = context.get('dashboard')
        except (json.JSONDecodeError, TypeError):
            pass

    # 构造 AnalysisResult
    result = AnalysisResult(
        code=history.code,
        name=history.name or f"股票{history.code}",
        sentiment_score=history.sentiment_score or 50,
        trend_prediction=history.trend_prediction or "未知",
        operation_advice=history.operation_advice or "观望",
        analysis_summary=history.analysis_summary or "",
        dashboard=dashboard,
        # 设置决策类型
        decision_type=_get_decision_type(history.operation_advice),
    )

    # 补充狙击点位信息（如果数据库中有单独存储）
    if history.ideal_buy and (not dashboard or not dashboard.get('battle_plan', {}).get('sniper_points')):
        if not result.dashboard:
            result.dashboard = {}
        if 'battle_plan' not in result.dashboard:
            result.dashboard['battle_plan'] = {}
        if 'sniper_points' not in result.dashboard['battle_plan']:
            result.dashboard['battle_plan']['sniper_points'] = {}
        result.dashboard['battle_plan']['sniper_points']['ideal_buy'] = history.ideal_buy

    if history.stop_loss and result.dashboard and result.dashboard.get('battle_plan', {}).get('sniper_points'):
        result.dashboard['battle_plan']['sniper_points']['stop_loss'] = history.stop_loss

    if history.take_profit and result.dashboard and result.dashboard.get('battle_plan', {}).get('sniper_points'):
        result.dashboard['battle_plan']['sniper_points']['take_profit'] = history.take_profit

    return result


def _get_decision_type(operation_advice: Optional[str]) -> str:
    """将操作建议转换为决策类型"""
    if not operation_advice:
        return "hold"

    advice = operation_advice.lower()
    if advice in ["买入", "强烈买入", "加仓"]:
        return "buy"
    elif advice in ["卖出", "强烈卖出", "减仓"]:
        return "sell"
    else:
        return "hold"


def get_today_results(db: DatabaseManager, start_date: Optional[date] = None) -> List[AnalysisHistory]:
    """
    获取今天的分析结果

    Args:
        db: 数据库管理器
        start_date: 开始日期，默认为今天

    Returns:
        今日分析结果列表
    """
    if start_date is None:
        start_date = date.today()

    # 获取从今天开始的所有分析记录
    cutoff_date = datetime.combine(start_date, datetime.min.time())

    with db.get_session() as session:
        from sqlalchemy import select, desc
        from src.storage import AnalysisHistory

        results = session.execute(
            select(AnalysisHistory)
            .where(AnalysisHistory.created_at >= cutoff_date)
            .order_by(desc(AnalysisHistory.created_at))
        ).scalars().all()

        return list(results)


def generate_and_send_report(
    results: List[AnalysisHistory],
    channel: str = "feishu",
    report_type: str = "simple",
    test_mode: bool = False
) -> bool:
    """
    生成并发送合并报告

    Args:
        results: 分析历史记录列表
        channel: 发送渠道 (feishu/wechat/telegram/email)
        report_type: 报告类型 (simple/wechat/markdown/brief)
        test_mode: 测试模式，只打印不发送

    Returns:
        是否发送成功
    """
    if not results:
        logger.warning("没有找到任何分析记录")
        return False

    # 转换为 AnalysisResult
    analysis_results = [history_to_result(r) for r in results]

    logger.info(f"找到 {len(analysis_results)} 条分析记录")

    # 创建通知服务
    config = get_config()
    notifier = NotificationService(config)

    # 根据报告类型生成内容
    if report_type == "simple":
        content = notifier.generate_simple_dashboard(analysis_results)
    elif report_type == "wechat":
        content = notifier.generate_wechat_dashboard(analysis_results)
    elif report_type == "brief":
        content = notifier.generate_brief_report(analysis_results)
    else:  # markdown
        content = notifier.generate_aggregate_report(
            analysis_results,
            "markdown",
            datetime.now().strftime('%Y-%m-%d')
        )

    logger.info(f"生成报告内容长度: {len(content)} 字符")

    if test_mode:
        print("\n" + "="*60)
        print("【测试模式】报告内容：")
        print("="*60)
        print(content)
        print("="*60)
        return True

    # 发送通知
    success = False
    channel_upper = channel.upper()

    if channel_upper == "FEISHU":
        success = notifier.send_to_feishu(content)
    elif channel_upper == "WECHAT":
        success = notifier.send_to_wechat(content)
    elif channel_upper == "TELEGRAM":
        success = notifier.send_to_telegram(content)
    elif channel_upper == "EMAIL":
        success = notifier.send_to_email(content)
    else:
        logger.error(f"不支持的渠道: {channel}")
        return False

    if success:
        logger.info(f"报告发送成功到 {channel}")
    else:
        logger.error(f"报告发送失败到 {channel}")

    return success


def main():
    parser = argparse.ArgumentParser(description="合并并发送今日分析报告")
    parser.add_argument(
        "--channel",
        type=str,
        default="feishu",
        choices=["feishu", "wechat", "telegram", "email"],
        help="发送渠道 (默认: feishu)"
    )
    parser.add_argument(
        "--report-type",
        type=str,
        default="simple",
        choices=["simple", "wechat", "markdown", "brief"],
        help="报告类型 (默认: simple)"
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="指定日期 (YYYY-MM-DD)，默认为今天"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="测试模式，只打印不发送"
    )
    parser.add_argument(
        "--codes",
        type=str,
        default=None,
        help="只发送指定股票，逗号分隔 (如: 600519,300750)"
    )

    args = parser.parse_args()

    # 设置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # 解析日期
    query_date = date.today()
    if args.date:
        try:
            query_date = datetime.strptime(args.date, '%Y-%m-%d').date()
        except ValueError:
            logger.error(f"日期格式错误: {args.date}，应为 YYYY-MM-DD")
            return 1

    # 获取数据库实例
    db = DatabaseManager.get_instance()

    # 获取今日结果
    results = get_today_results(db, query_date)

    # 过滤指定股票
    if args.codes:
        code_list = [c.strip() for c in args.codes.split(',')]
        results = [r for r in results if r.code in code_list]
        logger.info(f"过滤后剩余 {len(results)} 条记录 (股票: {args.codes})")

    if not results:
        logger.warning(f"没有找到 {query_date} 的分析记录")
        return 1

    # 显示找到的股票
    logger.info(f"找到以下 {len(results)} 只股票的分析记录:")
    for r in results:
        logger.info(f"  - {r.code} {r.name} ({r.operation_advice or '未知'})")

    # 生成并发送报告
    success = generate_and_send_report(
        results=results,
        channel=args.channel,
        report_type=args.report_type,
        test_mode=args.test
    )

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
