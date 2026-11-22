"""快照归并读取视图：合并旧版单JSON与新版压缩档案，去重后统一读取。

职责：
- 同一快照存在两份物理档案（旧JSON + 压缩档案）时归并为一份逻辑快照，避免重复计数；
- 既有数据只有旧版本预览时仍按 preview 层级读取；
- 旧报告里的物理引用（旧文件名 / 档案名 / 裸ID）在迁移后仍可解析；
- 运行确认时固定（pin）同批有效页面集合，之后的迁移进度不改变该集合；
- 迁移写入与采集写入按来源分别核对。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

from app import persistence as persist
from app import snapshot_archive as sa


def fingerprint_content(content: str) -> str:
    """页面内容指纹，用于同批页面去重。"""
    return hashlib.md5(content.encode()).hexdigest()[:16]


@dataclass
class SnapshotRef:
    """一份逻辑快照（可能对应两份物理档案）。"""
    snapshot_id: str
    source: str                       # legacy | archive | dual
    content_level: str                # full | preview
    batch_id: str | None = None
    legacy_file: str | None = None
    archive_file: str | None = None
    origin: str | None = None         # migration | collector | None(既存旧数据)
    valid: bool = True
    error: str | None = None

    @property
    def physical_files(self) -> list[str]:
        return [f for f in (self.legacy_file, self.archive_file) if f]

    @property
    def preferred_ref(self) -> str | None:
        """运行确认列出的物理引用：已迁移用档案名，未迁移用旧文件名。"""
        return self.archive_file or self.legacy_file


class SnapshotRepository:
    def __init__(self, snapshots_dir: str | None = None) -> None:
        self._dir = snapshots_dir or persist.data_path("snapshots", "")

    @property
    def snapshots_dir(self) -> str:
        return self._dir

    # ---------- 物理读取 ----------

    def _read_legacy_payload(self, name: str) -> dict[str, Any]:
        path = os.path.join(self._dir, name)
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            raise ValueError(f"旧版快照不是JSON对象: {name}")
        return payload

    # ---------- 归并视图 ----------

    def list_snapshots(self) -> list[SnapshotRef]:
        """归并后的逻辑快照列表（按 snapshot_id 去重，写入中的档案除外）。"""
        merged: dict[str, SnapshotRef] = {}

        for name in sa.list_legacy(self._dir):
            sid = sa.snapshot_id_from_name(name)
            ref = merged.setdefault(sid, SnapshotRef(
                snapshot_id=sid, source="legacy", content_level="preview",
            ))
            ref.legacy_file = name
            try:
                payload = self._read_legacy_payload(name)
                ref.content_level = sa.content_level_of(payload)
                ref.batch_id = payload.get("batch_id", ref.batch_id)
                ref.origin = payload.get("origin", ref.origin)
            except Exception as exc:  # 损坏的旧文件：标记无效但不影响其他快照
                ref.valid = False
                ref.error = f"legacy:{exc}"

        for name in sa.list_archives(self._dir):
            try:
                envelope = sa.read_archive(self._dir, name)
                sid = envelope["snapshot_id"]
                ref = merged.setdefault(sid, SnapshotRef(
                    snapshot_id=sid, source="archive", content_level="preview",
                ))
                ref.archive_file = name
                ref.content_level = envelope.get("content_level", ref.content_level)
                payload = envelope.get("payload") or {}
                ref.batch_id = payload.get("batch_id", ref.batch_id)
                # 归档信封上的 origin 表示该档案的写入来源（migration/collector）
                ref.origin = envelope.get("origin", ref.origin)
                ref.valid = True
                ref.error = None
            except Exception as exc:
                sid = sa.snapshot_id_from_name(name)
                ref = merged.setdefault(sid, SnapshotRef(
                    snapshot_id=sid, source="archive", content_level="preview",
                ))
                ref.archive_file = name
                if ref.legacy_file is None:
                    ref.valid = False
                    ref.error = f"archive:{exc}"

        for ref in merged.values():
            if ref.legacy_file and ref.archive_file:
                ref.source = "dual"
            elif ref.archive_file:
                ref.source = "archive"
            else:
                ref.source = "legacy"
        return [merged[sid] for sid in sorted(merged)]

    def valid_pages(self, batch_id: str | None = None) -> list[SnapshotRef]:
        """同批有效页面：已提交、可解析，按 snapshot_id 排序保证确定性。"""
        pages = [r for r in self.list_snapshots() if r.valid and r.preferred_ref]
        if batch_id is not None:
            pages = [r for r in pages if r.batch_id == batch_id]
        return pages

    # ---------- 内容读取 ----------

    def _payload_of(self, ref: SnapshotRef) -> dict[str, Any] | None:
        if ref.archive_file:
            try:
                return sa.read_archive(self._dir, ref.archive_file).get("payload")
            except Exception:
                pass  # 归档不可读时回退旧文件
        if ref.legacy_file:
            try:
                return self._read_legacy_payload(ref.legacy_file)
            except Exception:
                return None
        return None

    def read_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        """按快照ID读取载荷，与物理位置无关（迁移前后结果一致）。"""
        ref = self.resolve_reference(snapshot_id)
        if ref is None or not ref.valid:
            return None
        return self._payload_of(ref)

    def content_of(self, snapshot_id: str) -> str | None:
        """用于去重的页面内容：完整DOM优先，仅有预览时按预览层级返回预览。"""
        payload = self.read_snapshot(snapshot_id)
        if payload is None:
            return None
        if payload.get("raw_dom"):
            return str(payload["raw_dom"])
        preview = payload.get("dom_preview")
        return None if preview is None else str(preview)

    def level_of(self, snapshot_id: str) -> str | None:
        ref = self.resolve_reference(snapshot_id)
        return ref.content_level if ref else None

    # ---------- 引用解析 ----------

    def resolve_reference(self, ref: str | None) -> SnapshotRef | None:
        """把旧报告里的物理引用解析为逻辑快照。

        接受：旧文件名 page_x.json、档案名 page_x.snapgz、裸快照ID page_x。
        旧文件即使已随迁移产生压缩档案，仍按同一 snapshot_id 归并解析。
        """
        if not ref:
            return None
        sid = sa.snapshot_id_from_name(ref)
        for item in self.list_snapshots():
            if item.snapshot_id == sid:
                return item
        return None

    # ---------- 运行固定集合 ----------

    def select_run_pages(self, batch_id: str | None = None) -> dict[str, Any]:
        """为一次运行固定同批有效页面集合。

        集合在确认时刻定型并持久化；之后的迁移进度、新到批次、
        写入中档案的提交都不会改变这次运行已选入的页面。
        """
        pages = []
        for ref in self.valid_pages(batch_id=batch_id):
            content = self.content_of(ref.snapshot_id)
            pages.append({
                "snapshot_id": ref.snapshot_id,
                "ref": ref.preferred_ref,
                "source": ref.source,
                "content_level": ref.content_level,
                "batch_id": ref.batch_id,
                "content_fingerprint": fingerprint_content(content or ""),
            })
        return {
            "batch_id": batch_id,
            "selected_at": persist.now_iso(),
            "pages": pages,
        }

    # ---------- 写入来源核对 ----------

    def verify_writes(self) -> dict[str, Any]:
        """按写入来源分别核对：迁移产出的档案 vs 采集产出的文件。

        迁移任务与新采集并发写入时，两边可以独立对账；
        写入中的临时档案与损坏文件单独列出，不混入任何来源。
        """
        migration: list[str] = []
        collector: list[str] = []
        legacy_existing: list[str] = []
        invalid: list[dict[str, str]] = []

        for name in sa.list_archives(self._dir):
            try:
                envelope = sa.read_archive(self._dir, name)
                sid = envelope["snapshot_id"]
                if envelope.get("origin") == "migration":
                    migration.append(sid)
                else:
                    collector.append(sid)
            except Exception as exc:
                invalid.append({"file": name, "error": str(exc)})

        for name in sa.list_legacy(self._dir):
            sid = sa.snapshot_id_from_name(name)
            try:
                payload = self._read_legacy_payload(name)
                if payload.get("origin") == "collector":
                    collector.append(sid)
                else:
                    legacy_existing.append(sid)
            except Exception as exc:
                invalid.append({"file": name, "error": str(exc)})

        return {
            "migration": {"archives": len(migration), "snapshot_ids": sorted(set(migration))},
            "collector": {"files": len(collector), "snapshot_ids": sorted(set(collector))},
            "legacy_existing": {"files": len(legacy_existing),
                                "snapshot_ids": sorted(set(legacy_existing))},
            "in_progress": sa.inprogress_archives(self._dir),
            "invalid": invalid,
        }


_default_repository: SnapshotRepository | None = None


def get_default_repository() -> SnapshotRepository:
    global _default_repository
    if _default_repository is None:
        _default_repository = SnapshotRepository()
    return _default_repository


def set_default_repository(repo: SnapshotRepository | None) -> None:
    """切换默认归档环境（部署切换 / 测试隔离）。"""
    global _default_repository
    _default_repository = repo
