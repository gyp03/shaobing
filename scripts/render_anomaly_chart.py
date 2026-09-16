#!/usr/bin/env python3
"""Render anomaly time-series charts as JPG."""

from __future__ import annotations

import argparse
import csv
import sys

from pathlib import Path
from statistics import median, mean
from typing import List, Optional, Sequence, Tuple


DETECTORS = {"yoy", "robust_zscore", "stl", "mann_kendall", "sliding_window_t", "isolation_forest", "threshold"}
FONT_CANDIDATES = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Arial Unicode MS", "DejaVu Sans"]
DETECTOR_NOTES = {
    "yoy": "同环比突变",
    "robust_zscore": "偏离中位数",
    "stl": "趋势残差偏离",
    "mann_kendall": "持续趋势",
    "sliding_window_t": "窗口均值位移",
    "isolation_forest": "孤立点",
    "threshold": "阈值规则",
}
SEVERITY_COLORS = {
    "高": ("#fee2e2", "#b91c1c"),
    "中": ("#fef3c7", "#92400e"),
    "低": ("#e0f2fe", "#075985"),
    "无": ("#f3f4f6", "#4b5563"),
}


def _safe_float(value: object) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _read_text(path: Optional[str]) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8-sig")
    return sys.stdin.read()


def read_series(path: Optional[str], time_col: str, metric_col: str) -> Tuple[List[str], List[float]]:
    text = _read_text(path)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t,")
    except csv.Error:
        dialect = csv.excel_tab if "\t" in sample else csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    pairs: List[Tuple[str, float]] = []
    for row in reader:
        t = str(row.get(time_col, "")).strip()
        v = _safe_float(row.get(metric_col))
        if t and v is not None:
            pairs.append((t, v))
    pairs.sort(key=lambda x: x[0])
    return [t for t, _ in pairs], [v for _, v in pairs]


def _fmt(value: float) -> str:
    return format(float(value), ".15g")



def _shorten(text: str, max_len: int = 86) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _moving_average(values: Sequence[float], window: int) -> List[float]:
    window = max(2, window)
    result: List[float] = []
    for i in range(len(values)):
        start = max(0, i - window + 1)
        result.append(mean(values[start : i + 1]))
    return result


def _auto_plot_left(values: Sequence[float]) -> float:
    if not values:
        return 0.055
    v_min, v_max = min(values), max(values)
    if v_min == v_max:
        pad = abs(v_min) * 0.1 or 1.0
        v_min -= pad
        v_max += pad
    else:
        pad = (v_max - v_min) * 0.12
        v_min -= pad
        v_max += pad
    labels = [_fmt(v_max - (v_max - v_min) * k / 4) for k in range(5)]
    max_len = max(len(label) for label in labels)
    return min(0.15, max(0.055, 0.035 + max_len * 0.009))


