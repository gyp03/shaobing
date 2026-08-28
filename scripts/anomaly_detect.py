#!/usr/bin/env python3
"""
Standalone anomaly detector for the anomaly-detection-algorithms Skill.

Uses only Python standard library.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from statistics import median, mean
from typing import Any, Dict, List, Optional, Sequence, Tuple


DEFAULT_DETECTORS: Dict[str, Dict[str, Any]] = {
    "yoy": {"enabled": True, "min_data_points": 5, "threshold": 0.5, "confidence_base": 0.7, "extra": {}},
    "robust_zscore": {"enabled": True, "min_data_points": 4, "threshold": 3.0, "confidence_base": 0.7, "extra": {}},
    "stl": {"enabled": True, "min_data_points": 9, "threshold": 4.0, "confidence_base": 0.7, "extra": {"period": 7}},
    "mann_kendall": {"enabled": True, "min_data_points": 5, "threshold": 1.96, "confidence_base": 0.7, "extra": {"min_change_pct": 0.05}},
    "sliding_window_t": {"enabled": True, "min_data_points": 8, "threshold": 2.0, "confidence_base": 0.7, "extra": {"min_change_pct": 0.10}},
    "isolation_forest": {"enabled": True, "min_data_points": 4, "confidence_base": 0.7, "extra": {"window_ratio": 0.25, "score_threshold": 0.65}},

}

DETECTOR_ORDER = [
    "yoy",
    "robust_zscore",
    "stl",
    "mann_kendall",
    "sliding_window_t",
    "isolation_forest",
]

SEVERITY_RANK = {"无": 0, "低": 1, "中": 2, "高": 3}
DETECTION_LABEL = "[算法检测]"



@dataclass
class DetectionResult:
    detector: str
    is_anomaly: bool
    confidence: float
    anomaly_type: str
    anomaly_score: float
    explanation: str
    raw: Dict[str, Any]


@dataclass
class SeriesAlert:
    dimensions: Dict[str, str]
    metric: str
    current_time: str
    current_value: float
    is_anomaly: bool
    severity: str
    final_alert: bool
    main_detector: str
    confidence: float
    anomaly_type: str
    reason: str
    triggered_detectors: List[DetectionResult]
    all_detectors: List[DetectionResult]
    convergence_notes: List[str]


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _fmt_num(value: float) -> str:
    if abs(value) >= 100 or float(value).is_integer():
        return f"{value:.0f}"
    return f"{value:.2f}"


def _population_std(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    avg = mean(values)
    return math.sqrt(sum((x - avg) ** 2 for x in values) / len(values))


def _sample_var(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = mean(values)
    return sum((x - avg) ** 2 for x in values) / (len(values) - 1)


def _confidence_by_excess(score: float, threshold: float, base: float, scale: float = 0.3) -> float:
    if threshold <= 0:
        return 1.0
    return min(1.0, (score - threshold) / threshold * scale + base)


def _no_anomaly(detector: str, explanation: str) -> DetectionResult:
    return DetectionResult(detector, False, 0.0, "", 0.0, explanation, {})


def detect_yoy(values: Sequence[float], granularity: str, period_hint: int, cfg: Dict[str, Any]) -> DetectionResult:
    name = "yoy"
    min_data_points = int(cfg.get("min_data_points", 5))
    threshold = float(cfg.get("threshold", 0.5))
    confidence_base = float(cfg.get("confidence_base", 0.7))
    extra = cfg.get("extra") or {}
    previous_defaults = {"minutely": 24 * 60, "hourly": 24, "daily": 1, "weekly": 1, "monthly": 1}
    seasonal_defaults = {"minutely": 7 * 24 * 60, "hourly": 7 * 24, "daily": period_hint or 7, "weekly": period_hint or 4, "monthly": period_hint or 12}
    previous_lag = max(1, int(extra.get("previous_lag", previous_defaults.get(granularity, 1))))
    seasonal_lag = max(previous_lag, int(extra.get("seasonal_lag", seasonal_defaults.get(granularity, period_hint or 7))))
    min_points = max(min_data_points, previous_lag + 1, seasonal_lag + 1)
    if len(values) < min_points:
        return _no_anomaly(name, "数据量不足")
    current = float(values[-1])
    previous_value = float(values[-1 - previous_lag])
    seasonal_value = float(values[-1 - seasonal_lag])
    if previous_value == 0 or seasonal_value == 0:
        return _no_anomaly(name, "参考值无效")
    previous_change = (current - previous_value) / previous_value
    seasonal_change = (current - seasonal_value) / seasonal_value
    same_direction = (previous_change > 0 and seasonal_change > 0) or (previous_change < 0 and seasonal_change < 0)
    if not same_direction:
        return _no_anomaly(name, "前一参考点与上周期同期变化方向不一致")
    prev_abs = abs(previous_change)
    seas_abs = abs(seasonal_change)
    if prev_abs > threshold and seas_abs > threshold:
        effective = min(prev_abs, seas_abs)
        confidence = _confidence_by_excess(effective, threshold, confidence_base, scale=0.5)
        direction = "上涨" if previous_change > 0 else "下跌"
        anomaly_type = "同环比突变" if previous_change > 0 else "断崖下跌"
        return DetectionResult(
            name,
            True,
            confidence,
            anomaly_type,
            effective,
            f"当前值{_fmt_num(current)}相比前一参考点{_fmt_num(previous_value)}{direction}{prev_abs * 100:.1f}%，相比上周期同期{_fmt_num(seasonal_value)}{direction}{seas_abs * 100:.1f}%",
            {
                "current": current,
                "previous_value": previous_value,
                "seasonal_value": seasonal_value,
                "previous_change_rate": previous_change,
                "seasonal_change_rate": seasonal_change,
                "previous_lag": previous_lag,
                "seasonal_lag": seasonal_lag,
                "threshold": threshold,
            },
        )
    return _no_anomaly(name, "至少一个参考点变化在正常范围内")


def detect_robust_zscore(values: Sequence[float], cfg: Dict[str, Any]) -> DetectionResult:
    name = "robust_zscore"
    min_data_points = int(cfg.get("min_data_points", 4))
    threshold = float(cfg.get("threshold", 3.0))
    confidence_base = float(cfg.get("confidence_base", 0.7))
    if len(values) < min_data_points:
        return _no_anomaly(name, "数据量不足")
    med = median(values)
    mad = median([abs(x - med) for x in values])
    if mad == 0:
        return _no_anomaly(name, "数据无波动")
    robust_z = 0.6745 * (values[-1] - med) / mad
    abs_z = abs(robust_z)
    if abs_z > threshold:
        confidence = _confidence_by_excess(abs_z, threshold, confidence_base, scale=0.3)
        anomaly_type = "尖峰异常" if robust_z > 0 else "断崖下跌"
        direction = "高于" if robust_z > 0 else "低于"
        change_pct = abs(values[-1] - med) / abs(med) * 100 if med != 0 else 0.0
        return DetectionResult(
            name,
            True,
            confidence,
            anomaly_type,
            abs_z,
            f"当前值{_fmt_num(values[-1])}{direction}中位数{_fmt_num(med)}，偏离{change_pct:.1f}%",
            {"robust_zscore": robust_z, "median": med, "mad": mad, "threshold": threshold},
        )
    return _no_anomaly(name, "数值正常，无异常")


def _moving_average_same(values: Sequence[float], window: int) -> List[float]:
    if window < 2:
        avg = mean(values) if values else 0.0
        return [avg for _ in values]
    radius_left = (window - 1) // 2
    result = []
    for i in range(len(values)):
        start = max(0, i - radius_left)
        end = min(len(values), start + window)
        if end - start < window:
            start = max(0, end - window)
        result.append(mean(values[start:end]))
    return result


def _seasonal_component(values: Sequence[float], period: int) -> List[float]:
    if period <= 0 or len(values) // period < 2:
        return [0.0 for _ in values]
    seasonal = [0.0 for _ in values]
    for i in range(period):
        idx = list(range(i, len(values), period))
        if idx:
            avg = mean(values[j] for j in idx)
            for j in idx:
                seasonal[j] = avg
    seasonal_mean = mean(seasonal) if seasonal else 0.0
    return [x - seasonal_mean for x in seasonal]


def detect_stl(values: Sequence[float], period_hint: int, cfg: Dict[str, Any]) -> DetectionResult:
    name = "stl"
    min_data_points = int(cfg.get("min_data_points", 9))
    threshold = float(cfg.get("threshold", 4.0))
    confidence_base = float(cfg.get("confidence_base", 0.7))
    extra = cfg.get("extra") or {}
    period = int(extra.get("period", period_hint or 7))
    min_points = max(min_data_points, period + 2)
    if len(values) < min_points:
        return _no_anomaly(name, "数据量不足")
    window = min(period, len(values) // 3)
    if window < 2:
        trend = [mean(values) for _ in values]
    else:
        trend = _moving_average_same(values, window)
    seasonal = _seasonal_component(values, period)
    residual = [v - t - s for v, t, s in zip(values, trend, seasonal)]
    residual_std = _population_std(residual)
    if residual_std == 0:
        return _no_anomaly(name, "数据无波动")
    current_residual = residual[-1]
    z_score = abs(current_residual) / residual_std
    if z_score > threshold:
        confidence = _confidence_by_excess(z_score, threshold, confidence_base, scale=0.3)
        trend_std = _population_std(trend)
        if trend_std > 0 and abs(trend[-1] - trend[0]) > trend_std * 2:
            anomaly_type = "均值位移"
            change_pct = abs(trend[-1] - trend[0]) / abs(trend[0]) * 100 if trend[0] != 0 else 0.0
            explanation = f"趋势从{_fmt_num(trend[0])}变为{_fmt_num(trend[-1])}，变化{change_pct:.1f}%"
        else:
            anomaly_type = "周期性异常"
            avg = mean(values)
            deviation_pct = abs(values[-1] - avg) / abs(avg) * 100 if avg != 0 else 0.0
            explanation = f"当前值{_fmt_num(values[-1])}相对均值{_fmt_num(avg)}偏差{deviation_pct:.1f}%"
        return DetectionResult(
            name,
            True,
            confidence,
            anomaly_type,
            z_score,
            explanation,
            {"z_score": z_score, "current_residual": current_residual, "residual_std": residual_std, "period": period, "threshold": threshold},
        )
    return _no_anomaly(name, "数值在正常波动范围内")


def detect_mann_kendall(values: Sequence[float], cfg: Dict[str, Any]) -> DetectionResult:
    name = "mann_kendall"
    min_data_points = int(cfg.get("min_data_points", 5))
    threshold = float(cfg.get("threshold", 1.96))
    confidence_base = float(cfg.get("confidence_base", 0.7))
    extra = cfg.get("extra") or {}
    min_change_pct = float(extra.get("min_change_pct", 0.05))
    n = len(values)
    if n < min_data_points:
        return _no_anomaly(name, "数据量不足")
    s = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            if values[j] > values[i]:
                s += 1
            elif values[j] < values[i]:
                s -= 1
    counts: Dict[float, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    tie_correction = sum(c * (c - 1) * (2 * c + 5) for c in counts.values())
    var_s = (n * (n - 1) * (2 * n + 5) - tie_correction) / 18
    if var_s == 0:
        return _no_anomaly(name, "方差为零")
    z = s / math.sqrt(var_s)
    tau = s / (n * (n - 1) / 2)
    if abs(z) > threshold:
        prev_avg = mean(values[:-1]) if len(values) > 1 else values[-1]
        change_pct = abs(values[-1] - prev_avg) / abs(prev_avg) if prev_avg != 0 else 0.0
        if change_pct < min_change_pct:
            return _no_anomaly(name, "变化幅度过小")
        split = max(1, n // 2)
        front_avg = mean(values[:split])
        recent_avg = mean(values[split:]) if split < n else values[-1]
        window_change_pct = abs(recent_avg - front_avg) / abs(front_avg) if front_avg != 0 else 0.0
        confidence = _confidence_by_excess(abs(z), threshold, confidence_base, scale=0.3)
        anomaly_type = "趋势上升" if z > 0 else "趋势下降"
        direction = "高于" if z > 0 else "低于"
        return DetectionResult(
            name,
            True,
            confidence,
            anomaly_type,
            abs(z),
            f"数据整体呈{anomaly_type}，近段均值{_fmt_num(recent_avg)}{direction}前段均值{_fmt_num(front_avg)}，当前值{_fmt_num(values[-1])}仍处于趋势后的区间",
            {
                "z_score": z,
                "tau": tau,
                "s": s,
                "threshold": threshold,
                "change_pct": change_pct,
                "front_avg": front_avg,
                "recent_avg": recent_avg,
                "window_change_pct": window_change_pct,
            },
        )

    return _no_anomaly(name, "未检测到明显趋势变化")


def detect_sliding_window_t(values: Sequence[float], cfg: Dict[str, Any]) -> DetectionResult:
    name = "sliding_window_t"
    min_data_points = int(cfg.get("min_data_points", 8))
    threshold = float(cfg.get("threshold", 2.0))
    confidence_base = float(cfg.get("confidence_base", 0.7))
    extra = cfg.get("extra") or {}
    min_change_pct = float(extra.get("min_change_pct", 0.10))
    n_total = len(values)
    if n_total < min_data_points:
        return _no_anomaly(name, "数据量不足")
    window_size = max(2, min(n_total // 4, 7))
    first = list(values[-2 * window_size : -window_size])
    second = list(values[-window_size:])
    if len(first) < 2 or len(second) < 2:
        return _no_anomaly(name, "数据量不足")
    mean1, mean2 = mean(first), mean(second)
    var1, var2 = _sample_var(first), _sample_var(second)
    n = len(first)
    if var1 + var2 == 0:
        return _no_anomaly(name, "数据无波动")
    se = math.sqrt(var1 / n + var2 / n)
    if se == 0:
        return _no_anomaly(name, "数据无差异")
    t_stat = (mean2 - mean1) / se
    current = values[-1]
    current_deviation_ratio = abs(current - mean1) / abs(mean1) if mean1 != 0 else 0.0
    if current_deviation_ratio < min_change_pct:
        return _no_anomaly(name, f"当前值偏离前窗均值{current_deviation_ratio * 100:.1f}%，低于最小发布门槛{min_change_pct * 100:.1f}%")
    if abs(t_stat) > threshold:
        confidence = _confidence_by_excess(abs(t_stat), threshold, confidence_base, scale=0.3)
        current_deviation_pct = current_deviation_ratio * 100
        window_change_pct = abs(mean2 - mean1) / abs(mean1) * 100 if mean1 != 0 else 0.0
        return DetectionResult(
            name,
            True,
            confidence,
            "均值位移",
            abs(t_stat),
            f"当前值{_fmt_num(current)}偏离前窗均值{_fmt_num(mean1)}，偏离{current_deviation_pct:.1f}%；近期窗口均值{_fmt_num(mean2)}，窗口变化{window_change_pct:.1f}%",
            {"t_statistic": t_stat, "mean_before": mean1, "mean_after": mean2, "current": current, "current_deviation_ratio": current_deviation_ratio, "window_size": window_size, "threshold": threshold},
        )
    return _no_anomaly(name, "数值变化在正常范围内")


def _average_path_length(n: int) -> float:
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    return 2.0 * (math.log(n - 1) + 0.5772156649) - (2.0 * (n - 1) / n)


def _isolation_scores(values: Sequence[float], num_trees: int, sample_size: int, seed: int) -> List[float]:
    n = len(values)
    actual_sample = min(sample_size, n)
    max_depth = int(math.ceil(math.log2(max(actual_sample, 2))))
    path_lengths = [0.0 for _ in values]
    rng = random.Random(seed)
    for _ in range(num_trees):
        sample = [values[i] for i in rng.sample(range(n), actual_sample)]
        for i, current in enumerate(values):
            node = list(sample)
            depth = 0
            while len(node) > 1 and depth < max_depth:
                vmin, vmax = min(node), max(node)
                if vmin == vmax:
                    break
                split = rng.uniform(vmin, vmax)
                node = [v for v in node if v < split] if current < split else [v for v in node if v >= split]
                depth += 1
            leaf_correction = _average_path_length(len(node)) if len(node) > 1 else 0.0
            path_lengths[i] += depth + leaf_correction
    path_lengths = [p / num_trees for p in path_lengths]
    c = _average_path_length(actual_sample)
    if c == 0:
        return [0.5 for _ in values]
    return [2.0 ** (-p / c) for p in path_lengths]


def detect_isolation_forest(values: Sequence[float], cfg: Dict[str, Any], seed: int = 42) -> DetectionResult:
    name = "isolation_forest"
    min_data_points = int(cfg.get("min_data_points", 4))
    confidence_base = float(cfg.get("confidence_base", 0.7))
    extra = cfg.get("extra") or {}
    if len(values) < min_data_points:
        return _no_anomaly(name, "数据量不足")
    num_trees = int(extra.get("num_trees", 100))
    sample_size = int(extra.get("sample_size", 256))
    score_threshold = float(extra.get("score_threshold", 0.65))
    window_ratio = float(extra.get("window_ratio", 0.25))
    scores = _isolation_scores(values, num_trees, sample_size, seed)
    recent_score = scores[-1]
    if recent_score <= score_threshold:
        return _no_anomaly(name, f"数值正常（孤立分数={recent_score:.3f}）")
    window_size = max(1, int(len(values) * window_ratio))
    recent_mean = mean(values[-window_size:])
    historical_values = values[:-window_size] if len(values) > window_size else values
    historical_mean = mean(historical_values) if historical_values else recent_mean
    change_pct = (recent_mean - historical_mean) / abs(historical_mean) * 100 if historical_mean != 0 else 0.0
    anomaly_type = "尖峰异常" if change_pct > 0 else "断崖下跌"
    direction = "上升" if change_pct > 0 else "下降"
    range_above = 1.0 - score_threshold
    confidence = confidence_base + ((recent_score - score_threshold) / range_above * (1.0 - confidence_base) if range_above > 0 else 0.0)
    confidence = min(1.0, confidence)
    return DetectionResult(
        name,
        True,
        confidence,
        anomaly_type,
        recent_score,
        f"孤立森林检测到最近数据异常（分数={recent_score:.3f}），近期均值{_fmt_num(recent_mean)}相比历史均值{_fmt_num(historical_mean)}{direction}{abs(change_pct):.1f}%",
        {"isolation_score": recent_score, "score_threshold": score_threshold, "recent_mean": recent_mean, "historical_mean": historical_mean},
    )


def run_detectors(values: Sequence[float], granularity: str, period_hint: int, seed: int = 42, config: Optional[Dict[str, Any]] = None) -> List[DetectionResult]:
    cfgs = config or DEFAULT_DETECTORS
    results: List[DetectionResult] = []
    for name in DETECTOR_ORDER:
        cfg = cfgs.get(name, DEFAULT_DETECTORS[name])
        if not cfg.get("enabled", True):
            continue
        if name == "yoy":
            result = detect_yoy(values, granularity, period_hint, cfg)
        elif name == "robust_zscore":
            result = detect_robust_zscore(values, cfg)
        elif name == "stl":
            result = detect_stl(values, period_hint, cfg)
        elif name == "mann_kendall":
            result = detect_mann_kendall(values, cfg)
        elif name == "sliding_window_t":
            result = detect_sliding_window_t(values, cfg)
        elif name == "isolation_forest":
            result = detect_isolation_forest(values, cfg, seed=seed)
        else:
            continue
        results.append(result)
    return results


def _base_severity(confidence: float) -> str:
    if confidence >= 0.90:
        return "高"
    if confidence >= 0.70:
        return "中"
    if confidence >= 0.50:
        return "低"
    return "无"


def _metric_kind(metric_name: str) -> str:
    lowered = metric_name.lower()
    if "率" in metric_name or "rate" in lowered or "ratio" in lowered:
        return "rate"
    if "金额" in metric_name or "元" in metric_name or "amount" in lowered or "money" in lowered:
        return "amount"
    if "单量" in metric_name or "订单" in metric_name or "count" in lowered or "num" in lowered:
        return "count"
    return "generic"


def _low_base_threshold(metric_name: str) -> float:
    kind = _metric_kind(metric_name)
    if kind == "count":
        return 10.0
    if kind == "amount":
        return 10.0
    if kind == "rate":
        return 0.02
    return 10.0



def apply_convergence(
    metric: str,
    values: Sequence[float],
    triggered: List[DetectionResult],
    main: Optional[DetectionResult],
    enable_low_base_downgrade: bool = True,
) -> Tuple[str, bool, List[str]]:
    if main is None:
        return "无", False, []
    current = values[-1]
    hist_avg = mean(values[:-1]) if len(values) > 1 else current
    recent_window = values[-min(7, len(values)):]
    recent_avg = mean(recent_window) if recent_window else current
    threshold = _low_base_threshold(metric)
    kind = _metric_kind(metric)
    notes: List[str] = []
    severity = _base_severity(main.confidence)
    low_base_candidate = abs(current) <= threshold and abs(hist_avg) <= threshold
    low_base = enable_low_base_downgrade and low_base_candidate
    if low_base:
        notes.append(f"低基数降权：当前值{_fmt_num(current)}、历史均值{_fmt_num(hist_avg)}均不高")
        if SEVERITY_RANK[severity] > SEVERITY_RANK["低"]:
            severity = "低"
    elif low_base_candidate:
        notes.append("低基数降权已关闭：未按低基数压低异常等级")
    if enable_low_base_downgrade and kind == "rate" and abs(current) <= threshold:
        notes.append("比例类指标且数值很小，仅作辅助证据")
        severity = "低" if severity != "无" else "无"
    high_impact = False
    if kind == "count":
        high_impact = abs(hist_avg) >= 10 or abs(recent_avg) >= 10 or abs(current) >= 10
    elif kind == "amount":
        high_impact = abs(hist_avg) >= 50 or abs(recent_avg) >= 50 or abs(current) >= 50
    elif kind == "generic":
        high_impact = abs(hist_avg) >= 10 or abs(recent_avg) >= 10 or abs(current) >= 10
    if high_impact:
        notes.append("业务影响保留：绝对量或历史均值较高")
    trend_like = any(r.detector in {"mann_kendall", "sliding_window_t"} for r in triggered)
    if trend_like and high_impact and SEVERITY_RANK[severity] < SEVERITY_RANK["中"]:
        severity = "中"
        notes.append("持续趋势异常升为中等级")
    final_alert = SEVERITY_RANK[severity] >= SEVERITY_RANK["中"] and (high_impact or not low_base)
    if not final_alert and main.is_anomaly:
        notes.append("作为候选观察项，不进入最终主告警")
    return severity, final_alert, notes



def analyze_series(
    times: Sequence[str],
    values: Sequence[float],
    dimensions: Dict[str, str],
    metric: str,
    granularity: str,
    period_hint: int,
    seed: int = 42,
    enable_low_base_downgrade: bool = True,
    detector_config: Optional[Dict[str, Any]] = None,
) -> SeriesAlert:
    all_results = run_detectors(values, granularity, period_hint, seed=seed, config=detector_config)

    triggered = [r for r in all_results if r.is_anomaly]
    main = max(triggered, key=lambda r: r.confidence, default=None)
    severity, final_alert, notes = apply_convergence(metric, values, triggered, main, enable_low_base_downgrade)

    if main is None:
        return SeriesAlert(dimensions, metric, times[-1], values[-1], False, "无", False, "", 0.0, "", "未检测到异常", [], all_results, notes)
    return SeriesAlert(
        dimensions,
        metric,
        times[-1],
        values[-1],
        True,
        severity,
        final_alert,
        main.detector,
        main.confidence,
        main.anomaly_type,
        main.explanation,
        triggered,
        all_results,
        notes,
    )


def _read_text(path: Optional[str]) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8-sig")
    return sys.stdin.read()


def read_table(path: Optional[str]) -> List[Dict[str, str]]:
    text = _read_text(path)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t,")
    except csv.Error:
        dialect = csv.excel_tab if "\t" in sample else csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    return [dict(row) for row in reader]


def analyze_table(
    rows: List[Dict[str, str]],
    time_col: str,
    dimension_cols: Sequence[str],
    metric_cols: Sequence[str],
    granularity: str = "daily",
    period_hint: int = 7,
    latest_only: bool = True,
    seed: int = 42,
    enable_low_base_downgrade: bool = True,
    detector_config: Optional[Dict[str, Any]] = None,
) -> List[SeriesAlert]:

    grouped: Dict[Tuple[str, ...], List[Dict[str, str]]] = {}

    for row in rows:
        key = tuple(str(row.get(c, "")) for c in dimension_cols)
        grouped.setdefault(key, []).append(row)
    global_latest = max((str(row.get(time_col, "")) for row in rows if row.get(time_col)), default="")
    alerts: List[SeriesAlert] = []
    for key, items in grouped.items():
        items = sorted(items, key=lambda r: str(r.get(time_col, "")))
        dims = {col: value for col, value in zip(dimension_cols, key)}
        for metric in metric_cols:
            pairs: List[Tuple[str, float]] = []
            for row in items:
                value = _safe_float(row.get(metric))
                time_value = str(row.get(time_col, ""))
                if value is not None and time_value:
                    pairs.append((time_value, value))
            if not pairs:
                continue
            if latest_only and pairs[-1][0] != global_latest:
                continue
            times = [t for t, _ in pairs]
            values = [v for _, v in pairs]
            alerts.append(
                analyze_series(
                    times,
                    values,
                    dims,
                    metric,
                    granularity,
                    period_hint,
                    seed=seed,
                    enable_low_base_downgrade=enable_low_base_downgrade,
                    detector_config=detector_config,
                )

            )

    return alerts


def _alert_to_dict(alert: SeriesAlert) -> Dict[str, Any]:
    data = asdict(alert)
    data["detection_label"] = DETECTION_LABEL
    data["detection_source"] = "algorithm"
    return data




def render_markdown(alerts: Sequence[SeriesAlert], include_observations: bool = True) -> str:
    final_alerts = [a for a in alerts if a.final_alert]
    observations = [a for a in alerts if a.is_anomaly and not a.final_alert]
    final_alerts.sort(key=lambda a: (SEVERITY_RANK.get(a.severity, 0), a.confidence, abs(a.current_value)), reverse=True)
    observations.sort(key=lambda a: (a.confidence, abs(a.current_value)), reverse=True)
    lines: List[str] = []
    if final_alerts:
        top = final_alerts[0]
        lines.append(f"结论：{DETECTION_LABEL} 异常")
        lines.append(f"异常程度：{top.severity}")
        lines.append(f"检测口径：{DETECTION_LABEL} / 算法候选复核")
        lines.append(f"主异常对象：{_format_dims(top.dimensions)} / {top.metric}")
    else:
        lines.append(f"结论：{DETECTION_LABEL} 正常或仅有观察项")
        lines.append("异常程度：无")
        lines.append(f"检测口径：{DETECTION_LABEL} / 算法候选复核")
    lines.append("")
    lines.append("## 最终告警项")
    if final_alerts:
        lines.append("| 检测标识 | 指标/维度 | 当前值 | 触发算法 | confidence | 异常程度 | 判断原因 |")
        lines.append("|---|---|---:|---|---:|---|---|")
        for a in final_alerts[:10]:
            lines.append(f"| {DETECTION_LABEL} | {_format_dims(a.dimensions)} / {a.metric} | {_fmt_num(a.current_value)} | `{a.main_detector}` | {a.confidence:.3f} | {a.severity} | {a.reason} |")

    else:
        lines.append("无最终告警项。")
    if include_observations:
        lines.append("")
        lines.append("## 候选观察项")
        if observations:
            lines.append("| 检测标识 | 指标/维度 | 当前值 | 触发算法 | confidence | 异常程度 | 收敛说明 |")
            lines.append("|---|---|---:|---|---:|---|---|")
            for a in observations[:10]:
                note = "；".join(a.convergence_notes) if a.convergence_notes else a.reason
                lines.append(f"| {DETECTION_LABEL} | {_format_dims(a.dimensions)} / {a.metric} | {_fmt_num(a.current_value)} | `{a.main_detector}` | {a.confidence:.3f} | {a.severity} | {note} |")

        else:
            lines.append("无。")
    return "\n".join(lines)


def _format_dims(dimensions: Dict[str, str]) -> str:
    if not dimensions:
        return "全局"
    return "、".join(f"{k}={v}" for k, v in dimensions.items())


def _split_cols(text: str) -> List[str]:
    return [x.strip() for x in text.split(",") if x.strip()]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone anomaly detector")
    parser.add_argument("input", nargs="?", help="CSV/TSV input file. Omit to read stdin.")
    parser.add_argument("--time-col", default="日期", help="Time column name")
    parser.add_argument("--dimension-cols", default="", help="Comma-separated dimension columns, e.g. 城市,城市id")
    parser.add_argument("--metric-cols", required=True, help="Comma-separated metric columns")
    parser.add_argument("--granularity", default="daily", choices=["minutely", "hourly", "daily", "weekly", "monthly"])
    parser.add_argument("--period", type=int, default=7, help="Seasonal period hint")
    parser.add_argument("--all-dates", action="store_true", help="Analyze groups even when they do not contain the global latest date")
    parser.add_argument("--low-base-downgrade", choices=["on", "off"], default="on", help="Apply low-base downgrade in convergence rules")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--seed", type=int, default=42)

    args = parser.parse_args(argv)

    rows = read_table(args.input)
    if not rows:
        print("No data", file=sys.stderr)
        return 1
    alerts = analyze_table(
        rows,
        time_col=args.time_col,
        dimension_cols=_split_cols(args.dimension_cols),
        metric_cols=_split_cols(args.metric_cols),
        granularity=args.granularity,
        period_hint=args.period,
        latest_only=not args.all_dates,
        seed=args.seed,
        enable_low_base_downgrade=args.low_base_downgrade == "on",
    )

    if args.format == "json":
        print(json.dumps([_alert_to_dict(a) for a in alerts], ensure_ascii=False, indent=2))
    else:
        print(render_markdown(alerts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
