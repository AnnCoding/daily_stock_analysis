# -*- coding: utf-8 -*-
"""
信号监控服务 — 四策略综合投票（缠论/情绪周期/波浪理论/均线金叉）

固定触发时间：9:10（盘前）、12:40（午盘前）、18:00（盘后）
每只股票通过四策略独立分析后投票分类：触发买入点 / 触发卖出点 / 持续观察中

推送策略：
- 群聊：同一群内所有用户的监控股票合并去重，合并为一条消息推送
- 私聊：每个用户独立推送
"""

import json
import logging
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional, Set

from src.core.trading_calendar import get_open_markets_today
from src.repositories.alert_subscription_repo import AlertSubscriptionRepo

logger = logging.getLogger(__name__)

ALERT_SLOTS = [(9, 10), (12, 40), (18, 0)]
_POLL_INTERVAL = 60
_MAX_STOCKS_PER_CHECK = 30

_STRATEGY_SKILL_NAMES = ["chan_theory", "emotion_cycle", "wave_theory", "ma_golden_cross"]

_STRATEGY_META = {
    "chan_theory":     {"emoji": "🌀", "label": "缠论"},
    "emotion_cycle":   {"emoji": "💰", "label": "情绪"},
    "wave_theory":     {"emoji": "🌊", "label": "波浪"},
    "ma_golden_cross": {"emoji": "📊", "label": "均线"},
}


def _load_strategy_instructions() -> Dict[str, str]:
    """Load instructions from the strategy YAML files."""
    from src.agent.skills.base import SkillManager

    mgr = SkillManager()
    mgr.load_builtin_skills()
    result = {}
    for name in _STRATEGY_SKILL_NAMES:
        skill = mgr.get(name)
        if skill and skill.instructions:
            result[name] = skill.instructions
        else:
            logger.warning("[SignalAlert] 策略 %s 未找到或无 instructions", name)
            result[name] = f"(策略 {name} 加载失败)"
    return result


_SYSTEM_PROMPT_TEMPLATE = """你是四策略联合分析师，需要对股票进行独立的多视角分析。

## 策略一：缠论
{chan_theory}

## 策略二：情绪周期
{emotion_cycle}

## 策略三：波浪理论
{wave_theory}

## 策略四：均线金叉
{ma_golden_cross}

## 输出要求

对每只股票输出严格 JSON（不要 markdown 代码块），格式如下：
{{
  "stocks": [
    {{
      "code": "000063",
      "name": "中兴通讯",
      "chan_theory": {{
        "signal": "buy",
        "reason": "离开下跌中枢后回踩不破高点，二买成立",
        "buy_price": 37.5,
        "sell_price": null
      }},
      "emotion_cycle": {{
        "signal": "hold",
        "reason": "换手率正常，情绪平稳",
        "buy_price": null,
        "sell_price": null
      }},
      "wave_theory": {{
        "signal": "buy",
        "reason": "处于第2浪回调末端，接近38.2%回撤位",
        "buy_price": 37.2,
        "sell_price": null
      }},
      "ma_golden_cross": {{
        "signal": "buy",
        "reason": "MA5上穿MA10，量能配合金叉确认",
        "buy_price": 37.8,
        "sell_price": null
      }}
    }}
  ]
}}

signal 取值: buy / hold / sell
buy_price: 建议买入价位（如有买入信号或可给出关注买入价时填写，否则 null）
sell_price: 建议卖出价位（如有卖出信号或可给出关注卖出价时填写，否则 null）
对于 hold 的策略，如果有明确的等待买入/卖出价位，也请填入 buy_price 或 sell_price。"""


_SLOT_LABELS = {
    "0910": "盘前",
    "1240": "午盘前",
    "1800": "盘后",
}


def run_signal_analysis(codes: List[str]) -> Optional[str]:
    """Run one-shot signal analysis and return formatted message."""
    instructions = _load_strategy_instructions()
    now = datetime.now()
    slot = f"{now.hour:02d}{now.minute:02d}"

    stock_data = _fetch_stock_data(codes[:_MAX_STOCKS_PER_CHECK])
    if not stock_data:
        return None

    strategy_results = _run_strategy_analysis(stock_data, instructions)
    if not strategy_results:
        return None

    grouped = _vote_and_group(strategy_results)
    return _format_message(grouped, slot)


