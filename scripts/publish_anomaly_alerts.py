#!/usr/bin/env python3
"""Detect anomalies, render charts, upload medium/high alert images to OSS, and write alert rows to ODPS."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import random
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import anomaly_detect as det
import render_anomaly_chart as chart
from metadata_loader import (
    DEFAULT_METADATA_SQL,
    apply_metadata_period_filter,
    detector_config_from_text,
    infer_dimension_cols,
    infer_metric_cols,
    infer_time_col,
    load_active_metadata,
    monitor_period_matches,
    monitor_period_to_granularity,
    parse_ex_prompt_options,
    split_cols,
)

from odps_io import config_value, execute_odps_sql, load_config, query_odps_rows, require_config

DEFAULT_ALERT_TABLE = "ygcx_dw_pro.ads_shaobing_busi_detect_result"
DEFAULT_DETAIL_TABLE = "ygcx_dw_pro.ads_shaobing_busi_detect_detail"
DEFAULT_TASK_NAME = "算法异常检测告警"
PREPRODUCTION_TITLE_PREFIX = "预发测试-"
PIPELINE_VERSION = "20260729-unified-release-v1"
PIPELINE_FILES = (
    "anomaly_detect.py",
    "metadata_loader.py",
    "odps_io.py",
    "publish_anomaly_alerts.py",
    "render_anomaly_chart.py",
)

DEFAULT_OSS_DIR = "shaobing-uploaded-images/"
DEFAULT_OSS_URL_EXPIRE_SECONDS = 30 * 24 * 60 * 60
SEVERITY_RANK = {"无": 0, "低": 1, "中": 2, "高": 3}
LOG_LOCK = threading.RLock()
CHART_LOCK = threading.Lock()


@dataclass
class PublishedAlert:
    dimensions: Dict[str, str]
    metric: str
    current_time: str
    current_value: float
    severity: str
    confidence: float
    detector: str
    reason: str
    ex_prompt: str
    chart_path: str
    img_url: str
    sql: str


@dataclass
class PromptThresholdRule:
    metric: str
    operator: str
    operator_text: str
    threshold: float
    severity: str


class TeeStream:

    def __init__(self, *streams: object) -> None:
        self.streams = streams
        self._line_start = True

    def _with_timestamp(self, data: str) -> str:
        if not data:
            return data
        parts: List[str] = []
        for chunk in data.splitlines(True):
            if self._line_start and chunk:
                parts.append(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ")
            parts.append(chunk)
            self._line_start = chunk.endswith("\n") or chunk.endswith("\r")
        return "".join(parts)

    def write(self, data: str) -> int:
        with LOG_LOCK:
            text = self._with_timestamp(data)
            for stream in self.streams:
                stream.write(text)
                stream.flush()
        return len(data)

    def flush(self) -> None:
        with LOG_LOCK:
            for stream in self.streams:
                stream.flush()

    def isatty(self) -> bool:
        return any(getattr(stream, "isatty", lambda: False)() for stream in self.streams)



def default_workspace_dir() -> Path:
    for name in ("QODER_WORKSPACE", "WORKSPACE", "CODEBUDDY_WORKSPACE"):
        value = os.getenv(name, "").strip()
        if value:
            return Path(value)
    cloud_workspace = Path("/data/workspace")
    if cloud_workspace.is_dir():
        return cloud_workspace
    return Path(__file__).resolve().parents[4]


def resolve_output_dir(path: str) -> Path:
    value = (path or "").strip()
    if not value:
        return default_workspace_dir()
    output_path = Path(value)
    if output_path.is_absolute():
        return output_path
    return default_workspace_dir() / output_path


def default_log_dir() -> Path:
    return default_workspace_dir() / "logs"


def cleanup_log_files(log_dir: Path, keep_days: int = 7) -> None:
    log_files = sorted(log_dir.glob("publish_*.log"), key=lambda p: p.name, reverse=True)
    for old_log in log_files[keep_days:]:
        try:
            old_log.unlink()
        except OSError as exc:
            print(f"[LOG_CLEANUP] 删除旧日志失败: {old_log}，原因: {exc}")


def setup_file_logging(log_dir: str = "", log_file: str = ""):
    log_path = Path(log_file) if log_file else Path(log_dir or default_log_dir()) / f"publish_{datetime.now().strftime('%Y%m%d')}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cleanup_log_files(log_path.parent)
    log_handle = log_path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = TeeStream(sys.__stdout__, log_handle)
    sys.stderr = TeeStream(sys.__stderr__, log_handle)
    print(f"[LOG_FILE] {log_path}")
    return log_handle


def build_code_fingerprint() -> str:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in PIPELINE_FILES:
        path = root / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def checkpoint_state(code_fingerprint: str) -> Dict[str, Any]:
    return {
        "pipeline_version": PIPELINE_VERSION,
        "code_fingerprint": code_fingerprint,
        "completed_metadata_ids": [],
        "detected_alerts": {},
    }


def ensure_run_id(args: argparse.Namespace) -> str:
    run_id = args.run_id.strip()
    if not run_id:
        run_id = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        args.run_id = run_id
        print(f"[RUN_ID] {run_id}（新提交；若进程重启，请使用 --run-id {run_id} 续跑）")
    return run_id


def checkpoint_path(args: argparse.Namespace) -> Path:
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", ensure_run_id(args))
    return Path(args.checkpoint_dir or default_log_dir() / "checkpoints") / f"publish-{safe_run_id}.json"


def load_checkpoint(path: Path, resume: bool, code_fingerprint: str) -> Dict[str, Any]:
    if not resume or not path.is_file():
        return checkpoint_state(code_fingerprint)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"运行检查点损坏，无法安全恢复: {path}: {exc}") from exc
    if data.get("pipeline_version") != PIPELINE_VERSION or data.get("code_fingerprint") != code_fingerprint:
        print(
            "[RESUME] 检查点代码版本与当前脚本不一致，放弃旧检测缓存并重新检测 | "
            f"checkpoint_version={data.get('pipeline_version', '')} | current_version={PIPELINE_VERSION}"
        )
        return checkpoint_state(code_fingerprint)
    print(f"[RESUME] 加载运行检查点: {path}")
    return {
        "pipeline_version": PIPELINE_VERSION,
        "code_fingerprint": code_fingerprint,
        "completed_metadata_ids": list(data.get("completed_metadata_ids", [])),
        "detected_alerts": dict(data.get("detected_alerts", {})),
    }


def save_checkpoint(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


class CheckpointStore:
    """Thread-safe checkpoint state for parallel metadata workers."""

    def __init__(self, path: Path, state: Dict[str, Any], enabled: bool) -> None:
        self.path = path
        self.enabled = enabled
        self._state = state
        self._lock = threading.RLock()

    def is_completed(self, metadata_id: str) -> bool:
        with self._lock:
            return str(metadata_id) in {str(value) for value in self._state["completed_metadata_ids"]}

    def get_detected(self, metadata_id: str) -> Optional[List[Dict[str, Any]]]:
        with self._lock:
            value = self._state["detected_alerts"].get(str(metadata_id))
            return json.loads(json.dumps(value, ensure_ascii=False)) if value is not None else None

    def save_detected(self, metadata_id: str, alerts: List[det.SeriesAlert]) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._state["detected_alerts"][str(metadata_id)] = [serialize_alert(alert) for alert in alerts]
            save_checkpoint(self.path, self._state)

    def complete(self, metadata_id: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            completed = {str(value) for value in self._state["completed_metadata_ids"]}
            completed.add(str(metadata_id))
            self._state["completed_metadata_ids"] = sorted(completed)
            self._state["detected_alerts"].pop(str(metadata_id), None)
            save_checkpoint(self.path, self._state)


def serialize_alert(alert: det.SeriesAlert) -> Dict[str, Any]:
    return {
        "dimensions": alert.dimensions,
        "metric": alert.metric,
        "current_time": alert.current_time,
        "current_value": alert.current_value,
        "is_anomaly": alert.is_anomaly,
        "severity": alert.severity,
        "final_alert": alert.final_alert,
        "main_detector": alert.main_detector,
        "confidence": alert.confidence,
        "anomaly_type": alert.anomaly_type,
        "reason": alert.reason,
        "convergence_notes": alert.convergence_notes,
    }


def deserialize_alert(value: Dict[str, Any]) -> det.SeriesAlert:
    return det.SeriesAlert(
        dict(value.get("dimensions", {})),
        str(value.get("metric", "")),
        str(value.get("current_time", "")),
        float(value.get("current_value", 0)),
        bool(value.get("is_anomaly", True)),
        str(value.get("severity", "中")),
        bool(value.get("final_alert", True)),
        str(value.get("main_detector", "")),
        float(value.get("confidence", 0)),
        str(value.get("anomaly_type", "")),
        str(value.get("reason", "")),
        [],
        [],
        list(value.get("convergence_notes", [])),
    )


def next_five_minute_node(now: datetime) -> Tuple[str, str, str, str]:
    minutes_to_add = (5 - (now.minute % 5)) % 5
    if minutes_to_add == 0:
        minutes_to_add = 5
    next_node = now + timedelta(minutes=minutes_to_add)
    if next_node.day != now.day:
        next_node = next_node.replace(hour=0, minute=0)
    return next_node.strftime("%Y%m%d"), next_node.strftime("%H"), next_node.strftime("%M"), next_node.strftime("%H%M")


def sql_str(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text.replace("'", "''") + "'"


def print_step(step: str) -> None:
    print(f"\n[FLOW] {step}")


def print_sql_block(title: str, sql: str) -> None:
    print(f"\n========== {title} ==========")
    print(sql.strip())
    print(f"========== END {title} ==========\n")


def fmt_dims(dimensions: Dict[str, str]) -> str:
    return "、".join(f"{k}={v}" for k, v in dimensions.items()) or "全局"


def fmt_num(value: float) -> str:
    if abs(value) >= 100 or float(value).is_integer():
        return f"{value:.0f}"
    return f"{value:.2f}"


def fmt_result_value(value: float) -> str:
    return format(float(value), ".15g")


def compact_text(text: str, limit: int = 500) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    return value if len(value) <= limit else value[:limit].rstrip() + "..."


def split_option_values(text: str) -> List[str]:
    return [part.strip() for part in re.split(r"[,，;；、/|\s]+", text or "") if part.strip()]


def option_value(options: Dict[str, str], *keys: str) -> str:
    lowered = {str(k).strip().lower(): str(v).strip() for k, v in options.items() if str(v).strip()}
    for key in keys:
        direct = str(options.get(key, "")).strip()
        if direct:
            return direct
        normalized = lowered.get(key.lower(), "")
        if normalized:
            return normalized
    return ""


def prompt_publish_levels(options: Dict[str, str], ex_prompt: str, default_levels: set[str]) -> set[str]:
    value = option_value(options, "severity_levels", "发布等级", "告警等级")
    if value:
        levels = {level for level in split_option_values(value) if level in SEVERITY_RANK}
        if levels:
            return levels
    text = ex_prompt or ""
    if re.search(r"(只|仅).{0,4}(高|高等级|高危)", text):
        return {"高"}
    if re.search(r"(发布|包含|保留).{0,6}低", text):
        return {"低", "中", "高"}
    return default_levels


def prompt_low_base_downgrade(options: Dict[str, str], ex_prompt: str, default_enabled: bool) -> bool:
    value = option_value(options, "low_base_downgrade", "低基数降权")
    if value:
        normalized = value.strip().lower()
        return normalized not in {"off", "false", "0", "否", "不", "关闭", "禁用", "不降权"}
    if re.search(r"(关闭|禁用|不启用|不进行|不要).{0,6}低基数.{0,4}降权|低基数.{0,4}不降权", ex_prompt or ""):
        return False
    return default_enabled


def prompt_period(options: Dict[str, str], default_period: int) -> int:
    value = option_value(options, "period", "周期", "检测周期")
    if value:
        match = re.search(r"\d+", value)
        if match:
            return max(1, int(match.group(0)))
    return default_period


def _normalize_prompt_metric(text: str) -> str:
    metric = re.sub(r"^(如果|当|若|当前|最新)", "", (text or "").strip())
    if metric.endswith("指标值") and metric != "指标值":
        metric = metric[: -len("指标值")]
    if metric in {"", "指标", "指标值", "当前值", "数值", "值", "result"}:
        return ""
    return metric


def parse_prompt_threshold_rules(ex_prompt: str) -> List[PromptThresholdRule]:
    text = ex_prompt or ""
    severity_match = re.search(r"(?:发布等级|告警等级|severity_levels?)\s*[:=：]?\s*(高|中|低)", text, re.IGNORECASE)
    severity = severity_match.group(1) if severity_match else ("高" if re.search(r"高危|高等级|严重|紧急", text) else "中")
    pattern = re.compile(
        r"(?:(?P<metric>[A-Za-z0-9_\u4e00-\u9fa5]+?)\s*)?"
        r"(?P<op>大于等于|小于等于|不低于|不少于|不超过|超过|大于|高于|低于|小于|少于|>=|<=|>|<)"
        r"\s*(?P<threshold>-?\d+(?:\.\d+)?)"
    )
    op_map = {
        "超过": "gt",
        "大于": "gt",
        "高于": "gt",
        ">": "gt",
        "大于等于": "ge",
        "不低于": "ge",
        "不少于": "ge",
        ">=": "ge",
        "低于": "lt",
        "小于": "lt",
        "少于": "lt",
        "<": "lt",
        "小于等于": "le",
        "不超过": "le",
        "<=": "le",
    }
    rules: List[PromptThresholdRule] = []
    for match in pattern.finditer(text):
        operator_text = match.group("op")
        operator = op_map.get(operator_text)
        if not operator:
            continue
        rules.append(
            PromptThresholdRule(
                metric=_normalize_prompt_metric(match.group("metric") or ""),
                operator=operator,
                operator_text=operator_text,
                threshold=float(match.group("threshold")),
                severity=severity,
            )
        )
    return rules


def threshold_rule_matches(value: float, rule: PromptThresholdRule) -> bool:
    if rule.operator == "gt":
        return value > rule.threshold
    if rule.operator == "ge":
        return value >= rule.threshold
    if rule.operator == "lt":
        return value < rule.threshold
    if rule.operator == "le":
        return value <= rule.threshold
    return False


def metric_threshold_rules(metric: str, rules: Sequence[PromptThresholdRule]) -> List[PromptThresholdRule]:
    return [rule for rule in rules if not rule.metric or rule.metric == metric]


def threshold_rules_allow_value(metric: str, value: float, rules: Sequence[PromptThresholdRule]) -> bool:
    applicable_rules = metric_threshold_rules(metric, rules)
    return not applicable_rules or any(threshold_rule_matches(value, rule) for rule in applicable_rules)


def safe_name(text: str) -> str:



    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "_", text.strip())
    return cleaned.strip("_") or "alert"


def collect_series(
    rows: Sequence[Dict[str, str]],
    time_col: str,
    dimension_cols: Sequence[str],
    dimensions: Dict[str, str],
    metric: str,
) -> Tuple[List[str], List[float]]:
    pairs: List[Tuple[str, float]] = []
    for row in rows:
        if any(str(row.get(col, "")) != str(dimensions.get(col, "")) for col in dimension_cols):
            continue
        value = det._safe_float(row.get(metric))
        time_value = str(row.get(time_col, ""))
        if time_value and value is not None:
            pairs.append((time_value, value))
    pairs.sort(key=lambda x: x[0])
    return [t for t, _ in pairs], [v for _, v in pairs]


def build_prompt_threshold_alerts(
    rows: Sequence[Dict[str, str]],
    time_col: str,
    dimension_cols: Sequence[str],
    metric_cols: Sequence[str],
    rules: Sequence[PromptThresholdRule],
) -> List[det.SeriesAlert]:
    if not rules:
        return []
    groups: Dict[Tuple[Tuple[str, str], ...], List[Dict[str, str]]] = {}
    for row in rows:
        key = tuple((col, str(row.get(col, ""))) for col in dimension_cols)
        groups.setdefault(key, []).append(row)

    alerts: List[det.SeriesAlert] = []
    for key, group_rows in groups.items():
        latest_row = sorted(group_rows, key=lambda r: str(r.get(time_col, "")))[-1]
        dimensions = dict(key)
        current_time = str(latest_row.get(time_col, ""))
        for metric in metric_cols:
            value = det._safe_float(latest_row.get(metric))
            if value is None:
                continue
            for rule in rules:
                if rule.metric and rule.metric != metric:
                    continue
                if not threshold_rule_matches(value, rule):
                    continue
                reason = f"个性化规则命中：当前值{fmt_num(value)}{rule.operator_text}{fmt_num(rule.threshold)}"
                result = det.DetectionResult(
                    "custom_threshold",
                    True,
                    1.0,
                    "自定义阈值",
                    abs(value - rule.threshold),
                    reason,
                    {"operator": rule.operator, "threshold": rule.threshold, "ex_prompt_rule": True},
                )
                alerts.append(
                    det.SeriesAlert(
                        dimensions,
                        metric,
                        current_time,
                        value,
                        True,
                        rule.severity,
                        True,
                        "custom_threshold",
                        1.0,
                        "自定义阈值",
                        reason,
                        [result],
                        [result],
                        ["命中 ex_prompt 个性化阈值规则"],
                    )
                )
                break
    return alerts


def build_chart_file_name(now: Optional[datetime] = None, stable_key: str = "") -> str:
    """Build a retry-stable chart name while preserving the established file format."""
    now = now or datetime.now()
    if stable_key:
        timestamp = now.strftime("%Y%m%d") + "000000"
        suffix = f"{int(stable_key[:12], 16) % 100000:05d}"
    else:
        timestamp = now.strftime("%Y%m%d%H%M%S")
        suffix = f"{random.randint(0, 99999):05d}"
    return f"{timestamp}-{suffix}.jpg"


def build_oss_object_name(file_name: str, now: Optional[datetime] = None) -> str:
    """Build OSS object key: shaobing-uploaded-images/${yyyymmdd}/${文件名}."""
    now = now or datetime.now()
    return "/".join(
        [
            DEFAULT_OSS_DIR.strip("/"),
            now.strftime("%Y%m%d"),
            safe_name(file_name),
        ]
    )



def upload_image_to_oss(image_file: str, config: Dict[str, str], object_name: str = "") -> str:

    import oss2

    path = Path(image_file)

    if not path.is_file():
        raise FileNotFoundError(f"图片文件不存在: {path}")

    values = require_config(
        config,
        {
            "ALIYUN_ACCESS_KEY_ID": ("oss_access_key_id", "ALIYUN_ACCESS_KEY_ID"),
            "ALIYUN_ACCESS_KEY_SECRET": ("oss_access_key_secret", "ALIYUN_ACCESS_KEY_SECRET"),
            "OSS_BUCKET_NAME": ("oss_bucket_name", "OSS_BUCKET_NAME"),
            "OSS_REGION": ("oss_region", "OSS_REGION"),
        },
    )
    bucket_name = values["OSS_BUCKET_NAME"]
    region = values["OSS_REGION"]
    endpoint = config_value(config, "oss_endpoint", "OSS_ENDPOINT", f"https://{region}.aliyuncs.com")
    oss_dir = config_value(config, "oss_dir", "OSS_DIR", DEFAULT_OSS_DIR)
    public_host = config_value(config, "oss_public_host", "OSS_PUBLIC_HOST")
    expire_text = config_value(config, "oss_url_expire_seconds", "OSS_URL_EXPIRE_SECONDS")
    expire_seconds = int(expire_text) if expire_text else DEFAULT_OSS_URL_EXPIRE_SECONDS

    if object_name:
        normalized_object_name = object_name.lstrip("/")
        oss_object_name = (
            normalized_object_name
            if normalized_object_name.startswith((oss_dir, DEFAULT_OSS_DIR))
            else f"{oss_dir}{normalized_object_name}"
        )
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S%f")
        oss_object_name = f"{oss_dir}{timestamp}{path.suffix.lower()}"


    auth = oss2.Auth(values["ALIYUN_ACCESS_KEY_ID"], values["ALIYUN_ACCESS_KEY_SECRET"])
    bucket = oss2.Bucket(auth, endpoint, bucket_name)
    result = bucket.put_object_from_file(
        oss_object_name,
        str(path),
        headers={
            "Cache-Control": f"max-age={expire_seconds}",
            "Content-Type": mimetypes.guess_type(str(path))[0] or "application/octet-stream",
        },
    )
    if result.status != 200:
        raise RuntimeError(f"OSS 上传失败，状态码: {result.status}")

    signed_url = bucket.sign_url("GET", oss_object_name, expire_seconds)
    if public_host:
        source_host = f"{bucket_name}.{region}.aliyuncs.com"
        signed_url = signed_url.replace(source_host, public_host)
    return signed_url



def build_task_title(business_name: str, task_name: str, publish_mode: str) -> str:
    """Build an environment-aware alert title without duplicating the preproduction prefix."""
    requested_title = (task_name or "").strip()
    clean_business_name = (business_name or "").strip()
    if requested_title and requested_title != DEFAULT_TASK_NAME:
        base_title = requested_title
    elif clean_business_name and clean_business_name != DEFAULT_TASK_NAME:
        base_title = f"{clean_business_name}异常检测告警"
    else:
        base_title = DEFAULT_TASK_NAME

    while base_title.startswith(PREPRODUCTION_TITLE_PREFIX):
        base_title = base_title[len(PREPRODUCTION_TITLE_PREFIX):].lstrip()
    if publish_mode == "preproduction":
        return f"{PREPRODUCTION_TITLE_PREFIX}{base_title}"
    return base_title


def build_alert_idempotency_key(run_id: str, metadata_id: str, task_name: str, alert: det.SeriesAlert) -> str:
    payload = {
        "run_id": run_id,
        "metadata_id": str(metadata_id or ""),
        "title": task_name,
        "metric": alert.metric,
        "dimensions": sorted((str(k), str(v)) for k, v in alert.dimensions.items()),
        "source_time": str(alert.current_time),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_published_identities(
    table: str,
    task_name: str,
    config: Dict[str, str],
    lookback_days: int = 30,
) -> set[str]:
    start_dt = (datetime.now() - timedelta(days=max(1, lookback_days))).strftime("%Y%m%d")
    sql = f"""SELECT GET_JSON_OBJECT(content, '$.idempotency_key') AS idempotency_key