def render_jpg(
    times: Sequence[str],
    values: Sequence[float],
    metric: str,
    detector: str,
    output: str,
    title: str = "",
    period: int = 7,
    width: int = 1000,
    height: int = 520,
    severity: str = "",
    reason: str = "",
    dimensions: str = "",
    threshold_lines: Optional[Sequence[Tuple[str, float]]] = None,
    dpi: int = 120,
    quality: int = 92,
) -> None:

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        from matplotlib.patches import FancyBboxPatch

    except Exception as exc:
        raise RuntimeError("生成 JPG 需要 matplotlib：请先安装 matplotlib") from exc


    available_fonts = {f.name for f in font_manager.fontManager.ttflist}
    for font_name in FONT_CANDIDATES:
        if font_name in available_fonts:
            plt.rcParams["font.sans-serif"] = [font_name]
            break
    plt.rcParams["axes.unicode_minus"] = False

    dimensions = _shorten(dimensions, 76)
    fig_w, fig_h = width / dpi, height / dpi
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("#f8fafc")
    plot_left, plot_right = _auto_plot_left(values), 0.945
    plot_top = 0.755 if dimensions else 0.785
    fig.subplots_adjust(left=plot_left, right=plot_right, top=plot_top, bottom=0.155)


    x = list(range(len(values)))
    threshold_lines = list(threshold_lines or [])
    if threshold_lines:
        y_values = list(values) + [line[1] for line in threshold_lines]
        y_min, y_max = min(y_values), max(y_values)
        y_pad = (y_max - y_min) * 0.12 or abs(y_max) * 0.1 or 1.0
        ax.set_ylim(y_min - y_pad, y_max + y_pad)
    ax.grid(axis="y", color="#e5e7eb", linewidth=0.8)
    ax.plot(x, values, color="#2563eb", linewidth=2.2, marker="o", markersize=3.8)
    ax.scatter([x[-1]], [values[-1]], s=72, color="#ef4444", edgecolors="white", linewidths=1.4, zorder=5)
    ax.annotate(f"当前 {_fmt(values[-1])}", xy=(x[-1], values[-1]), xytext=(-12, 10), textcoords="offset points", color="#b91c1c", fontsize=10)

    if detector == "yoy":
        for idx, label in [(len(values) - 2, "前一参考点"), (len(values) - 1 - max(1, period), "上周期同期")]:
            if 0 <= idx < len(values):
                ax.axvline(idx, color="#f59e0b", linestyle=(0, (4, 4)), linewidth=1.2)
                ax.text(idx, ax.get_ylim()[1], label, ha="center", va="bottom", color="#b45309", fontsize=9)
                ax.plot([idx, x[-1]], [values[idx], values[-1]], color="#f59e0b", linestyle=(0, (3, 4)), linewidth=1.2)
    elif detector == "robust_zscore":
        med = median(values)
        mad = median([abs(v - med) for v in values])
        band = 3.0 * mad / 0.6745 if mad else 0.0
        if band:
            ax.axhspan(med - band, med + band, color="#eef6ff", alpha=0.75)
        ax.axhline(med, color="#2563eb", linestyle=(0, (6, 4)), linewidth=1.3)
        ax.text(x[-1], med, f"中位数 {_fmt(med)}", ha="right", va="bottom", color="#1d4ed8", fontsize=9)
    elif detector == "stl":
        trend = _moving_average(values, min(max(2, period), max(2, len(values) // 2)))
        ax.plot(x, trend, color="#7c3aed", linestyle=(0, (6, 3)), linewidth=1.8)
    elif detector == "mann_kendall":
        split = max(1, len(values) // 2)
        specs = [(0, split - 1, "前段", "#ecfdf5", "#059669"), (split, len(values) - 1, "近段", "#fef2f2", "#dc2626")]
        mids: List[Tuple[float, float]] = []
        for start, end, label, fill, stroke in specs:
            if 0 <= start <= end < len(values):
                ax.axvspan(start, end, color=fill, alpha=0.65)
                avg = mean(values[start : end + 1])
                ax.hlines(avg, start, end, colors=stroke, linestyles=(0, (6, 4)), linewidth=1.8)
                mid = (start + end) / 2
                mids.append((mid, avg))
                ax.text(mid, avg, f"{label}均值 {_fmt(avg)}", ha="center", va="bottom", color=stroke, fontsize=9)
        if len(mids) == 2:
            ax.annotate("", xy=mids[1], xytext=mids[0], arrowprops={"arrowstyle": "->", "color": "#64748b", "linestyle": (0, (3, 4)), "linewidth": 1.3})
    elif detector == "sliding_window_t":
        window = max(2, min(len(values) // 4, 7))
        for start, end, label, fill in [(len(values) - 2 * window, len(values) - window - 1, "前窗", "#e0f2fe"), (len(values) - window, len(values) - 1, "近窗", "#fee2e2")]:
            if 0 <= start <= end < len(values):
                ax.axvspan(start, end, color=fill, alpha=0.65)
                avg = mean(values[start : end + 1])
                ax.hlines(avg, start, end, colors="#dc2626", linestyles=(0, (5, 3)), linewidth=1.5)
                ax.text((start + end) / 2, ax.get_ylim()[1], label, ha="center", va="top", color="#991b1b", fontsize=9)
    elif detector == "isolation_forest":
        window = max(1, int(len(values) * 0.25))
        start = len(values) - window
        if start > 0:
            historical_mean = mean(values[:start])
            ax.axvspan(0, start - 1, color="#e0f2fe", alpha=0.35)
            ax.hlines(historical_mean, 0, start - 1, colors="#0284c7", linestyles=(0, (6, 4)), linewidth=1.5)
            ax.text((start - 1) / 2, historical_mean, f"历史均值 {_fmt(historical_mean)}", ha="center", va="bottom", color="#0369a1", fontsize=9)
        recent_mean = mean(values[start:])
        ax.axvspan(start, len(values) - 1, color="#fef3c7", alpha=0.65)
        ax.hlines(recent_mean, start, len(values) - 1, colors="#d97706", linestyles=(0, (6, 4)), linewidth=1.6)
        ax.text((start + len(values) - 1) / 2, ax.get_ylim()[1], "近期孤立窗口", ha="center", va="top", color="#92400e", fontsize=9)
        ax.text((start + len(values) - 1) / 2, recent_mean, f"近期均值 {_fmt(recent_mean)}", ha="center", va="bottom", color="#b45309", fontsize=9)
    elif detector == "threshold":
        for label, threshold_value in threshold_lines:
            is_upper = "上限" in label or "高于" in label or ">" in label
            color = "#dc2626" if is_upper else "#2563eb"
            ax.axhline(threshold_value, color=color, linestyle=(0, (7, 4)), linewidth=1.7)
            ax.text(x[-1], threshold_value, f"{label} {_fmt(threshold_value)}", ha="right", va="bottom", color=color, fontsize=9)

    ax.set_xticks(x)
    ax.set_xticklabels(times, rotation=45, ha="right", fontstyle="italic", fontsize=8.5, color="#6b7280")
    ax.tick_params(axis="y", labelsize=9, colors="#6b7280")
    ax.tick_params(axis="x", length=3, pad=1, colors="#9ca3af")

    ax.set_ylabel("")
    ax.set_xlabel("")
    for spine in ax.spines.values():
        spine.set_color("#e5e7eb")

    chart_title = title or f"{metric} 异常趋势图"
    fig.text(0.5, 0.972, chart_title, ha="center", va="top", fontsize=15, color="#111827")
    if dimensions:
        fig.text(0.5, 0.905, f"维度：{dimensions}", ha="center", va="top", fontsize=9.8, color="#475569")
        algo_y, reason_y = 0.868, 0.812

    else:
        algo_y, reason_y = 0.900, 0.840
    fig.text(0.5, algo_y, f"算法：{detector} · {DETECTOR_NOTES.get(detector, '自定义规则')}", ha="center", va="top", fontsize=9.8, color="#64748b")

    if reason:
        fig.text(0.5, reason_y, f"异常原因：{_shorten(reason, 74)}", ha="center", va="top", fontsize=9.6, color="#475569")
    fig.text(plot_left, plot_top + 0.018, "指标值", ha="left", va="top", fontsize=10, color="#64748b")

    if severity:
        badge_fill, badge_text = SEVERITY_COLORS.get(severity, ("#f3f4f6", "#4b5563"))
        badge_right, badge_y, badge_w, badge_h = plot_right, 0.928, 0.115, 0.042
        badge_x = badge_right - badge_w
        badge = FancyBboxPatch(
            (badge_x, badge_y),
            badge_w,
            badge_h,
            boxstyle="round,pad=0.004,rounding_size=0.015",
            transform=fig.transFigure,
            facecolor=badge_fill,
            edgecolor=badge_text,
            linewidth=0.8,
            alpha=0.95,
            zorder=10,
        )
        fig.patches.append(badge)
        fig.text(badge_x + badge_w / 2, badge_y + badge_h / 2, f"异常等级：{severity}", ha="center", va="center", fontsize=10, color=badge_text, zorder=11)


    Path(output).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, format="jpg", dpi=dpi, facecolor="white", pil_kwargs={"quality": quality})
    plt.close(fig)


render_svg = render_jpg




def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render anomaly time-series chart")
    parser.add_argument("input", nargs="?", help="CSV/TSV input file. Omit to read stdin.")
    parser.add_argument("--time-col", default="日期")
    parser.add_argument("--metric-col", required=True)
    parser.add_argument("--detector", required=True, choices=sorted(DETECTORS))
    parser.add_argument("--period", type=int, default=7)
    parser.add_argument("--output", required=True, help="Output JPG image path")
    parser.add_argument("--title", default="")
    parser.add_argument("--severity", default="", help="Anomaly severity label, e.g. 高/中/低")
    parser.add_argument("--reason", default="", help="Short anomaly reason shown on chart")
    parser.add_argument("--dimensions", default="", help="Dimension text shown on chart, e.g. 城市=武汉、车型=惠享")
    parser.add_argument("--width", type=int, default=1000)

    parser.add_argument("--height", type=int, default=520)
    parser.add_argument("--dpi", type=int, default=120)
    parser.add_argument("--quality", type=int, default=92)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    times, values = read_series(args.input, args.time_col, args.metric_col)
    render_jpg(
        times=times,
        values=values,
        metric=args.metric_col,
        detector=args.detector,
        output=args.output,
        title=args.title,
        period=args.period,
        width=args.width,
        height=args.height,
        severity=args.severity,
        reason=args.reason,
        dimensions=args.dimensions,
        dpi=args.dpi,

        quality=args.quality,
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