def _fetch_stock_data(codes: List[str]) -> List[Dict]:
    """Fetch realtime quotes and technical data for all stocks."""
    from data_provider.base import DataFetcherManager

    mgr = DataFetcherManager()
    results = []
    for code in codes:
        try:
            quote = mgr.get_realtime_quote(code)
            stock_name = getattr(quote, "name", "") or code
            price = getattr(quote, "price", None) or 0

            trend = None
            try:
                df, _ = mgr.get_daily_data(code, days=60)
                if df is not None and not df.empty:
                    from src.stock_analyzer import StockTrendAnalyzer
                    analyzer = StockTrendAnalyzer()
                    trend = analyzer.analyze(df, code)
            except Exception as e:
                logger.debug("[SignalAlert] 获取 %s 趋势数据失败: %s", code, e)

            results.append({
                "code": code,
                "name": stock_name,
                "price": price,
                "trend": trend,
            })
        except Exception as e:
            logger.warning("[SignalAlert] 获取 %s 数据失败: %s", code, e)
            results.append({
                "code": code,
                "name": code,
                "price": 0,
                "trend": None,
            })
    return results


def _run_strategy_analysis(stock_data: List[Dict],
                           instructions: Optional[Dict[str, str]] = None) -> List[Dict]:
    """Run four-strategy LLM analysis for all stocks."""
    from src.agent.llm_adapter import LLMToolAdapter

    if instructions is None:
        instructions = _load_strategy_instructions()
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        chan_theory=instructions.get("chan_theory", "(未加载)"),
        emotion_cycle=instructions.get("emotion_cycle", "(未加载)"),
        wave_theory=instructions.get("wave_theory", "(未加载)"),
        ma_golden_cross=instructions.get("ma_golden_cross", "(未加载)"),
    )

    stock_descriptions = []
    for s in stock_data:
        trend = s.get("trend")
        trend_info = ""
        if trend:
            trend_info = (
                f"  趋势: {getattr(trend, 'trend_status', 'N/A')}, "
                f"信号: {getattr(trend, 'buy_signal', 'N/A')}, "
                f"评分: {getattr(trend, 'signal_score', 'N/A')}/100, "
                f"支撑位: {getattr(trend, 'support_levels', [])}, "
                f"阻力位: {getattr(trend, 'resistance_levels', [])}, "
                f"MACD: {getattr(trend, 'macd_status', 'N/A')}, "
                f"RSI: {getattr(trend, 'rsi_status', 'N/A')}"
            )
        stock_descriptions.append(
            f"- {s['name']}({s['code']}): 现价 {s['price']}\n{trend_info}"
        )

    user_msg = "请分析以下股票：\n" + "\n".join(stock_descriptions)

    try:
        adapter = LLMToolAdapter()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]
        response = adapter.call_text(messages)
        content = response.content if response else ""
        return _parse_strategy_response(content, stock_data)
    except Exception as e:
        logger.error("[SignalAlert] LLM 分析失败: %s", e)
        return []