FROM {table}
WHERE dt >= {sql_str(start_dt)}
  AND title = {sql_str(task_name)};"""
    rows = query_odps_rows(sql, config, label=f"批量加载告警幂等状态 | {task_name}")
    return {row.get("idempotency_key", "") for row in rows if row.get("idempotency_key", "")}


def build_alert_sql(
    table: str,
    task_name: str,
    alert: det.SeriesAlert,
    img_url: str,
    owner: str,
    ex_prompt: str = "",
    run_id: str = "",
    idempotency_key: str = "",
    publish_mode: str = "",
    pipeline_version: str = PIPELINE_VERSION,
    code_fingerprint: str = "",
    deduplicate: bool = True,
    lookback_days: int = 30,
    now: Optional[datetime] = None,
) -> str:
    now = now or datetime.now()
    dt, hh, mi, hhmi = next_five_minute_node(now)
    start_dt = (now - timedelta(days=max(1, lookback_days))).strftime("%Y%m%d")
    dimensions = fmt_dims(alert.dimensions)
    prompt_text = compact_text(ex_prompt)
    analysis = f"[算法检测] {dimensions} / {alert.metric} 异常，当前值{fmt_num(alert.current_value)}。{alert.reason}"
    if prompt_text:
        analysis = f"{analysis}；个性化要求：{prompt_text}"
    detail = f"{alert.metric}|{dimensions}|{alert.reason}"
    dedupe_clause = ""
    if deduplicate and idempotency_key:
        dedupe_clause = f"""
