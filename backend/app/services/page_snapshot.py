"""页面快照领域服务：页面DOM快照的采集与存储，消费login_mgr凭证和canvas_config配置。
新采集的快照仍写旧版单JSON（迁移窗口内新批数据持续进入），由迁移任务压缩归档。"""
from __future__ import annotations

import json
import os
from typing import Any

from app.store import store
from app.services.base import BaseModuleService, register_hook
from app import persistence as persist
from app.snapshot_repository import get_default_repository


class PageSnapshotService(BaseModuleService):
    MODULE = "page_snapshot"
    ENTITY = "页面快照"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["快照编号", "采集来源", "页面版本", "DOM摘要", "快照时间", "快照类型", "数据量", "回收时间"]
    REQUIRED_FIELDS = ["快照编号", "采集来源", "页面版本"]
    STATUS_ORDER = ["草稿", "已存档", "已清洗", "已回收"]
    ACTION_RULES = {"标记存档": "已存档", "确认清洗": "已清洗", "启动回收": "已回收"}
    NEGATIVE_ACTIONS = []

    def capture_snapshot(self, page_url: str, dom_hash: str, raw_dom: str,
                         batch_id: str = "default",
                         snapshots_dir: str | None = None) -> dict[str, Any]:
        """采集页面快照并写入snapshots/目录（旧版单JSON，等待迁移压缩归档）。

        origin=collector 标记采集来源，与迁移任务的写入分开核对。
        """
        snapshot = {
            "snapshot_id": f"page_{dom_hash}",
            "page_url": page_url,
            "dom_hash": dom_hash,
            "batch_id": batch_id,
            "origin": "collector",
            "raw_dom": raw_dom,
            "raw_dom_length": len(raw_dom),
            "timestamp": persist.now_iso(),
            "dom_preview": raw_dom[:500],
        }
        if snapshots_dir is None:
            persist.write_json("snapshots", f"page_{dom_hash}.json", snapshot)
        else:
            os.makedirs(snapshots_dir, exist_ok=True)
            path = os.path.join(snapshots_dir, f"page_{dom_hash}.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, path)
        return snapshot

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        repo = get_default_repository()
        snaps = repo.list_snapshots()
        base["snapshot_files"] = len(snaps)
        base["archived"] = len([s for s in snaps if s.archive_file])
        base["dual_physical"] = len([s for s in snaps if s.source == "dual"])
        base["preview_only"] = len([s for s in snaps if s.content_level == "preview"])
        # 关联的登录凭证
        credentials = store.rows("login_mgr")
        rows = store.rows(self.MODULE)
        sites = {r.get("采集来源") for r in rows}
        base["sites_with_credentials"] = sum(1 for c in credentials if c.get("目标站点") in sites)
        return base