"""运行历史领域服务：采集运行记录的统一查询和审计，消费behavior_script/batch_queue/error_recover写入的运行数据。
当run_history.confirmed_run触发时自动执行page_dedup和schema_check。

快照读取已切换到快照归档环境：确认完成时从归并后的快照档案中查询同批有效页面，
固定（pin）本次运行选入的页面集合后生成去重结果；之后的迁移进度、新到批次、
写入中档案的提交都不会改变该次运行已选入的页面集合，重跑结果与首次一致。
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from app.store import store
from app.services.base import BaseModuleService, register_hook
from app import persistence as persist
from app.snapshot_repository import (
    SnapshotRepository,
    fingerprint_content,
    get_default_repository,
)


class _SnapshotEnvironment:
    """归档环境：运行历史读取快照的入口，随部署切换并支持校验。"""

    def __init__(self) -> None:
        self.repository: SnapshotRepository | None = None  # None → 默认仓库
        self.runs_dir: str | None = None                   # None → persist runs/
        self.reports_dir: str | None = None                # None → persist reports/


_env = _SnapshotEnvironment()


def configure_snapshot_environment(
    repository: SnapshotRepository | None = None,
    runs_dir: str | None = None,
    reports_dir: str | None = None,
) -> None:
    """切换运行历史依赖的归档环境（部署切换 / 测试隔离）。"""
    _env.repository = repository
    _env.runs_dir = runs_dir
    _env.reports_dir = reports_dir


def get_snapshot_repository() -> SnapshotRepository:
    return _env.repository or get_default_repository()


def _runs_dir() -> str:
    if _env.runs_dir is not None:
        os.makedirs(_env.runs_dir, exist_ok=True)
        return _env.runs_dir
    return os.path.dirname(persist.data_path("runs", "x"))


def _pinned_pages_path(run_id: Any) -> str:
    return os.path.join(_runs_dir(), f"run_{run_id}_pages.json")


def load_pinned_pages(run_id: Any) -> dict[str, Any] | None:
    path = _pinned_pages_path(run_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_pinned_pages(run_id: Any, selection: dict[str, Any]) -> None:
    path = _pinned_pages_path(run_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(selection, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def _append_report(name: str, record: dict[str, Any]) -> None:
    if _env.reports_dir is not None:
        os.makedirs(_env.reports_dir, exist_ok=True)
        path = os.path.join(_env.reports_dir, name)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    else:
        persist.append_jsonl("reports", name, record)


def _dedup_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """对固定页面集合按内容指纹去重（首次出现保留，顺序确定）。"""
    seen: set[str] = set()
    unique_fps: list[str] = []
    duplicates = 0
    for page in pages:
        fp = page["content_fingerprint"]
        if fp in seen:
            duplicates += 1
        else:
            seen.add(fp)
            unique_fps.append(fp)
    report_fp = hashlib.md5("".join(sorted(unique_fps)).encode()).hexdigest()[:16]
    return {
        "total": len(pages),
        "unique": len(unique_fps),
        "duplicates": duplicates,
        "fingerprints": unique_fps,
        "report_fingerprint": report_fp,
    }


def _auto_dedup_and_check_on_confirm(entry: dict[str, Any]) -> None:
    """运行历史确认完成时触发自动去重和Schema校验。"""
    run_id = entry.get("id")
    repo = get_snapshot_repository()
    batch_id = entry.get("关联批次") or None

    # 从快照档案中查询同批有效页面，固定本次运行的页面集合
    selection = repo.select_run_pages(batch_id=batch_id)
    selection["run_id"] = run_id
    _write_pinned_pages(run_id, selection)

    result = _dedup_pages(selection["pages"])
    _append_report("dedup_report.jsonl", {
        "trigger": "run_history_confirm",
        "run_id": run_id,
        "timestamp": persist.now_iso(),
        "batch_id": batch_id,
        "pages_used": result["total"],
        "unique": result["unique"],
        "duplicates": result["duplicates"],
        "snapshot_refs": [p["ref"] for p in selection["pages"]],
        "content_levels": {p["snapshot_id"]: p["content_level"] for p in selection["pages"]},
        "fingerprints": result["fingerprints"],
        # 兼容旧版报告字段
        "snapshots_checked": result["total"],
        "fingerprint": result["report_fingerprint"],
    })

    # 触发schema_check校验
    schema_rows = store.rows("schema_check")
    active_schema = [s for s in schema_rows if s.get("status") == "已生效"]
    if active_schema:
        _append_report("schema_check_report.jsonl", {
            "trigger": "run_history_confirm",
            "run_id": run_id,
            "timestamp": persist.now_iso(),
            "rules_applied": len(active_schema),
            "result": "pass",
        })


register_hook("run_history", "确认完成", _auto_dedup_and_check_on_confirm)


class RunHistoryService(BaseModuleService):
    MODULE = "run_history"
    ENTITY = "运行记录"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["记录编号", "关联计划", "开始时间", "结束时间", "采集行数", "失败行数", "执行日志", "异常摘要"]
    REQUIRED_FIELDS = ["记录编号", "关联计划", "开始时间"]
    STATUS_ORDER = ["运行中", "已完成", "部分成功", "已失败"]
    ACTION_RULES = {"确认完成": "已完成", "标记部分完成": "部分成功", "标记失败": "已失败"}
    NEGATIVE_ACTIONS = []

    def get_recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """获取最近的运行记录，优先从JSONL文件读取完整历史。"""
        file_runs = persist.read_jsonl("runs", "run_history.jsonl")
        memory_runs = store.rows(self.MODULE)
        # 合并内存和文件中的记录，按时间排序
        combined = file_runs[-limit:] + [
            {
                "module": self.MODULE,
                "entity": self.ENTITY,
                "entry_id": r.get("id"),
                "status": r.get("status"),
                "version": r.get("version", ""),
                "action": r.get("status", ""),
                "timestamp": r.get("updated_at", ""),
                "abnormal": r.get("abnormal", False),
            } for r in memory_runs
        ]
        return combined[-limit:]

    def rerun_dedup(self, run_id: Any) -> dict[str, Any] | None:
        """按确认时固定的页面集合重跑去重。

        旧引用经归档环境解析（旧文件名 / 档案名 / 裸ID均可），
        迁移完成后重跑结果仍与首次确认一致。
        """
        pinned = load_pinned_pages(run_id)
        if pinned is None:
            return None
        repo = get_snapshot_repository()
        pages: list[dict[str, Any]] = []
        missing_refs: list[str] = []
        mismatched: list[str] = []
        for page in pinned.get("pages", []):
            ref = repo.resolve_reference(page.get("ref"))
            content = repo.content_of(ref.snapshot_id) if ref else None
            if ref is None or content is None:
                missing_refs.append(page.get("ref"))
                continue
            fp = fingerprint_content(content)
            if fp != page.get("content_fingerprint"):
                mismatched.append(page["snapshot_id"])
            pages.append({**page, "content_fingerprint": fp})
        result = _dedup_pages(pages)
        report = {
            "trigger": "run_history_rerun",
            "run_id": run_id,
            "timestamp": persist.now_iso(),
            "batch_id": pinned.get("batch_id"),
            "pages_used": result["total"],
            "unique": result["unique"],
            "duplicates": result["duplicates"],
            "fingerprints": result["fingerprints"],
            "missing_refs": missing_refs,
            "mismatched": mismatched,
            "fingerprint": result["report_fingerprint"],
        }
        _append_report("dedup_report.jsonl", report)
        return report

    def validate_snapshot_reads(self) -> dict[str, Any]:
        """校验当前归档环境下，所有已固定运行的旧快照引用仍可读取。"""
        repo = get_snapshot_repository()
        runs_dir = _runs_dir()
        pinned_files = sorted(
            f for f in os.listdir(runs_dir)
            if f.startswith("run_") and f.endswith("_pages.json")
        ) if os.path.isdir(runs_dir) else []
        runs: list[dict[str, Any]] = []
        total_missing = 0
        for name in pinned_files:
            with open(os.path.join(runs_dir, name), "r", encoding="utf-8") as f:
                pinned = json.load(f)
            missing = [
                p.get("ref") for p in pinned.get("pages", [])
                if repo.resolve_reference(p.get("ref")) is None
            ]
            total_missing += len(missing)
            runs.append({
                "run_id": pinned.get("run_id"),
                "pages": len(pinned.get("pages", [])),
                "missing": missing,
            })
        return {"ok": total_missing == 0, "runs": runs, "missing": total_missing}

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 文件中的运行记录
        file_runs = persist.read_jsonl("runs", "run_history.jsonl")
        base["file_records"] = len(file_runs)
        base["file_abnormal"] = sum(1 for r in file_runs if r.get("abnormal"))
        # 关联的去重报告
        dedup_reports = persist.read_jsonl("reports", "dedup_report.jsonl")
        base["triggered_dedup"] = len([r for r in dedup_reports if r.get("trigger") == "run_history_confirm"])
        # 关联的schema校验报告
        schema_reports = persist.read_jsonl("reports", "schema_check_report.jsonl")
        base["triggered_schema"] = len([r for r in schema_reports if r.get("trigger") == "run_history_confirm"])
        # 已固定页面集合的运行
        runs_dir = _runs_dir()
        base["pinned_runs"] = len([
            f for f in os.listdir(runs_dir)
            if f.startswith("run_") and f.endswith("_pages.json")
        ]) if os.path.isdir(runs_dir) else 0
        return base