WHERE NOT EXISTS (
    SELECT 1 FROM {table} existing
    WHERE existing.dt >= {sql_str(start_dt)}
      AND GET_JSON_OBJECT(existing.content, '$.idempotency_key') = {sql_str(idempotency_key)}
)"""
    return f"""set odps.sql.type.system.odps2=true;

INSERT INTO TABLE {table} PARTITION (dt,hh,mi)
select
    *
    ,if(event is not null,{sql_str(dt)},'00000000') as dt
    ,if(event is not null,{sql_str(hh)},'00') as hh
    ,if(event is not null,{sql_str(mi)},'00') as mi
from(
    SELECT  'alarm_auto_zn' AS event
        ,{sql_str(task_name)} AS title
        ,{sql_str(fmt_result_value(alert.current_value))} AS result
        ,{sql_str(alert.severity)} AS severity

        ,{sql_str(img_url)} AS img_url
        ,TO_JSON(
                NAMED_STRUCT(
                    'analysis', {sql_str(analysis)},
                    'img_url', {sql_str(img_url)},
                    'data_period', {sql_str(now.strftime('%Y-%m-%d'))},
                    'alert_time', {sql_str(now.strftime('%Y-%m-%d %H:%M:%S'))},
                    'detail', {sql_str(detail)},
                    'detector', {sql_str(alert.main_detector)},
                    'confidence', {sql_str(f'{alert.confidence:.3f}')},
                    'ex_prompt', {sql_str(prompt_text)},
                    'source_time', {sql_str(alert.current_time)},
                    'run_id', {sql_str(run_id)},
                    'idempotency_key', {sql_str(idempotency_key)},
                    'publish_mode', {sql_str(publish_mode)},
                    'pipeline_version', {sql_str(pipeline_version)},
                    'code_fingerprint', {sql_str(code_fingerprint)}
                )

        ) AS content
        ,{sql_str(owner)} AS create_user
        ,CAST(GETDATE() AS TIMESTAMP) AS create_time
        ,{sql_str(alert.metric)} AS metric_name
        ,1 AS alarm_detector
    FROM (

        SELECT {sql_str(dt)} as dt, {sql_str(hhmi)} as hhmi, '' as quota_detail
        FROM (SELECT 1) tmp
    ) ta
    GROUP BY dt, hhmi
) t{dedupe_clause};"""


def build_metadata_detail_sql(
    table: str,
    metadata_id: str,
    business_name: str,
    now: Optional[datetime] = None,
) -> str:
    now = now or datetime.now()
    return f"""set odps.sql.type.system.odps2=true;
