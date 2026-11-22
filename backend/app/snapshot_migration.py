"""快照迁移任务：旧版单JSON快照 → 带格式版本的压缩快照档案。

特性：
- 幂等：已完成的快照跳过，重复执行不产生重复档案；
- 可重试：失败记录进 manifest/journal，修复后 retry_failed 只重跑失败项；
- 并发安全：档案原子提交，采集端持续写入的新旧文件互不干扰；
- 不删除旧文件：迁移后同一快照存在两份物理档案，旧报告引用保持可解析，
  旧文件的回收属于独立的保留策略，不在迁移任务内。
"""
from __future__ import annotations

import json
import os
from typing import Any

from app import persistence as persist
from app import snapshot_archive as sa
from app.snapshot_repository import SnapshotRepository


class SnapshotMigrator:
    JOURNAL_FILE = "migration_journal.jsonl"
    MANIFEST_FILE = "migration_manifest.json"

    def __init__(self, snapshots_dir: str | None = None,
                 repository: SnapshotRepository | None = None) -> None:
        self._repo = repository or SnapshotRepository(snapshots_dir)
        self._dir = self._repo.snapshots_dir

    # ---------- 状态存取 ----------

    def _manifest_path(self) -> str:
        return os.path.join(self._dir, self.MANIFEST_FILE)

    def _load_manifest(self) -> dict[str, Any]:
        path = self._manifest_path()
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        os.makedirs(self._dir, exist_ok=True)
        tmp = self._manifest_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, self._manifest_path())

    def _journal(self, record: dict[str, Any]) -> None:
        os.makedirs(self._dir, exist_ok=True)
        path = os.path.join(self._dir, self.JOURNAL_FILE)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _record(self, manifest: dict[str, Any], snapshot_id: str, state: str,
                **extra: Any) -> None:
        entry = dict(manifest.get(snapshot_id) or {})
        entry.update({
            "state": state,
            "attempts": int(entry.get("attempts", 0)) + 1,
            "updated_at": persist.now_iso(),
            **extra,
        })
        manifest[snapshot_id] = entry
        self._journal({"snapshot_id": snapshot_id, "state": state,
                       "timestamp": entry["updated_at"], **extra})

    # ---------- 计划与状态 ----------

    def plan(self) -> list[str]:
        """待迁移的旧版快照ID（已完成的不重复处理，失败的可再试）。"""
        manifest = self._load_manifest()
        pending = []
        for name in sa.list_legacy(self._dir):
            sid = sa.snapshot_id_from_name(name)
            if manifest.get(sid, {}).get("state") != "done":
                pending.append(sid)
        return sorted(pending)

    def status(self) -> dict[str, Any]:
        manifest = self._load_manifest()
        done = sorted(sid for sid, e in manifest.items() if e.get("state") == "done")
        failed = sorted(sid for sid, e in manifest.items() if e.get("state") == "failed")
        return {
            "done": len(done),
            "failed": len(failed),
            "pending": len(self.plan()),
            "done_ids": done,
            "failed_ids": failed,
            "archives_committed": len(sa.list_archives(self._dir)),
            "in_progress": len(sa.inprogress_archives(self._dir)),
        }

    # ---------- 迁移执行 ----------

    def migrate_one(self, snapshot_id: str) -> bool:
        """迁移单份快照；失败记入manifest可稍后重试，不会中断其他快照。"""
        manifest = self._load_manifest()
        if manifest.get(snapshot_id, {}).get("state") == "done":
            return True  # 幂等：已完成直接跳过
        legacy_name = f"{snapshot_id}{sa.LEGACY_SUFFIX}"
        try:
            payload = self._repo._read_legacy_payload(legacy_name)
            archive_file = sa.write_archive(
                self._dir, snapshot_id, payload,
                origin="migration",
                archived_at=persist.now_iso(),
            )
        except Exception as exc:
            self._record(manifest, snapshot_id, "failed",
                         last_error=str(exc), archive_file=None)
            self._save_manifest(manifest)
            return False
        self._record(
            manifest, snapshot_id, "done",
            last_error=None,
            archive_file=archive_file,
            content_level=sa.content_level_of(payload),
        )
        self._save_manifest(manifest)
        return True

    def migrate_all(self, limit: int | None = None) -> dict[str, Any]:
        """按当前计划迁移；limit 用于分批（迁移中途运行确认的场景）。"""
        migrated: list[str] = []
        failed: dict[str, str] = {}
        for sid in self.plan()[:limit]:
            if self.migrate_one(sid):
                migrated.append(sid)
            else:
                error = self._load_manifest().get(sid, {}).get("last_error", "")
                failed[sid] = error
        return {
            "migrated": migrated,
            "failed": failed,
            "remaining": len(self.plan()),
        }

    def retry_failed(self) -> dict[str, Any]:
        """只重跑此前失败的快照。"""
        manifest = self._load_manifest()
        failed_ids = sorted(
            sid for sid, e in manifest.items() if e.get("state") == "failed"
        )
        migrated: list[str] = []
        still_failed: dict[str, str] = {}
        for sid in failed_ids:
            if self.migrate_one(sid):
                migrated.append(sid)
            else:
                still_failed[sid] = self._load_manifest().get(sid, {}).get("last_error", "")
        return {"retried": failed_ids, "migrated": migrated, "failed": still_failed}
