# -*- coding: utf-8 -*-
"""
===================================
Name-to-Code Resolution Engine
===================================

Resolve stock name to code: local mapping + pinyin + AkShare fallback + fuzzy matching.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import time
from typing import Dict, Optional, Set, Tuple

from src.data.stock_mapping import STOCK_NAME_MAP
from src.services.stock_code_utils import is_code_like, normalize_code

logger = logging.getLogger(__name__)

# AkShare result cache: (timestamp, name_to_code_dict)
_akshare_cache: Optional[tuple[float, Dict[str, str]]] = None
_AKSHARE_CACHE_TTL = 1800  # 30 MIN

# Local file cache path for persisting AkShare mappings across restarts
_AKSHARE_LOCAL_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "akshare_name_cache.json",
)

# AkShare HK stock cache: (timestamp, name_to_code_dict)
_akshare_hk_cache: Optional[tuple[float, Dict[str, str]]] = None
_AKSHARE_HK_CACHE_TTL = 1800  # 30 MIN
_AKSHARE_HK_LOCAL_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "akshare_hk_name_cache.json",
)

# AkShare fund name cache: (timestamp, name_to_code_dict)
_akshare_fund_cache: Optional[tuple[float, Dict[str, str]]] = None
_AKSHARE_FUND_CACHE_TTL = 3600  # 60 MIN


def _contains_cjk(text: str) -> bool:
    """Return True when text contains CJK characters."""
    return any("\u3400" <= ch <= "\u9fff" for ch in text)


def _is_code_like(s: str) -> bool:
    """Backward-compatible wrapper of shared code-like check."""
    return is_code_like(s)


def _normalize_code(raw: str) -> Optional[str]:
    """Backward-compatible wrapper of shared code normalization."""
    return normalize_code(raw)


def _build_reverse_map_no_duplicates(
    code_to_name: Dict[str, str],
) -> Dict[str, str]:
    """
    Build name -> code map. If a name maps to multiple codes (ambiguous), exclude it.
    """
    name_to_codes: Dict[str, Set[str]] = {}
    for code, name in code_to_name.items():
        if not name or not code:
            continue
        name = name.strip()
        if name not in name_to_codes:
            name_to_codes[name] = set()
        name_to_codes[name].add(code)
    # Only include names with exactly one code
    return {name: next(iter(codes)) for name, codes in name_to_codes.items() if len(codes) == 1}


def _build_local_name_indexes(code_to_name: Dict[str, str]) -> Tuple[Dict[str, str], Set[str]]:
    """
    Build cached local lookup structures:
    - unique name -> code
    - ambiguous names that should fail fast
    """
    name_to_codes: Dict[str, Set[str]] = {}
    for code, name in code_to_name.items():
        if not name or not code:
            continue
        normalized_name = name.strip()
        if not normalized_name:
            continue
        name_to_codes.setdefault(normalized_name, set()).add(code)

    unique_names = {
        name: next(iter(codes))
        for name, codes in name_to_codes.items()
        if len(codes) == 1
    }
    ambiguous_names = {
        name
        for name, codes in name_to_codes.items()
        if len(codes) > 1
    }
    return unique_names, ambiguous_names


_LOCAL_REVERSE_MAP, _LOCAL_AMBIGUOUS_NAMES = _build_local_name_indexes(STOCK_NAME_MAP)


def _get_akshare_name_to_code() -> Optional[Dict[str, str]]:
    """Fetch A-share name->code from AkShare, with in-memory + file cache."""
    global _akshare_cache
    now = time.time()
    if _akshare_cache is not None and (now - _akshare_cache[0]) < _AKSHARE_CACHE_TTL:
        return _akshare_cache[1]

    # Try AkShare online first
    try:
        import akshare as ak

        df = ak.stock_info_a_code_name()
        if df is not None and not df.empty:
            code_to_name = {}
            for _, row in df.iterrows():
                code = row.get("code")
                name = row.get("name")
                if code is None or name is None:
                    continue
                code_str = str(code).strip()
                if "." in code_str:
                    base, suffix = code_str.rsplit(".", 1)
                    if suffix.upper() in ("SH", "SZ", "SS") and base.isdigit():
                        code_str = base
                code_to_name[code_str] = str(name).strip()
            result = _build_reverse_map_no_duplicates(code_to_name)
            _akshare_cache = (now, result)
            logger.info(f"[NameResolver] AkShare cache loaded: {len(result)} name->code mappings")
            # Persist to file for future restarts
            _save_akshare_local_cache(result)
            return result
    except Exception as e:
        logger.warning(f"[NameResolver] AkShare fallback failed: {e}")

    # Fallback: load from local file cache
    cached = _load_akshare_local_cache()
    if cached:
        _akshare_cache = (now, cached)
        logger.info(f"[NameResolver] Using local file cache: {len(cached)} name->code mappings")
        return cached
    return None


def _get_akshare_hk_name_to_code() -> Optional[Dict[str, str]]:
    """Fetch HK stock name->code from AkShare, with in-memory + file cache."""
    global _akshare_hk_cache
    now = time.time()
    if _akshare_hk_cache is not None and (now - _akshare_hk_cache[0]) < _AKSHARE_HK_CACHE_TTL:
        return _akshare_hk_cache[1]

    try:
        import akshare as ak

        df = ak.stock_hk_spot_em()
        if df is not None and not df.empty:
            code_to_name = {}
            for _, row in df.iterrows():
                code = str(row.get("代码", "")).strip()
                name = str(row.get("名称", "")).strip()
                if not code or not name:
                    continue
                code = code.zfill(5)
                code_to_name[code] = name
            result = _build_reverse_map_no_duplicates(code_to_name)
            _akshare_hk_cache = (now, result)
            logger.info(f"[NameResolver] AkShare HK cache loaded: {len(result)} name->code mappings")
            _save_cache_json(_AKSHARE_HK_LOCAL_CACHE_PATH, result)
            return result
    except Exception as e:
        logger.warning(f"[NameResolver] AkShare HK fallback failed: {e}")

    cached = _load_cache_json(_AKSHARE_HK_LOCAL_CACHE_PATH, min_entries=50)
    if cached:
        _akshare_hk_cache = (now, cached)
        logger.info(f"[NameResolver] Using local HK file cache: {len(cached)} name->code mappings")
        return cached
    return None


def _save_cache_json(path: str, data: Dict[str, str]) -> None:
    """Persist name->code mapping to a local JSON file."""
    try:
        cache_dir = os.path.dirname(path)
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.debug("[NameResolver] Cache saved to %s", path)
    except Exception as e:
        logger.debug("[NameResolver] Failed to save cache to %s: %s", path, e)


def _load_cache_json(path: str, min_entries: int = 100) -> Optional[Dict[str, str]]:
    """Load name->code mapping from a local JSON file."""
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and len(data) > min_entries:
            return data
        return None
    except Exception as e:
        logger.debug("[NameResolver] Failed to load cache from %s: %s", path, e)
        return None


def _get_akshare_fund_name_to_code() -> Optional[Dict[str, str]]:
    """Fetch fund name->code from AkShare (ak.fund_name_em), with cache."""
    global _akshare_fund_cache
    now = time.time()
    if _akshare_fund_cache is not None and (now - _akshare_fund_cache[0]) < _AKSHARE_FUND_CACHE_TTL:
        return _akshare_fund_cache[1]
    try:
        import akshare as ak

        df = ak.fund_name_em()
        if df is None or df.empty:
            return None
        code_to_name = {}
        for _, row in df.iterrows():
            code = str(row.get("基金代码", "")).strip()
            name = str(row.get("基金简称", "")).strip()
            if code and name:
                code_to_name[code] = name
        result = _build_reverse_map_no_duplicates(code_to_name)
        _akshare_fund_cache = (now, result)
        logger.info(f"[NameResolver] AkShare fund cache loaded: {len(result)} name->code mappings")
        return result
    except Exception as e:
        logger.warning(f"[NameResolver] AkShare fund fallback failed: {e}")
        return None


def _is_single_char_typo(input_name: str, candidate_name: str) -> bool:
    """Return True when two names only differ by one character position."""
    if not input_name or not candidate_name:
        return False
    if len(input_name) != len(candidate_name):
        return False
    # Keep typo fallback conservative: only for names with enough signal.
    if len(input_name) < 3:
        return False
    diff = sum(1 for a, b in zip(input_name, candidate_name) if a != b)
    return diff == 1


def _save_akshare_local_cache(data: Dict[str, str]) -> None:
    _save_cache_json(_AKSHARE_LOCAL_CACHE_PATH, data)


def _load_akshare_local_cache() -> Optional[Dict[str, str]]:
    return _load_cache_json(_AKSHARE_LOCAL_CACHE_PATH, min_entries=100)


def resolve_name_to_code(name: str) -> Optional[str]:
    """
    Resolve stock name to code.

    Strategy (in order):
    1. If input looks like a code (5-6 digits or 1-5 letters), return it normalized.
    2. Local STOCK_NAME_MAP reverse (exclude ambiguous names).
    3. Pinyin match against local names.
    4. AkShare online fallback (A-shares).
    5. Fuzzy match (difflib).
    6. Return None.

    Args:
        name: Stock name or code string.

    Returns:
        Resolved stock code, or None if ambiguous/failed.
    """
    if not name or not isinstance(name, str):
        return None
    s = name.strip()
    if not s:
        return None

    # 1. Input looks like code
    if _is_code_like(s):
        return _normalize_code(s)

    # 2. Local reverse map (no duplicates)
    local_reverse = _LOCAL_REVERSE_MAP
    if s in local_reverse:
        return local_reverse[s]
    if s in _LOCAL_AMBIGUOUS_NAMES:
        logger.debug(f"[NameResolver] 命中本地歧义名称，快速返回 None: {s}")
        return None

    # 3. Pinyin match (exact)
    try:
        from pypinyin import lazy_pinyin

        input_pinyin = "".join(lazy_pinyin(s)).lower()
        for local_name, code in local_reverse.items():
            local_pinyin = "".join(lazy_pinyin(local_name)).lower()
            if input_pinyin == local_pinyin:
                return code
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"[NameResolver] Pinyin match failed: {e}")

    # Skip AkShare/fuzzy fallback for non-CJK free text such as random Latin noise.
    # These paths are expensive and only meaningfully help Chinese stock names.
    if not _contains_cjk(s):
        logger.debug(f"[NameResolver] Skip CJK-only fallbacks for non-CJK input: {s}")
        return None

    # 4. AkShare A-share fallback
    akshare_map = _get_akshare_name_to_code()
    if akshare_map and s in akshare_map:
        logger.debug(f"[NameResolver] 命中 AkShare A股映射: {s} -> {akshare_map[s]}")
        return akshare_map[s]

    # 4.5. AkShare HK stock fallback
    akshare_hk_map = _get_akshare_hk_name_to_code()
    if akshare_hk_map and s in akshare_hk_map:
        logger.debug(f"[NameResolver] 命中 AkShare 港股映射: {s} -> {akshare_hk_map[s]}")
        return akshare_hk_map[s]

    # 5. Fuzzy match (local + akshare A-share + HK, local takes precedence)
    all_name_to_code = dict(local_reverse)
    if akshare_map:
        all_name_to_code.update(akshare_map)
    if akshare_hk_map:
        all_name_to_code.update(akshare_hk_map)
    # Skip fuzzy matching for very short inputs (<=2 chars) to avoid false positives,
    # e.g. '中国' matching arbitrary company names in a pool of 5000+ stocks.
    # Use a higher cutoff (0.8) to reduce mis-hits on longer inputs as well.
    if len(s) > 2:
        names = list(all_name_to_code.keys())
        matches = difflib.get_close_matches(s, names, n=1, cutoff=0.8)
        if matches:
            logger.debug(f"[NameResolver] 命中模糊匹配: input={s}, matched={matches[0]}")
            return all_name_to_code[matches[0]]

        # Conservative fallback for one-character typo in medium/long names.
        # This keeps the strict default threshold while fixing obvious misspellings
        # such as "贵州茅苔" -> "贵州茅台".
        typo_matches = difflib.get_close_matches(s, names, n=1, cutoff=0.7)
        if typo_matches and _is_single_char_typo(s, typo_matches[0]):
            logger.debug(f"[NameResolver] 命中单字误写兜底: input={s}, matched={typo_matches[0]}")
            return all_name_to_code[typo_matches[0]]

    # 6. AkShare fund name fallback
    fund_map = _get_akshare_fund_name_to_code()
    if fund_map and s in fund_map:
        logger.debug(f"[NameResolver] 命中基金名称映射: {s} -> {fund_map[s]}")
        return fund_map[s]

    logger.debug(f"[NameResolver] 解析失败: {s}")
    return None