INSERT INTO TABLE {table} PARTITION (dt={sql_str(now.strftime('%Y%m%d'))})
SELECT  CAST({sql_str(metadata_id)} AS BIGINT) AS id
        ,{sql_str(business_name)} AS business_name
        ,CAST(GETDATE() AS TIMESTAMP) AS create_time
FROM (SELECT 1) seed
WHERE NOT EXISTS (
    SELECT 1 FROM {table} existing
    WHERE existing.dt = {sql_str(now.strftime('%Y%m%d'))}
      AND CAST(existing.id AS STRING) = {sql_str(metadata_id)}
);"""



def publish_metadata_detail(
    table: str,
    metadata_id: str,
    business_name: str,
    config: Dict[str, str],
    execute: bool,
) -> str:
    sql = build_metadata_detail_sql(table, metadata_id, business_name)
    print_step(f"写入检测明细表 | {table} | {business_name}")
    print_sql_block("检测明细写入SQL", sql)
    if execute:
        execute_odps_sql(sql, config, label=f"检测明细写入SQL | {business_name}")
    return sql


def detect_selected_alerts(
    rows: List[Dict[str, str]],
    *,
    time_col: str,
    dimension_cols: Sequence[str],
    metric_cols: Sequence[str],
    granularity: str,
    period: int,
    low_base_downgrade: bool,
    publish_levels: set[str],
    business_name: str,
    detector_config: Optional[Dict[str, Any]] = None,
    ex_prompt: str = "",
) -> List[det.SeriesAlert]:
    print_step(f"异常检测 | {business_name} | 指标={','.join(metric_cols)}")
    alerts = det.analyze_table(
        rows,
        time_col=time_col,
        dimension_cols=dimension_cols,
        metric_cols=metric_cols,
        granularity=granularity,
        period_hint=period,
        latest_only=True,
        enable_low_base_downgrade=low_base_downgrade,
        detector_config=detector_config,
    )
    threshold_rules = parse_prompt_threshold_rules(ex_prompt)
    selected: List[det.SeriesAlert] = []
    for alert in alerts:
        if not (alert.final_alert and alert.severity in publish_levels and alert.main_detector):
            continue
        if not threshold_rules_allow_value(alert.metric, alert.current_value, threshold_rules):
            print_step(
                f"阈值门禁过滤 | {business_name} | {fmt_dims(alert.dimensions)} | "
                f"{alert.metric} | 当前值={fmt_num(alert.current_value)}"
            )
            continue
        selected.append(alert)
    seen = {(a.metric, tuple(sorted(a.dimensions.items()))) for a in selected}
    for threshold_alert in build_prompt_threshold_alerts(
        rows, time_col, dimension_cols, metric_cols, threshold_rules
    ):
        key = (threshold_alert.metric, tuple(sorted(threshold_alert.dimensions.items())))
        if key not in seen and threshold_alert.severity in publish_levels:
            selected.append(threshold_alert)
            seen.add(key)
    selected.sort(key=lambda a: (SEVERITY_RANK.get(a.severity, 0), a.confidence, abs(a.current_value)), reverse=True)
    return selected


def publish_alerts_for_rows(
    rows: List[Dict[str, str]],
    *,
    time_col: str,
    dimension_cols: Sequence[str],
    metric_cols: Sequence[str],
    granularity: str,
    period: int,
    low_base_downgrade: bool,
    publish_levels: set[str],
    business_name: str,
    task_name: str,
    owner: str,
    alert_table: str,
    output_dir: Path,
    config: Dict[str, str],
    execute: bool,
    detector_config: Optional[Dict[str, Any]] = None,
    ex_prompt: str = "",
    metadata_id: str = "",
    run_id: str = "",
    publish_mode: str = "",
    pipeline_version: str = PIPELINE_VERSION,
    code_fingerprint: str = "",
    force_republish: bool = False,
    idempotency_lookback_days: int = 30,
    selected_alerts: Optional[List[det.SeriesAlert]] = None,
) -> List[PublishedAlert]:
    selected = selected_alerts
    if selected is None:
        selected = detect_selected_alerts(
            rows,
            time_col=time_col,
            dimension_cols=dimension_cols,
            metric_cols=metric_cols,
            granularity=granularity,
            period=period,
            low_base_downgrade=low_base_downgrade,
            publish_levels=publish_levels,
            business_name=business_name,
            detector_config=detector_config,
            ex_prompt=ex_prompt,
        )
    published: List[PublishedAlert] = []
    existing_keys: set[str] = set()
    if execute and not force_republish and selected:
        existing_keys = load_published_identities(
            alert_table, task_name, config, idempotency_lookback_days
        )
        print_step(f"告警幂等状态已加载 | {business_name} | keys={len(existing_keys)}")

    for alert in selected:
        times, values = collect_series(rows, time_col, dimension_cols, alert.dimensions, alert.metric)
        if not times:
            continue
        dims = fmt_dims(alert.dimensions)
        idempotency_key = build_alert_idempotency_key(run_id, metadata_id, task_name, alert)
        if execute and not force_republish and idempotency_key in existing_keys:
            print_step(f"跳过已发布告警 | {business_name} | {dims} | {alert.metric} | key={idempotency_key[:12]}")
            continue
        publish_time = datetime.now()
        chart_file_name = build_chart_file_name(publish_time, stable_key=idempotency_key)
        object_name = build_oss_object_name(chart_file_name, publish_time)
        chart_path = output_dir / Path(object_name)
        chart_path.parent.mkdir(parents=True, exist_ok=True)
        print_step(f"生成图表 | {business_name} | {dims} | {alert.metric}")
        with CHART_LOCK:
            chart.render_jpg(
                times=times,
                values=values,
                metric=alert.metric,
                detector=alert.main_detector,
                output=str(chart_path),
                title=f"{business_name}/{alert.metric} 异常趋势图",
                period=period,
                severity=alert.severity,
                reason=alert.reason,
                dimensions=dims,
            )
        img_url = ""
        if execute:
            print_step(f"上传OSS | {object_name}")
            img_url = upload_image_to_oss(str(chart_path), config, object_name)

        sql = build_alert_sql(
            alert_table,
            task_name,
            alert,
            img_url,
            owner,
            ex_prompt=ex_prompt,
            run_id=run_id,
            idempotency_key=idempotency_key,
            publish_mode=publish_mode,
            pipeline_version=pipeline_version,
            code_fingerprint=code_fingerprint,
            deduplicate=not force_republish,
            lookback_days=idempotency_lookback_days,
        )
        if execute:

            print_step(f"写入告警表 | {alert_table} | {business_name} | {alert.metric}")
            print_sql_block("告警写入SQL", sql)
            execute_odps_sql(sql, config, label=f"告警写入SQL | {business_name} | {alert.metric}")
            existing_keys.add(idempotency_key)

        published.append(
            PublishedAlert(
                dimensions=alert.dimensions,
                metric=alert.metric,
                current_time=alert.current_time,
                current_value=alert.current_value,
                severity=alert.severity,
                confidence=alert.confidence,
                detector=alert.main_detector,
                reason=alert.reason,
                ex_prompt=compact_text(ex_prompt),
                chart_path=img_url or str(chart_path),

                img_url=img_url,
                sql=sql,
            )
        )

    return published


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect, chart, upload, and publish medium/high anomaly alerts")
    parser.add_argument("input", nargs="?", help="CSV/TSV input file. Omit when using --from-metadata.")
    parser.add_argument("--from-metadata", action="store_true", help="Read active business metadata from ODPS and execute each data_sql.")
    parser.add_argument("--metadata-sql", default=DEFAULT_METADATA_SQL, help="SQL used by --from-metadata to load business metadata.")
    parser.add_argument("--time-col", default="日期")
    parser.add_argument("--dimension-cols", default="")
    parser.add_argument("--metric-cols", default="")
    parser.add_argument("--granularity", default="daily", choices=["minutely", "hourly", "daily", "weekly", "monthly"])
    parser.add_argument("--period", type=int, default=7)
    parser.add_argument("--low-base-downgrade", choices=["on", "off"], default="on")
    parser.add_argument("--severity-levels", default="中,高", help="Comma-separated severities to publish")
    parser.add_argument("--task-name", default=DEFAULT_TASK_NAME)
    parser.add_argument(
        "--publish-mode",
        choices=["production", "preproduction"],
        default="production",
        help="Title environment: preproduction adds '预发测试-'; production removes it.",
    )
    parser.add_argument("--business-name", default="", help="Business name used in OSS path. Defaults to --task-name or metadata business_name.")
    parser.add_argument("--owner", default="test_user")
    parser.add_argument("--detectors", default="", help="Optional detector list. Empty means all detectors.")
    parser.add_argument("--monitor-period", default="", help="Run only metadata rows with this monitor_period, e.g. 日调度, 小时调度, 分钟调度, 1小时, 5分钟.")
    parser.add_argument("--metadata-workers", type=int, default=4, help="Maximum metadata tasks processed concurrently. Default: 4.")
    parser.add_argument("--metadata-retries", type=int, default=2, help="Retries per failed metadata task before the run fails. Default: 2.")

    parser.add_argument("--alert-table", default=DEFAULT_ALERT_TABLE)
    parser.add_argument("--detail-table", default=DEFAULT_DETAIL_TABLE, help="ODPS table used to record metadata rows that completed detection and publishing.")

    parser.add_argument("--output-dir", default="", help="Local JPG output root. Defaults to workspace root, not current working directory.")

    parser.add_argument("--config", default="", help="Optional fallback JSON for OSS/ODPS settings. Environment variables take precedence.")

    parser.add_argument("--log-dir", default="", help="Directory for full run logs. Defaults to workspace logs/.")
    parser.add_argument("--log-file", default="", help="Exact log file path. Overrides --log-dir.")
    parser.add_argument("--checkpoint-dir", default="", help="Persistent resume-state directory. Defaults to logs/checkpoints/.")
    parser.add_argument("--run-id", default="", help="Optional logical run ID. Restarts with the same ID resume progress.")
    parser.add_argument("--no-resume", action="store_true", help="Ignore the existing run checkpoint and start detection from the beginning.")
    parser.add_argument("--idempotency-lookback-days", type=int, default=30, help="Days searched for an existing alert idempotency key.")
    parser.add_argument("--force-republish", action="store_true", help="Explicitly bypass idempotency checks. Use only for intentional replay.")
    parser.add_argument("--execute", action="store_true", help="Upload images and execute ODPS INSERT. Default is dry-run.")
    return parser.parse_args(argv)



def process_metadata_item(
    meta: Any,
    args: argparse.Namespace,
    config: Dict[str, str],
    output_dir: Path,
    checkpoint: CheckpointStore,
    default_publish_levels: set[str],
) -> List[PublishedAlert]:
    business_label = meta.business_name or meta.id or "未命名业务"
    if checkpoint.enabled and checkpoint.is_completed(meta.id):
        print_step(f"恢复运行：跳过已完成元数据 | {business_label} | metadata_id={meta.id}")
        return []

    period_text = meta.monitor_period or "未配置周期"
    ex_prompt = compact_text(meta.ex_prompt, limit=1000)
    prompt_options = parse_ex_prompt_options(meta.ex_prompt)
    if ex_prompt:
        print_step(f"个性化需求 | {business_label} | {ex_prompt}")
    print_step(f"执行业务提数 | {business_label} | monitor_period={period_text}")
    print_sql_block(f"提数SQL | {business_label}", meta.data_sql)
    rows = query_odps_rows(meta.data_sql, config, label=f"提数SQL | {business_label}")

    business_name = args.business_name.strip() or meta.business_name or args.task_name
    if not rows:
        publish_metadata_detail(args.detail_table, meta.id, business_name, config, args.execute)
        checkpoint.complete(meta.id)
        print(f"[CHECKPOINT] 元数据处理完成（无数据） | {business_label}")
        return []

    requested_time_col = option_value(prompt_options, "time_col", "时间列") or args.time_col
    requested_metric_cols = option_value(prompt_options, "metric_cols", "指标列") or args.metric_cols
    requested_dimension_cols = option_value(prompt_options, "dimension_cols", "维度列") or args.dimension_cols
    time_col = infer_time_col(rows, requested_time_col)
    metric_cols = infer_metric_cols(
        rows,
        time_col,
        meta.metric_name,
        requested_metric_cols,
        exclude_cols=split_cols(requested_dimension_cols),
        sql_text=meta.data_sql,
    )
    dimension_cols = infer_dimension_cols(rows, time_col, metric_cols, requested_dimension_cols)
    print_step(
        f"列识别 | {business_label} | 时间列={time_col} | "
        f"维度列={','.join(dimension_cols) or '无'} | 指标列={','.join(metric_cols) or '无'}"
    )
    owner = meta.owner or args.owner
    task_name = build_task_title(business_name, args.task_name, args.publish_mode)
    detection_algor = meta.detection_algor or option_value(prompt_options, "detectors", "检测算法", "算法") or args.detectors
    detector_config = detector_config_from_text(detection_algor)
    granularity = monitor_period_to_granularity(meta.monitor_period, args.granularity)
    effective_period = prompt_period(prompt_options, args.period)
    effective_low_base = prompt_low_base_downgrade(prompt_options, meta.ex_prompt, args.low_base_downgrade == "on")
    publish_levels = prompt_publish_levels(prompt_options, meta.ex_prompt, default_publish_levels)

    cached_alerts = checkpoint.get_detected(meta.id) if checkpoint.enabled else None
    if cached_alerts is not None:
        selected_alerts = [deserialize_alert(value) for value in cached_alerts]
        print_step(f"恢复运行：复用已检测结果 | {business_label} | 告警数={len(selected_alerts)}")
    else:
        selected_alerts = detect_selected_alerts(
            rows,
            time_col=time_col,
            dimension_cols=dimension_cols,
            metric_cols=metric_cols,
            granularity=granularity,
            period=effective_period,
            low_base_downgrade=effective_low_base,
            publish_levels=publish_levels,
            business_name=business_name,
            detector_config=detector_config,
            ex_prompt=ex_prompt,
        )
        checkpoint.save_detected(meta.id, selected_alerts)
        if checkpoint.enabled:
            print(f"[CHECKPOINT] 已保存检测结果 | {business_label} | 告警数={len(selected_alerts)}")

    published = publish_alerts_for_rows(
        rows,
        time_col=time_col,
        dimension_cols=dimension_cols,
        metric_cols=metric_cols,
        granularity=granularity,
        period=effective_period,
        low_base_downgrade=effective_low_base,
        publish_levels=publish_levels,
        business_name=business_name,
        task_name=task_name,
        owner=owner,
        alert_table=args.alert_table,
        output_dir=output_dir,
        config=config,
        execute=args.execute,
        detector_config=detector_config,
        ex_prompt=ex_prompt,
        metadata_id=meta.id,
        run_id=args.run_id,
        publish_mode=args.publish_mode,
        pipeline_version=PIPELINE_VERSION,
        code_fingerprint=args.code_fingerprint,
        force_republish=args.force_republish,
        idempotency_lookback_days=args.idempotency_lookback_days,
        selected_alerts=selected_alerts,
    )
    publish_metadata_detail(args.detail_table, meta.id, business_name, config, args.execute)
    checkpoint.complete(meta.id)
    if checkpoint.enabled:
        print(f"[CHECKPOINT] 元数据处理完成 | {business_label}")
    return published


def process_metadata_with_retry(
    meta: Any,
    args: argparse.Namespace,
    config: Dict[str, str],
    output_dir: Path,
    checkpoint: CheckpointStore,
    default_publish_levels: set[str],
) -> List[PublishedAlert]:
    business_label = meta.business_name or meta.id or "未命名业务"
    attempts = max(0, args.metadata_retries) + 1
    for attempt in range(1, attempts + 1):
        try:
            return process_metadata_item(meta, args, config, output_dir, checkpoint, default_publish_levels)
        except Exception as exc:
            if attempt >= attempts:
                print(f"[TASK_FAILED] {business_label} | attempts={attempts} | {type(exc).__name__}: {exc}")
                raise
            wait_seconds = min(5 * attempt, 20)
            print(
                f"[TASK_RETRY] {business_label} | attempt={attempt}/{attempts} | "
                f"{type(exc).__name__}: {exc} | {wait_seconds}s后使用同一run_id恢复",
                flush=True,
            )
            time.sleep(wait_seconds)
    return []


def run_from_metadata(args: argparse.Namespace, config: Dict[str, str], output_dir: Path) -> List[PublishedAlert]:
    resume_enabled = args.execute and not args.no_resume
    state_path = checkpoint_path(args)
    checkpoint = CheckpointStore(state_path, load_checkpoint(state_path, resume_enabled, args.code_fingerprint), resume_enabled)
    if resume_enabled:
        print(f"[CHECKPOINT] {state_path}")

    print_step("读取元数据配置")
    metadata_sql = apply_metadata_period_filter(args.metadata_sql, args.monitor_period)
    print_sql_block("元数据SQL", metadata_sql)
    metadata_items = load_active_metadata(config, metadata_sql)
    pending_items = [
        (index, meta)
        for index, meta in enumerate(metadata_items)
        if (not args.monitor_period or monitor_period_matches(meta.monitor_period, args.monitor_period))
        and bool(meta.data_sql)
    ]
    if not pending_items:
        print("[SUMMARY] 没有需要处理的元数据任务")
        return []

    workers = max(1, min(args.metadata_workers, len(pending_items)))
    default_publish_levels = set(split_cols(args.severity_levels))
    print(f"[PARALLEL] 元数据任务数={len(pending_items)} | workers={workers} | retries={max(0, args.metadata_retries)}")
    results: Dict[int, List[PublishedAlert]] = {}
    failures: List[str] = []

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="metadata") as executor:
        futures = {
            executor.submit(
                process_metadata_with_retry,
                meta,
                args,
                config,
                output_dir,
                checkpoint,
                default_publish_levels,
            ): (index, meta)
            for index, meta in pending_items
        }
        for future in as_completed(futures):
            index, meta = futures[future]
            business_label = meta.business_name or meta.id or "未命名业务"
            try:
                results[index] = future.result()
                print(f"[TASK_DONE] {business_label} | 告警数={len(results[index])}")
            except Exception as exc:
                failures.append(f"{business_label}: {type(exc).__name__}: {exc}")

    all_published = [alert for index in sorted(results) for alert in results[index]]
    print(
        f"[SUMMARY] 元数据总数={len(pending_items)} | 成功={len(results)} | "
        f"失败={len(failures)} | 发布告警={len(all_published)}"
    )
    if failures:
        raise RuntimeError("部分元数据任务在重试后仍失败：" + "；".join(failures))
    return all_published


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    args.code_fingerprint = build_code_fingerprint()
    setup_file_logging(args.log_dir, args.log_file)
    print(
        f"[CODE_VERSION] pipeline_version={PIPELINE_VERSION} | "
        f"code_fingerprint={args.code_fingerprint} | publish_mode={args.publish_mode}"
    )
    output_dir = resolve_output_dir(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"[OUTPUT_DIR] {output_dir}", flush=True)
    config = load_config(args.config)
    if args.execute and not args.from_metadata:
        ensure_run_id(args)

    if args.from_metadata:
        published = run_from_metadata(args, config, output_dir)
    else:
        rows = det.read_table(args.input)
        if not rows:
            print("No data")
            return 1
        time_col = infer_time_col(rows, args.time_col)
        metric_cols = infer_metric_cols(
            rows,
            time_col,
            requested=args.metric_cols,
            exclude_cols=split_cols(args.dimension_cols),
        )
        if not metric_cols:
            raise ValueError("请通过 --metric-cols 指定指标列，或使用 --from-metadata 自动识别")
        dimension_cols = infer_dimension_cols(rows, time_col, metric_cols, args.dimension_cols)
        business_name = args.business_name.strip() or args.task_name
        task_name = build_task_title(business_name, args.task_name, args.publish_mode)
        published = publish_alerts_for_rows(
            rows,
            time_col=time_col,
            dimension_cols=dimension_cols,
            metric_cols=metric_cols,
            granularity=args.granularity,
            period=args.period,
            low_base_downgrade=args.low_base_downgrade == "on",
            publish_levels=set(split_cols(args.severity_levels)),
            business_name=business_name,
            task_name=task_name,
            owner=args.owner,
            alert_table=args.alert_table,
            output_dir=output_dir,
            config=config,
            execute=args.execute,
            detector_config=detector_config_from_text(args.detectors),
            run_id=args.run_id,
            publish_mode=args.publish_mode,
            pipeline_version=PIPELINE_VERSION,
            code_fingerprint=args.code_fingerprint,
            force_republish=args.force_republish,
            idempotency_lookback_days=args.idempotency_lookback_days,
        )

    print(json.dumps([p.__dict__ for p in published], ensure_ascii=False, indent=2))
    if not args.execute:
        print("dry-run：已生成本地图片和 SQL 预览；添加 --execute 后会先上传 OSS 再写入 ODPS。")
    print(f"[RUN_DONE] run_id={args.run_id} | execute={args.execute} | published={len(published)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