def _parse_strategy_response(content: str, stock_data: List[Dict]) -> List[Dict]:
    """Parse LLM JSON response into structured results."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.debug("[SignalAlert] JSON 解析失败: %s", text[:200])
        return []

    # Build price lookup: exact code match, then suffix match (e.g. "SZ300408")
    price_map = {}
    for s in stock_data:
        price_map[s["code"]] = s["price"]
        if s["code"].isdigit():
            for suffix_len in (4, 5, 6):
                price_map[s["code"][-suffix_len:]] = s["price"]

    stocks = parsed.get("stocks", [])
    results = []
    for item in stocks:
        code = item.get("code", "")
        price = price_map.get(code, 0)
        # If still no price, try matching by name
        if not price:
            name = item.get("name", "")
            for s in stock_data:
                if name and s["name"] == name:
                    price = s["price"]
                    break
        results.append({
            "code": code,
            "name": item.get("name", ""),
            "price": price,
            "chan_theory": item.get("chan_theory", {}),
            "emotion_cycle": item.get("emotion_cycle", {}),
            "wave_theory": item.get("wave_theory", {}),
            "ma_golden_cross": item.get("ma_golden_cross", {}),
        })
    return results


def _vote_and_group(strategy_results: List[Dict]) -> Dict[str, List[Dict]]:
    """Four-strategy voting: ≥2 buy → buy, ≥2 sell → sell, else observe."""
    buy_stocks = []
    sell_stocks = []
    observe_stocks = []

    for item in strategy_results:
        signals = [
            item.get("chan_theory", {}).get("signal", "hold"),
            item.get("emotion_cycle", {}).get("signal", "hold"),
            item.get("wave_theory", {}).get("signal", "hold"),
            item.get("ma_golden_cross", {}).get("signal", "hold"),
        ]
        buy_count = sum(1 for s in signals if s == "buy")
        sell_count = sum(1 for s in signals if s == "sell")

        if buy_count >= 2:
            buy_stocks.append(item)
        elif sell_count >= 2:
            sell_stocks.append(item)
        else:
            observe_stocks.append(item)

    return {"buy": buy_stocks, "sell": sell_stocks, "observe": observe_stocks}


def _format_message(grouped: Dict[str, List[Dict]], slot: str) -> str:
    slot_label = _SLOT_LABELS.get(slot, "即时")
    time_label = f"{slot[:2]}:{slot[2:]}" if len(slot) == 4 else slot
    lines = [f"📊 信号监控（{time_label} {slot_label}）", ""]

    buy = grouped.get("buy", [])
    sell = grouped.get("sell", [])
    observe = grouped.get("observe", [])
    all_items = buy + sell + observe

    if not all_items:
        lines.append("暂无监控数据")
        lines.append("")
        return "\n".join(lines)

    # === Part 1: Summary — grouped by signal, stock + price only ===
    lines.append("━━ 一、信号总览 ━━")
    lines.append("")

    if buy:
        lines.append(f"🟢 触发买入点（{len(buy)} 只）")
        for item in buy:
            _format_summary_line(lines, item, "buy")
        lines.append("")

    if sell:
        lines.append(f"🔴 触发卖出点（{len(sell)} 只）")
        for item in sell:
            _format_summary_line(lines, item, "sell")
        lines.append("")

    if observe:
        lines.append(f"🟡 持续观察中（{len(observe)} 只）")
        for item in observe:
            _format_summary_line(lines, item, "observe")
        lines.append("")

    # === Part 2: Detail — per-stock strategy breakdown ===
    lines.append("━━ 二、策略详情 ━━")
    lines.append("")

    for item in buy:
        _format_stock_detail(lines, item, "buy")
    for item in sell:
        _format_stock_detail(lines, item, "sell")
    for item in observe:
        _format_stock_detail(lines, item, "observe")

    strategy_footer = " × ".join(
        f"{m['emoji']}{m['label']}" for m in _STRATEGY_META.values()
    )
    lines.append("---")
    lines.append(f"策略：{strategy_footer}（四策略投票）")
    return "\n".join(lines)


def _fmt_price(value) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.2f}"
    except (ValueError, TypeError):
        return str(value)


def _format_summary_line(lines: List[str], item: Dict, group: str):
    """Part 1: stock name + current price only."""
    name = item.get("name", "")
    code = item.get("code", "")
    price = item.get("price", 0)
    price_str = f"{price:.2f}" if price else "—"

    strategies = {k: item.get(k, {}) for k in _STRATEGY_META}
    signals = {k: strategies.get(k, {}).get("signal", "hold") for k in _STRATEGY_META}
    buy_count = sum(1 for v in signals.values() if v == "buy")
    sell_count = sum(1 for v in signals.values() if v == "sell")

    if group == "buy":
        buy_prices = [_fmt_price(strategies[k].get("buy_price"))
                      for k in _STRATEGY_META
                      if strategies.get(k, {}).get("buy_price") is not None]
        target = f" → ≤ {buy_prices[0]}" if buy_prices else ""
        lines.append(f"  📈 **{name}**({code}) 现价 {price_str}{target}  [{buy_count}/4看多]")
    elif group == "sell":
        sell_prices = [_fmt_price(strategies[k].get("sell_price"))
                       for k in _STRATEGY_META
                       if strategies.get(k, {}).get("sell_price") is not None]
        target = f" → ≥ {sell_prices[0]}" if sell_prices else ""
        lines.append(f"  📉 **{name}**({code}) 现价 {price_str}{target}  [{sell_count}/4看空]")
    else:
        all_buy_p = [_fmt_price(strategies[k].get("buy_price"))
                     for k in _STRATEGY_META
                     if strategies.get(k, {}).get("buy_price") is not None]
        all_sell_p = [_fmt_price(strategies[k].get("sell_price"))
                      for k in _STRATEGY_META
                      if strategies.get(k, {}).get("sell_price") is not None]
        hints = []
        if all_buy_p:
            hints.append(f"买≤{all_buy_p[0]}")
        if all_sell_p:
            hints.append(f"卖≥{all_sell_p[0]}")
        hint_str = " | ".join(hints)
        target = f" → {hint_str}" if hint_str else ""
        lines.append(f"  📊 **{name}**({code}) 现价 {price_str}{target}")


def _format_stock_detail(lines: List[str], item: Dict, group: str):
    """Part 2: per-stock strategy breakdown with reasons and prices."""
    name = item.get("name", "")
    code = item.get("code", "")
    price = item.get("price", 0)
    price_str = f"{price:.2f}" if price else "—"

    group_emoji = {"buy": "📈", "sell": "📉", "observe": "📊"}.get(group, "•")
    group_label = {"buy": "买入", "sell": "卖出", "observe": "观望"}.get(group, "")
    lines.append(f"{group_emoji} **{name}**({code}) 现价 {price_str}  [{group_label}]")
    lines.append("")

    strategies = {k: item.get(k, {}) for k in _STRATEGY_META}

    for key, meta in _STRATEGY_META.items():
        s = strategies.get(key, {})
        reason = s.get("reason", "")
        if not reason:
            continue

        buy_p = s.get("buy_price")
        sell_p = s.get("sell_price")
        price_hint = ""
        if buy_p is not None:
            price_hint = f" | 买入 ≤ {_fmt_price(buy_p)}"
        if sell_p is not None:
            price_hint += f" | 卖出 ≥ {_fmt_price(sell_p)}"

        lines.append(f"  {meta['emoji']} {meta['label']}：{reason}{price_hint}")

    # Consensus line
    signals = {k: strategies.get(k, {}).get("signal", "hold") for k in _STRATEGY_META}
    buy_count = sum(1 for v in signals.values() if v == "buy")
    sell_count = sum(1 for v in signals.values() if v == "sell")

    if group == "buy":
        lines.append(f"  → {buy_count}/4 看多")
    elif group == "sell":
        lines.append(f"  → {sell_count}/4 看空")
    else:
        lines.append(f"  → 等待共识信号")

    lines.append("")


def _push_to_chat(chat_id: str, chat_type: str,
                  message_id: Optional[str], message: str):
    """Push alert message via Feishu Stream."""
    try:
        from bot.platforms.feishu_stream import FeishuReplyClient

        client = FeishuReplyClient()
        if chat_type == "group" and message_id:
            success = client.reply_text(message_id, message, at_user=False)
        else:
            success = client.send_to_chat(chat_id, message)

        if success:
            logger.info("[SignalAlert] 推送成功: chat=%s type=%s", chat_id, chat_type)
        else:
            logger.warning("[SignalAlert] 推送失败: chat=%s type=%s", chat_id, chat_type)
    except Exception as e:
        logger.error("[SignalAlert] 推送异常: %s", e)


class SignalAlertScheduler:
    """信号监控调度器 — 后台线程，固定时间触发"""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._sent_today: Dict[str, str] = {}
        self._strategy_instructions: Optional[Dict[str, str]] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop, name="signal_alert_scheduler", daemon=True
        )
        self._thread.start()
        logger.info("[SignalAlert] 调度器已启动，触发时间: %s",
                     ", ".join(f"{h:02d}:{m:02d}" for h, m in ALERT_SLOTS))

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self._check_and_fire()
            except Exception as e:
                logger.error("[SignalAlert] 调度循环异常: %s", e)
            self._stop_event.wait(_POLL_INTERVAL)

    def _check_and_fire(self):
        now = datetime.now()
        current_slot = None
        for hour, minute in ALERT_SLOTS:
            if now.hour == hour and now.minute == minute:
                current_slot = f"{hour:02d}{minute:02d}"
                break
        if current_slot is None:
            return

        open_markets = get_open_markets_today()
        if not open_markets:
            return

        repo = AlertSubscriptionRepo()
        subs = repo.list_all_active()
        if not subs:
            return

        today_str = now.strftime("%Y-%m-%d")

        if self._strategy_instructions is None:
            self._strategy_instructions = _load_strategy_instructions()

        group_subs: Dict[str, List] = {}
        private_subs: List = []

        for sub in subs:
            if sub.chat_type == "group":
                group_subs.setdefault(sub.chat_id, []).append(sub)
            else:
                private_subs.append(sub)

        for chat_id, members in group_subs.items():
            dedup_key = f"group:{chat_id}:{current_slot}"
            if self._sent_today.get(dedup_key) == today_str:
                continue
            try:
                self._process_group(members, current_slot)
                self._sent_today[dedup_key] = today_str
            except Exception as e:
                logger.error("[SignalAlert] 处理群组 %s 失败: %s", chat_id, e)

        for sub in private_subs:
            dedup_key = f"private:{sub.user_id}:{sub.chat_id}:{current_slot}"
            if self._sent_today.get(dedup_key) == today_str:
                continue
            try:
                self._process_single(sub, current_slot)
                self._sent_today[dedup_key] = today_str
            except Exception as e:
                logger.error("[SignalAlert] 处理私聊 %s/%s 失败: %s",
                             sub.user_id, sub.chat_id, e)

        expired = [k for k, v in self._sent_today.items() if v != today_str]
        for k in expired:
            del self._sent_today[k]

    def _process_group(self, members: List, slot: str):
        """Merge all stocks from all users in the same group, push one message."""
        merged_codes = OrderedDict()
        message_id = None
        for sub in members:
            for c in (sub.stock_codes or "").split(","):
                c = c.strip()
                if c and c not in merged_codes:
                    merged_codes[c] = sub.user_id
            if sub.message_id:
                message_id = sub.message_id

        codes = list(merged_codes.keys())[:_MAX_STOCKS_PER_CHECK]
        if not codes:
            return

        stock_data = _fetch_stock_data(codes)
        if not stock_data:
            logger.warning("[SignalAlert] 群组无有效数据，跳过推送")
            return

        strategy_results = _run_strategy_analysis(stock_data, self._strategy_instructions)
        if not strategy_results:
            return

        grouped = _vote_and_group(strategy_results)
        message = _format_message(grouped, slot)

        _push_to_chat(members[0].chat_id, "group", message_id, message)

    def _process_single(self, sub, slot: str):
        """Process a single private subscription independently."""
        codes = [c.strip() for c in (sub.stock_codes or "").split(",") if c.strip()]
        if not codes:
            return

        stock_data = _fetch_stock_data(codes[:_MAX_STOCKS_PER_CHECK])
        if not stock_data:
            logger.warning("[SignalAlert] 无有效数据，跳过推送")
            return

        strategy_results = _run_strategy_analysis(stock_data, self._strategy_instructions)
        if not strategy_results:
            return

        grouped = _vote_and_group(strategy_results)
        message = _format_message(grouped, slot)

        _push_to_chat(sub.chat_id, sub.chat_type, sub.message_id, message)
