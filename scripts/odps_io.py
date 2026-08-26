#!/usr/bin/env python3
"""ODPS I/O helpers for anomaly alert publishing."""

from __future__ import annotations

import json
import os
import threading
import time
import warnings
from pathlib import Path
from typing import Dict, List, Tuple

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 .* doesn't match a supported version!",
    category=Warning,
    module=r"requests(\.|$)",
)



def workspace_root() -> Path:
    return Path(__file__).resolve().parents[4]


def resolve_config_path(path: str) -> Path:
    config_path = Path(path)
    candidates = []
    if config_path.is_absolute():
        candidates.append(config_path)
    else:
        root = workspace_root()
        candidates.extend(
            [
                Path.cwd() / config_path,
                Path(__file__).resolve().parent / config_path,
                Path(__file__).resolve().parents[1] / config_path,
                root / config_path,
                root / "发送消息" / config_path.name,
            ]
        )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else config_path


def load_config(path: str) -> Dict[str, str]:
    if not (path or "").strip():
        print("[CONFIG_SOURCE] environment variables")
        return {}
    config_path = resolve_config_path(path)
    if not config_path.is_file():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    print(f"[CONFIG_FILE] {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {str(k): "" if v is None else str(v) for k, v in data.items()}


def config_value(config: Dict[str, str], config_key: str, env_key: str, default: str = "") -> str:
    return os.getenv(env_key, "").strip() or config.get(config_key, "").strip() or default


def require_config(config: Dict[str, str], mapping: Dict[str, Tuple[str, str]]) -> Dict[str, str]:
    values = {name: config_value(config, config_key, env_key) for name, (config_key, env_key) in mapping.items()}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"缺少配置: {', '.join(missing)}。请填写配置文件或设置环境变量。")
    return values


def create_odps_client(config: Dict[str, str]):
    from odps import ODPS

    values = require_config(
        config,
        {
            "ODPS_ACCESS_ID": ("odps_access_id", "ODPS_ACCESS_ID"),
            "ODPS_ACCESS_KEY": ("odps_access_key", "ODPS_ACCESS_KEY"),
            "ODPS_PROJECT": ("odps_project", "ODPS_PROJECT"),
            "ODPS_ENDPOINT": ("odps_endpoint", "ODPS_ENDPOINT"),
        },
    )
    return ODPS(
        values["ODPS_ACCESS_ID"],
        values["ODPS_ACCESS_KEY"],
        values["ODPS_PROJECT"],
        endpoint=values["ODPS_ENDPOINT"],
    )


def print_logview(instance, label: str = "ODPS") -> None:
    instance_id = getattr(instance, "id", "") or getattr(instance, "instance_id", "")
    print(f"[ODPS_LOGVIEW] label: {label}")
    if instance_id:
        print(f"[ODPS_LOGVIEW] instance_id: {instance_id}")
    logview = ""
    try:
        if hasattr(instance, "get_logview_address"):
            logview = instance.get_logview_address()
    except Exception as exc:
        print(f"[ODPS_LOGVIEW] get_logview_failed: {exc}")
    if logview:
        print(f"[ODPS_LOGVIEW] url: {logview}")



class OperationHeartbeat:
    def __init__(self, label: str, interval: int = 20) -> None:
        self.label = label
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        started = time.monotonic()
        while not self._stop.wait(self.interval):
            elapsed = int(time.monotonic() - started)
            print(f"[HEARTBEAT] {self.label} 仍在执行，已等待 {elapsed}s", flush=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._stop.set()
        self._thread.join(timeout=1)


def query_odps_rows(sql: str, config: Dict[str, str], label: str = "ODPS查询") -> List[Dict[str, str]]:
    odps = create_odps_client(config)
    print(f"[ODPS_SUBMIT] {label}", flush=True)
    with OperationHeartbeat(label):
        instance = odps.execute_sql(sql, hints={"odps.sql.submit.mode": "script"})
        print_logview(instance, label)
        with instance.open_reader(tunnel=True, limit=False) as reader:
            schema = getattr(reader, "schema", None)
            if schema is None:
                return []
            columns = [col.name for col in schema.columns]
            rows: List[Dict[str, str]] = []
            for record in reader:
                row: Dict[str, str] = {}
                for col in columns:
                    try:
                        value = record[col]
                    except Exception:
                        value = getattr(record, col, "")
                    row[col] = "" if value is None else str(value)
                rows.append(row)
            return rows


def execute_odps_sql(sql: str, config: Dict[str, str], label: str = "ODPS写入") -> None:
    odps = create_odps_client(config)
    print(f"[ODPS_SUBMIT] {label}", flush=True)
    instance = odps.run_sql(sql, hints={"odps.sql.submit.mode": "script"})
    print_logview(instance, label)
    with OperationHeartbeat(label):
        instance.wait_for_success()


