# -*- coding: utf-8 -*-
"""
===================================
股票分析命令
===================================

分析指定股票，调用 AI 生成分析报告。
"""

import re
import logging
from typing import List, Optional

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse
from data_provider.base import canonical_stock_code

logger = logging.getLogger(__name__)


class AnalyzeCommand(BotCommand):
    """
    股票分析命令

    分析指定股票代码，生成 AI 分析报告并推送。

    用法：
        /analyze 600519              - 分析贵州茅台（精简报告）
        /analyze 比亚迪              - 支持股票名称
        /analyze 600519 比亚迪 300750 - 支持多个股票
        /analyze 600519 full          - 分析并生成完整报告
    """

    @property
    def name(self) -> str:
        return "analyze"

    @property
    def aliases(self) -> List[str]:
        return ["a", "分析", "查"]

    @property
    def description(self) -> str:
        return "分析指定股票或基金（基金代码需加 JJ 前缀，如 JJ023408）"

    @property
    def usage(self) -> str:
        return "/analyze <股票代码|JJ基金代码|名称> [股票2] [full]"

    def validate_args(self, args: List[str]) -> Optional[str]:
        """验证参数"""
        if not args:
            return "请输入股票代码或 JJ+基金代码"

        return None

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行分析命令"""
        # 将所有参数按空格/逗号/顿号拆分
        raw_items = []
        for arg in args:
            for part in re.split(r'[,，、\s]+', arg):
                part = part.strip()
                if part:
                    raw_items.append(part)

        # 分离报告类型参数
        report_type = "simple"
        type_keywords = ["full", "完整", "详细"]
        stock_items = []
        for item in raw_items:
            if item.lower() in type_keywords:
                report_type = "full"
            else:
                stock_items.append(item)

        if not stock_items:
            return BotResponse.error_response("请输入股票代码或 JJ+基金代码")

        # 解析所有股票，同时收集用户问题文本
        codes = []
        failed = []
        question_parts = []
        for item in stock_items:
            code = self._resolve_stock_code(item)
            if code:
                codes.append(code)
            else:
                # 包含中文且不像代码的，视为用户问题
                if re.search(r'[\u4e00-\u9fff]', item):
                    question_parts.append(item)
                else:
                    failed.append(item)

        if not codes:
            return BotResponse.error_response(
                f"无法识别: {', '.join(failed)}\n"
                f"请输入股票代码（如 600519）或 JJ+基金代码（如 JJ023408）"
            )

        # 拼接用户问题
        user_question = '，'.join(question_parts) if question_parts else None

        logger.info(f"[AnalyzeCommand] 分析股票: {codes}, 报告类型: {report_type}, 用户问题: {user_question}")

        try:
            from src.services.task_service import get_task_service
            from src.enums import ReportType

            service = get_task_service()

            # 提交分析任务
            success_codes = []
            for code in codes:
                result = service.submit_analysis(
                    code=code,
                    report_type=ReportType.from_str(report_type),
                    source_message=message,
                    user_question=user_question
                )
                if result.get("success"):
                    success_codes.append(code)
                else:
                    failed.append(code)

            if success_codes:
                logger.info(f"[AnalyzeCommand] 分析任务已提交: {success_codes}")
                return BotResponse(
                    text="",
                    markdown=True,
                    reaction_only=True,
                    at_user=False,
                )
            else:
                return BotResponse.error_response("提交分析任务失败")

        except Exception as e:
            logger.error(f"[AnalyzeCommand] 执行失败: {e}")
            return BotResponse.error_response(f"分析失败: {str(e)[:100]}")

    def _resolve_stock_code(self, raw_input: str) -> Optional[str]:
        """解析股票代码或名称为标准代码"""
        from src.services.name_to_code_resolver import resolve_name_to_code

        # 先尝试直接作为代码规范化
        code = canonical_stock_code(raw_input)
        if code:
            upper = code.upper()
            if re.match(r'^\d{6}$', upper) or \
               re.match(r'^JJ\d{6}$', upper) or \
               re.match(r'^HK\d{5}$', upper) or \
               re.match(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$', upper):
                return code

        # 尝试剥离前导非数字非字母字符（处理"下300001"等粘连情况）
        stripped = re.sub(r'^[^\da-zA-Z]+', '', raw_input)
        if stripped and stripped != raw_input:
            code = canonical_stock_code(stripped)
            if code:
                upper = code.upper()
                if re.match(r'^\d{6}$', upper) or \
                   re.match(r'^JJ\d{6}$', upper) or \
                   re.match(r'^HK\d{5}$', upper) or \
                   re.match(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$', upper):
                    return code

        # 使用名称解析器（支持名称、拼音、模糊匹配）
        resolved = resolve_name_to_code(raw_input)
        if resolved:
            return canonical_stock_code(resolved)

        return None
