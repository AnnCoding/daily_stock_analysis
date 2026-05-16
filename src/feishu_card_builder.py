# -*- coding: utf-8 -*-
"""
飞书消息卡片模板构建模块

使用飞书卡片模板 (template_id) 发送结构化卡片。
Stream 模式不支持卡片 callback，因此 show_content 直接传全文。
"""

from typing import Any, Dict, Optional

TEMPLATE_ID = "AAqtkdJ4Hp1Iy"

# 信号关键词（用于从内容推断 header 颜色）
_SIGNAL_KEYWORDS = {
    'buy': ['买入', '加仓', '偏多', '强烈推荐', '建议买入', '看多'],
    'sell': ['卖出', '减仓', '偏空', '强烈建议卖出', '建议卖出', '看空'],
}

# header 颜色映射
_SIGNAL_COLOR_MAP = {
    'buy': 'green',
    'hold': 'indigo',
    'sell': 'red',
}


def detect_signal(content: str) -> str:
    """从内容中推断信号类型：buy / hold / sell"""
    buy_pos = len(content)
    sell_pos = len(content)

    for kw in _SIGNAL_KEYWORDS['buy']:
        pos = content.find(kw)
        if 0 <= pos < buy_pos:
            buy_pos = pos

    for kw in _SIGNAL_KEYWORDS['sell']:
        pos = content.find(kw)
        if 0 <= pos < sell_pos:
            sell_pos = pos

    if buy_pos < sell_pos and buy_pos < len(content):
        return 'buy'
    if sell_pos < buy_pos and sell_pos < len(content):
        return 'sell'
    return 'hold'


def build_template_card(content: str, title: Optional[str] = None,
                        signal: Optional[str] = None) -> Dict[str, Any]:
    """
    构建飞书卡片模板 payload（card 层，不含 msg_type）。

    Args:
        content: 经过 format_feishu_markdown 转换后的完整文本
        title: header 标题
        signal: buy / hold / sell，为 None 时自动推断

    Returns:
        卡片 dict，可直接作为 card 字段或 content JSON
    """
    if not signal:
        signal = detect_signal(content)

    return {
        "type": "template",
        "data": {
            "template_id": TEMPLATE_ID,
            "template_variable": {
                "title": title or "股票智能分析报告",
                "show_content": content,
                "header_color": _SIGNAL_COLOR_MAP.get(signal, 'indigo'),
            }
        }
    }
