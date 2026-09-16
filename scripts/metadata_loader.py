#!/usr/bin/env python3
"""Metadata loading and parsing helpers for anomaly alert publishing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import anomaly_detect as det
from odps_io import query_odps_rows

DEFAULT_METADATA_SQL = """
SELECT  id                  AS `主键id`
        ,business_name      AS `业务名称`
        ,business_desc      AS `业务描述`
        ,metric_name        AS `指标名称`
        ,data_sql           AS `提数sql`
        ,detection_algor    AS `检测算法`
        ,ex_prompt          AS `提示词`
        ,owner              AS `负责人`
        ,monitor_period     AS `监控周期：日/小时/分钟`
FROM    ygcx_dw_pro.ods_shaobing_busi_schema
WHERE   status = 'on'
;"""


@dataclass
class BusinessMetadata:
    id: str
    business_name: str
    business_desc: str
    metric_name: str
    data_sql: str
    detection_algor: str
    ex_prompt: str
    owner: str
    monitor_period: str


def split_cols(text: str) -> List[str]:
    return [x.strip() for x in re.split(r"[,，;；、]+", text or "") if x.strip()]


def _normalize_prompt_key(key: str) -> str:
    return re.sub(r"[\s_\-]+", "", key.strip().lower())


_PROMPT_OPTION_ALIASES = {
    _normalize_prompt_key(key): value
    for key, value in {
        "时间列": "time_col",
        "time_col": "time_col",
        "timecol": "time_col",
        "指标列": "metric_cols",
        "指标": "metric_cols",
        "metric_cols": "metric_cols",
        "metriccols": "metric_cols",
        "维度列": "dimension_cols",
        "维度": "dimension_cols",
        "dimension_cols": "dimension_cols",
        "dimensioncols": "dimension_cols",
        "发布等级": "severity_levels",
        "告警等级": "severity_levels",
        "severity_levels": "severity_levels",
        "severitylevels": "severity_levels",
        "检测周期": "period",
        "周期": "period",
        "period": "period",
        "低基数降权": "low_base_downgrade",
        "low_base_downgrade": "low_base_downgrade",
        "lowbasedowngrade": "low_base_downgrade",
        "检测算法": "detectors",
        "使用算法": "detectors",
        "detection_algor": "detectors",
        "detectionalgor": "detectors",
        "算法": "detectors",
        "detectors": "detectors",
    }.items()
}

_DETECTOR_ALIASES = {
    _normalize_prompt_key(key): value
    for key, value in {
        "yoy": "yoy",
        "同环比": "yoy",
        "同比": "yoy",
        "环比": "yoy",
        "robust_zscore": "robust_zscore",
        "zscore": "robust_zscore",
        "z-score": "robust_zscore",
        "稳健zscore": "robust_zscore",
        "稳健z-score": "robust_zscore",
        "stl": "stl",
        "季节分解": "stl",
        "趋势周期残差": "stl",
        "mann_kendall": "mann_kendall",
        "mann-kendall": "mann_kendall",
        "mk": "mann_kendall",
        "趋势检验": "mann_kendall",
        "sliding_window_t": "sliding_window_t",
        "滑动窗口t检验": "sliding_window_t",
        "滑窗t检验": "sliding_window_t",
        "滑窗": "sliding_window_t",
        "isolation_forest": "isolation_forest",
        "孤立森林": "isolation_forest",
    }.items()
}

_AGG_FUNC_RE = re.compile(
    r"\b(sum|count|avg|mean|max|min|stddev|stddev_pop|stddev_samp|variance|var_pop|var_samp|percentile|count_distinct|approx_distinct)\s*\(",
    re.IGNORECASE,
)


def parse_ex_prompt_options(text: str) -> Dict[str, str]:
    """Parse per-metadata custom requirements from ex_prompt.

    Supports a JSON object or key-value lines like `指标列=xxx` / `维度列: a,b`.
    Free-form text is kept by the caller and only parsed when explicit options exist.
    """
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return {str(k).strip(): str(v).strip() for k, v in value.items() if str(k).strip() and str(v).strip()}
    except json.JSONDecodeError:
        pass

    options: Dict[str, str] = {}
    for part in re.split(r"[\n;；]+", raw):
        match = re.match(r"\s*([^:=：]+?)\s*[:=：]\s*(.+?)\s*$", part)
        if not match:
            continue
        key = _PROMPT_OPTION_ALIASES.get(_normalize_prompt_key(match.group(1)), match.group(1).strip())
        value = match.group(2).strip()
        if key and value:
            options[key] = value
    return options


def metadata_value(row: Dict[str, str], *keys: str) -> str:

    for key in keys:
        value = row.get(key, "")
        if str(value).strip():
            return str(value).strip()
    return ""


def parse_metadata_row(row: Dict[str, str]) -> BusinessMetadata:
    return BusinessMetadata(
        id=metadata_value(row, "主键id", "id"),
        business_name=metadata_value(row, "业务名称", "business_name"),
        business_desc=metadata_value(row, "业务描述", "business_desc"),
        metric_name=metadata_value(row, "指标名称", "metric_name"),
        data_sql=metadata_value(row, "提数sql", "data_sql"),
        detection_algor=metadata_value(row, "检测算法", "detection_algor"),
        ex_prompt=metadata_value(row, "提示词", "ex_prompt"),
        owner=metadata_value(row, "负责人", "owner"),
        monitor_period=metadata_value(row, "监控周期：日/小时/分钟", "monitor_period"),
    )


def load_active_metadata(config: Dict[str, str], metadata_sql: str = DEFAULT_METADATA_SQL) -> List[BusinessMetadata]:
    return [parse_metadata_row(row) for row in query_odps_rows(metadata_sql, config, label="元数据SQL")]


def infer_time_col(rows: Sequence[Dict[str, str]], requested: str = "") -> str:
    if not rows:
        raise ValueError("无数据，无法识别时间列")
    columns = list(rows[0].keys())
    if requested and requested in columns:
        return requested
    candidates = ["dt", "日期", "date", "biz_date", "stat_date", "time", "时间"]
    lowered = {col.lower(): col for col in columns}
    for candidate in candidates:
        if candidate in columns:
            return candidate
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return columns[0]


def _non_empty_values(rows: Sequence[Dict[str, str]], col: str) -> List[str]:
    return [str(row.get(col, "")).strip() for row in rows if str(row.get(col, "")).strip()]


def _has_non_empty_values(rows: Sequence[Dict[str, str]], col: str) -> bool:
    return bool(_non_empty_values(rows, col))


def _numeric_ratio(rows: Sequence[Dict[str, str]], col: str) -> float:
    non_empty = _non_empty_values(rows, col)
    if not non_empty:
        return 0.0
    numeric = [value for value in non_empty if det._safe_float(value) is not None]
    return len(numeric) / len(non_empty)


def _valid_columns(rows: Sequence[Dict[str, str]]) -> List[str]:
    if not rows:
        return []
    return [col for col in rows[0].keys() if str(col).strip()]


def _split_top_level_csv(text: str) -> List[str]:
    items: List[str] = []
    buf: List[str] = []
    depth = 0
    quote = ""
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif ch == "," and depth == 0:
            item = "".join(buf).strip()
            if item:
                items.append(item)
            buf = []
            continue
        buf.append(ch)
    item = "".join(buf).strip()
    if item:
        items.append(item)
    return items


def _strip_identifier_quotes(text: str) -> str:
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"`", "'", '"'}:
        return value[1:-1].strip()
    return value


def _select_alias_and_expr(item: str) -> tuple[str, str, bool]:
    text = item.strip()
    match = re.search(r"\s+as\s+(`[^`]+`|'[^']+'|\"[^\"]+\"|[\w\u4e00-\u9fff]+)\s*$", text, re.IGNORECASE)
    if match:
        return _strip_identifier_quotes(match.group(1)), text[: match.start()].strip(), True
    match = re.search(r"\s+(`[^`]+`|'[^']+'|\"[^\"]+\"|[\w\u4e00-\u9fff]+)\s*$", text)
    if match and not text[: match.start()].strip().endswith("."):
        return _strip_identifier_quotes(match.group(1)), text[: match.start()].strip(), True
    match = re.match(r"^(?:[\w`]+\.)?(`[^`]+`|[\w\u4e00-\u9fff]+)\s*$", text)
    return (_strip_identifier_quotes(match.group(1)), text, False) if match else ("", text, False)


def _identifier_name(expr: str) -> str:
    text = expr.strip()
    match = re.match(r"^(?:`?[\w]+`?\.)?(`[^`]+`|[\w\u4e00-\u9fff]+)\s*$", text)
    return _strip_identifier_quotes(match.group(1)) if match else ""


def _top_level_keyword(sql: str, word: str, start: int = 0) -> int:
    depth = 0
    quote = ""
    idx = start
    pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
    while idx < len(sql):
        ch = sql[idx]
        if quote:
            if ch == quote:
                quote = ""
            idx += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif depth == 0:
            match = pattern.match(sql, idx)
            if match:
                return idx
        idx += 1
    return -1


def _outer_select_items(sql_text: str) -> List[str]:
    if not sql_text:
        return []
    sql = re.sub(r"/\*.*?\*/", " ", sql_text, flags=re.S)
    sql = re.sub(r"--.*?(?=\n|$)", " ", sql)
    select_pos = -1
    start = 0
    while True:
        pos = _top_level_keyword(sql, "select", start)
        if pos < 0:
            break
        select_pos = pos
        start = pos + 6
    if select_pos < 0:
        return []
    from_pos = _top_level_keyword(sql, "from", select_pos + 6)
    if from_pos < 0:
        return []
    return _split_top_level_csv(sql[select_pos + 6: from_pos])


def _sql_metric_cols(sql_text: str, columns: Sequence[str], time_col: str, exclude_cols: Sequence[str]) -> List[str]:
    col_set = set(columns)
    exclude_set = set(exclude_cols)
    projections = [
        (alias, expr, explicit_alias, item)
        for item in _outer_select_items(sql_text)
        for alias, expr, explicit_alias in [_select_alias_and_expr(item)]
        if alias in col_set and alias != time_col and alias not in exclude_set
    ]
    aggregate_cols = [alias for alias, _expr, _explicit_alias, item in projections if _AGG_FUNC_RE.search(item)]
    if aggregate_cols:
        return aggregate_cols

    metric_suffix: List[str] = []
    for alias, expr, explicit_alias, _item in reversed(projections):
        source_name = _identifier_name(expr)
        if explicit_alias and source_name and alias != source_name:
            metric_suffix.append(alias)
            continue
        break
    return list(reversed(metric_suffix))


def infer_metric_cols(
    rows: Sequence[Dict[str, str]],
    time_col: str,
    metadata_metric: str = "",
    requested: str = "",
    exclude_cols: Sequence[str] = (),
    sql_text: str = "",
) -> List[str]:
    if not rows:
        return []
    columns = _valid_columns(rows)
    exclude_set = {col for col in exclude_cols if col in columns}
    requested_cols = [col for col in split_cols(requested) if col in columns and col not in exclude_set]
    if requested_cols:
        return requested_cols

    numeric_ratios: Dict[str, float] = {}

    def numeric_ratio(col: str) -> float:
        if col not in numeric_ratios:
            numeric_ratios[col] = _numeric_ratio(rows, col)
        return numeric_ratios[col]

    metadata_metric_cols = [col for col in split_cols(metadata_metric) if col in columns and col not in exclude_set]
    if metadata_metric_cols:
        numeric_metadata_cols = [col for col in metadata_metric_cols if numeric_ratio(col) > 0]
        if numeric_metadata_cols:
            return numeric_metadata_cols
    sql_metric_cols = [col for col in _sql_metric_cols(sql_text, columns, time_col, exclude_set) if numeric_ratio(col) > 0]
    if sql_metric_cols:
        return sql_metric_cols
    numeric_cols = [
        col
        for col in columns
        if col != time_col and col not in exclude_set and numeric_ratio(col) >= 0.8
    ]
    if not numeric_cols:
        raise ValueError("无法从提数 SQL 结果中识别指标列，请检查 data_sql 是否包含数值指标列")
    return numeric_cols


def infer_dimension_cols(rows: Sequence[Dict[str, str]], time_col: str, metric_cols: Sequence[str], requested: str = "") -> List[str]:
    if not rows:
        return []
    columns = _valid_columns(rows)
    metric_set = set(metric_cols)
    non_empty_cols = {col for col in columns if _has_non_empty_values(rows, col)}
    requested_cols = [
        col
        for col in split_cols(requested)
        if col in columns and col != time_col and col not in metric_set and col in non_empty_cols
    ]
    if requested_cols:
        return requested_cols
    return [
        col
        for col in columns
        if col != time_col and col not in metric_set and col in non_empty_cols
    ]


def detector_config_from_text(text: str) -> Optional[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw or raw in {"全部", "全量", "默认", "all", "ALL", "*"}:
        return None
    selected: List[str] = []
    for part in re.split(r"[,，;；、/|\s]+", raw):
        token = part.strip()
        if not token:
            continue
        normalized = _DETECTOR_ALIASES.get(_normalize_prompt_key(token))
        if not normalized:
            raise ValueError(f"未知检测算法: {token}，可选: {', '.join(det.DETECTOR_ORDER)}")
        if normalized not in selected:
            selected.append(normalized)
    if not selected:
        return None
    config: Dict[str, Any] = json.loads(json.dumps(det.DEFAULT_DETECTORS, ensure_ascii=False))
    for name in det.DETECTOR_ORDER:
        config[name]["enabled"] = name in selected
    return config


def normalize_monitor_period(text: str) -> str:
    value = re.sub(r"\s+", "", (text or "").strip().lower())
    aliases = {
        "日": "日",
        "天": "日",
        "每天": "日",
        "日调度": "日",
        "daily": "日",
        "day": "日",
        "1d": "日",
        "1day": "日",
        "小时调度": "小时调度",
        "hourly": "小时调度",
        "小时": "1小时",
        "每小时": "1小时",
        "hour": "1小时",
        "1h": "1小时",
        "1hour": "1小时",
        "分钟调度": "分钟调度",
        "minutely": "分钟调度",
        "分钟": "1分钟",
        "minute": "1分钟",
        "min": "1分钟",
        "1m": "1分钟",
        "1min": "1分钟",
    }
    if value in aliases:
        return aliases[value]
    match = re.fullmatch(r"(\d+)(d|day|days|日|天)", value)
    if match:
        number = int(match.group(1))
        return "日" if number == 1 else f"{number}日"
    match = re.fullmatch(r"(\d+)(h|hour|hours|小时)", value)
    if match:
        return f"{int(match.group(1))}小时"
    match = re.fullmatch(r"(\d+)(m|min|mins|minute|minutes|分钟)", value)
    if match:
        return f"{int(match.group(1))}分钟"
    return value


def monitor_period_matches(actual: str, expected: str) -> bool:
    expected_periods = [normalize_monitor_period(part) for part in re.split(r"[,，;；、/|\s]+", expected or "") if part.strip()]
    if not expected_periods:
        return True
    actual_period = normalize_monitor_period(actual)
    for expected_period in expected_periods:
        if expected_period == actual_period:
            return True
        if expected_period == "小时调度" and actual_period.endswith("小时"):
            return True
        if expected_period == "分钟调度" and actual_period.endswith("分钟"):
            return True
    return False


def _sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def metadata_period_condition(monitor_period: str) -> str:
    period = normalize_monitor_period(monitor_period)
    if not period:
        return ""
    if period == "小时调度":
        return "monitor_period LIKE '%小时'"
    if period == "分钟调度":
        return "monitor_period LIKE '%分钟'"
    return f"monitor_period = {_sql_literal(period)}"


def apply_metadata_period_filter(metadata_sql: str, monitor_period: str) -> str:
    condition = metadata_period_condition(monitor_period)
    if not condition:
        return metadata_sql
    sql = metadata_sql.strip().rstrip(";").strip()
    connector = "AND" if re.search(r"\bwhere\b", sql, flags=re.IGNORECASE) else "WHERE"
    return f"{sql}\n  {connector} {condition}\n;"


def monitor_period_to_granularity(text: str, default: str = "daily") -> str:

    value = normalize_monitor_period(text)
    if value.endswith("分钟") or value == "分钟调度":
        return "minutely"
    if value.endswith("小时") or value == "小时调度":
        return "hourly"
    if value == "日" or value.endswith("日"):
        return "daily"
    if "周" in value or "week" in value:
        return "weekly"
    if "月" in value or "month" in value:
        return "monthly"
    return default


