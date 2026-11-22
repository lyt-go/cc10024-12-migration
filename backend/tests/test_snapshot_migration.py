"""快照归档迁移集成测试：旧版单JSON快照 → 带格式版本的压缩快照档案。

场景覆盖（对应迁移验证要求）：
- 完整记录压缩为带格式版本的档案（格式版本 + gzip 压缩内容）
- 仅有预览的旧快照在迁移前后都按预览层级读取
- 写入中的档案对运行确认不可见，提交后才计入
- 同一快照两份物理档案归并读取，不重复计数
- 迁移中途执行运行确认：核对页数、旧来源引用、重跑结果
- 迁移失败可重试；迁移与采集的并发写入分别核对
- 部署前旧报告的快照引用在迁移后仍可关联
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import snapshot_archive as sa
from app.snapshot_repository import SnapshotRepository
from app.snapshot_migration import SnapshotMigrator

BATCH = "BATCH-T1"


# ---------- 测试数据 ----------

def _make_env():
    root = tempfile.mkdtemp(prefix="snap_mig_")
    snaps = os.path.join(root, "snapshots")
    runs = os.path.join(root, "runs")
    reports = os.path.join(root, "reports")
    for d in (snaps, runs, reports):
        os.makedirs(d)
    return root, snaps, runs, reports


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_legacy(snaps, hash_, url, batch, raw_dom=None, preview=None, origin=None):
    payload = {
        "snapshot_id": f"page_{hash_}",
        "page_url": url,
        "dom_hash": hash_,
        "batch_id": batch,
        "timestamp": "2026-11-20T10:00:00",
        "dom_preview": preview if preview is not None else (raw_dom or "")[:500],
    }
    if raw_dom is not None:
        payload["raw_dom"] = raw_dom
        payload["raw_dom_length"] = len(raw_dom)
    if origin:
        payload["origin"] = origin
    path = os.path.join(snaps, f"page_{hash_}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


def _seed_preview_only(snaps, count=3, batch=BATCH):
    """一组仅有预览的旧快照（无 raw_dom）。"""
    return {
        f"a{i}": _write_legacy(snaps, f"a{i}", f"https://t1.example.com/a{i}", batch,
                               preview=f"<html>preview-only-{i}</html>")
        for i in range(1, count + 1)
    }


def _seed_full_records(snaps, count=4, batch=BATCH):
    """一组完整可压缩的记录。"""
    return {
        f"b{i}": _write_legacy(snaps, f"b{i}", f"https://t1.example.com/b{i}", batch,
                               raw_dom=f"<html><body>page-{i}-{'x' * 1500}</body></html>")
        for i in range(1, count + 1)
    }


def _seed_inprogress_archive(snaps):
    """一份正在写入的档案（未提交的临时文件）。"""
    tmp = sa.begin_archive(snaps, "page_c1")
    with open(tmp, "wb") as f:
        f.write(b"\x1f\x8b partial-gzip-bytes-not-committed")
    return tmp


def _commit_inflight_archive(tmp, payload):
    """模拟写入方完成并提交档案。"""
    envelope = sa.make_envelope("page_c1", payload, origin="collector")
    sa.write_envelope_file(tmp, envelope)
    sa.commit_archive(tmp)


# ---------- 测试 ----------

def test_archive_format_version_and_compression():
    """完整记录迁移为带格式版本的压缩档案。"""
    root, snaps, _, _ = _make_env()
    try:
        seeded = _seed_full_records(snaps)
        repo = SnapshotRepository(snaps)
        result = SnapshotMigrator(repository=repo).migrate_all()
        assert len(result["migrated"]) == 4, result

        archives = sa.list_archives(snaps)
        assert len(archives) == 4
        for name in archives:
            path = os.path.join(snaps, name)
            with open(path, "rb") as f:
                assert f.read(2) == b"\x1f\x8b", f"{name} 不是gzip压缩内容"
            envelope = sa.read_archive(snaps, name)
            assert envelope["format"] == sa.ARCHIVE_FORMAT
            assert envelope["format_version"] == 2, "档案应带格式版本"
            assert envelope["origin"] == "migration"
            assert envelope["content_level"] == "full"
            legacy_path = os.path.join(snaps, sa.snapshot_id_from_name(name) + ".json")
            assert os.path.getsize(path) < os.path.getsize(legacy_path), "压缩档案应更小"
        payload = repo.read_snapshot("page_b1")
        assert payload["raw_dom"] == seeded["b1"]["raw_dom"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_preview_only_snapshots_keep_preview_level():
    """仅有预览的旧快照，迁移前后都按预览层级读取。"""
    root, snaps, _, _ = _make_env()
    try:
        seeded = _seed_preview_only(snaps)
        repo = SnapshotRepository(snaps)
        assert repo.level_of("page_a1") == "preview"
        assert repo.content_of("page_a1") == seeded["a1"]["dom_preview"]

        SnapshotMigrator(repository=repo).migrate_all()
        assert repo.level_of("page_a1") == "preview", "迁移后仍应按预览层级读取"
        assert repo.content_of("page_a1") == seeded["a1"]["dom_preview"]
        envelope = sa.read_archive(snaps, "page_a1.snapgz")
        assert envelope["content_level"] == "preview"
        assert "raw_dom" not in envelope["payload"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_inprogress_archive_invisible_until_committed():
    """正在写入的档案不计入读取，提交后才可见。"""
    root, snaps, _, _ = _make_env()
    try:
        _seed_preview_only(snaps, count=1)
        tmp = _seed_inprogress_archive(snaps)
        repo = SnapshotRepository(snaps)

        ids = [r.snapshot_id for r in repo.list_snapshots()]
        assert ids == ["page_a1"], ids
        assert repo.read_snapshot("page_c1") is None
        assert len(repo.verify_writes()["in_progress"]) == 1

        _commit_inflight_archive(tmp, {"page_url": "https://t1.example.com/c1",
                                       "batch_id": BATCH, "dom_preview": "<p>c1</p>"})
        ids = [r.snapshot_id for r in repo.list_snapshots()]
        assert "page_c1" in ids
        assert repo.verify_writes()["in_progress"] == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_dual_physical_archives_merged():
    """同一快照的两份物理档案归并为一份逻辑快照。"""
    root, snaps, _, _ = _make_env()
    try:
        _seed_full_records(snaps, count=2)
        repo = SnapshotRepository(snaps)
        SnapshotMigrator(repository=repo).migrate_all()

        refs = repo.list_snapshots()
        assert len(refs) == 2, "两份物理档案应归并为一份逻辑快照"
        for ref in refs:
            assert ref.source == "dual"
            assert len(ref.physical_files) == 2
        selection = repo.select_run_pages(batch_id=BATCH)
        assert len(selection["pages"]) == 2, "归并后不应重复计数"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_run_confirmation_mid_migration():
    """迁移中途执行运行确认：核对页数、旧来源引用与重跑结果。"""
    root, snaps, runs, reports = _make_env()
    from app.services import run_history as rh
    try:
        preview_seeded = _seed_preview_only(snaps)      # 3 份仅预览旧快照
        full_seeded = _seed_full_records(snaps)         # 4 份完整可压缩记录
        inflight_tmp = _seed_inprogress_archive(snaps)  # 1 份正在写入的档案

        repo = SnapshotRepository(snaps)
        migrator = SnapshotMigrator(repository=repo)
        assert migrator.status()["pending"] == 7

        # 迁移第一批（3份仅预览旧快照），其余待迁移
        first = migrator.migrate_all(limit=3)
        assert len(first["migrated"]) == 3
        assert migrator.status()["done"] == 3

        # 迁移窗口内新批数据持续进入（采集端并发写入）
        from app.services.page_snapshot import PageSnapshotService
        collector = PageSnapshotService()
        collector.capture_snapshot("https://t1.example.com/n1", "n1",
                                   "<html>new-page-1</html>",
                                   batch_id=BATCH, snapshots_dir=snaps)
        collector.capture_snapshot("https://t1.example.com/n1-dup", "n2",
                                   full_seeded["b1"]["raw_dom"],  # 与b1内容重复的采集页
                                   batch_id=BATCH, snapshots_dir=snaps)

        # 并发写入分别核对：迁移3份档案 vs 采集2个新文件，互不混淆
        writes = repo.verify_writes()
        assert writes["migration"]["snapshot_ids"] == ["page_a1", "page_a2", "page_a3"]
        assert writes["collector"]["snapshot_ids"] == ["page_n1", "page_n2"]
        assert not (set(writes["migration"]["snapshot_ids"])
                    & set(writes["collector"]["snapshot_ids"]))
        assert len(writes["in_progress"]) == 1

        # 迁移中途执行运行确认
        rh.configure_snapshot_environment(repository=repo, runs_dir=runs,
                                          reports_dir=reports)
        svc = rh.RunHistoryService()
        entry, missing = svc.create_entry({
            "记录编号": "RUN-MIG-0001", "关联计划": "PLAN-T1", "开始时间": "2026-11-22",
        })
        assert entry is not None, missing
        entry["关联批次"] = BATCH
        result, msg = svc.run_action(entry["id"], "确认完成")
        assert result is not None, msg
        run_id = entry["id"]

        # 核对确认使用的页数：7旧 + 2新 = 9；写入中的档案不计入
        confirm = [r for r in _read_jsonl(os.path.join(reports, "dedup_report.jsonl"))
                   if r.get("run_id") == run_id and r["trigger"] == "run_history_confirm"]
        assert len(confirm) == 1
        confirm = confirm[0]
        assert confirm["pages_used"] == 9, confirm
        assert confirm["unique"] == 8, "n2与b1内容重复应被去重"
        assert confirm["duplicates"] == 1
        assert len(confirm["snapshot_refs"]) == 9
        assert not any("page_c1" in ref for ref in confirm["snapshot_refs"])
        # 已迁移的列出档案名，未迁移的列出旧文件名（运行确认已列出旧文件）
        assert "page_a1.snapgz" in confirm["snapshot_refs"]
        assert "page_b1.json" in confirm["snapshot_refs"]

        pinned = rh.load_pinned_pages(run_id)
        assert pinned is not None and len(pinned["pages"]) == 9

        # 迁移继续推进直至完成（含新到的2份），写入中档案随后提交
        migrator.migrate_all()
        assert migrator.status()["done"] == 9
        _commit_inflight_archive(inflight_tmp,
                                 {"page_url": "https://t1.example.com/c1",
                                  "batch_id": BATCH, "dom_preview": "<p>c1</p>"})
        assert migrator.status()["in_progress"] == 0

        # 迁移进度不改变该次运行已选入的页面集合：重跑结果与首次一致
        rerun = svc.rerun_dedup(run_id)
        assert rerun is not None
        assert rerun["pages_used"] == 9
        assert rerun["unique"] == confirm["unique"] == 8
        assert rerun["duplicates"] == confirm["duplicates"] == 1
        assert rerun["fingerprints"] == confirm["fingerprints"]
        assert rerun["missing_refs"] == []
        assert rerun["mismatched"] == []

        # 旧来源引用仍可解析（部署后原有报告关联旧快照）
        for ref in confirm["snapshot_refs"]:
            resolved = repo.resolve_reference(ref)
            assert resolved is not None, f"旧引用不可解析: {ref}"
            assert repo.content_of(resolved.snapshot_id) is not None
        b1 = repo.resolve_reference("page_b1.json")
        assert b1 is not None and b1.source == "dual"
        assert repo.content_of("page_b1") == full_seeded["b1"]["raw_dom"]
        a1 = repo.resolve_reference("page_a1.json")
        assert repo.content_of(a1.snapshot_id) == preview_seeded["a1"]["dom_preview"]

        # 归档环境切换后，校验运行历史对旧快照的读取依赖
        check = svc.validate_snapshot_reads()
        assert check["ok"] and check["missing"] == 0, check
        rh.configure_snapshot_environment(repository=SnapshotRepository(snaps),
                                          runs_dir=runs, reports_dir=reports)
        svc2 = rh.RunHistoryService()
        assert svc2.validate_snapshot_reads()["ok"]
        rerun2 = svc2.rerun_dedup(run_id)
        assert rerun2["unique"] == 8 and rerun2["duplicates"] == 1

        # 迁移与采集的并发写入最终分别核对
        final_writes = repo.verify_writes()
        assert final_writes["migration"]["archives"] == 9
        assert final_writes["collector"]["snapshot_ids"] == ["page_c1", "page_n1", "page_n2"]
        assert final_writes["in_progress"] == []
        assert final_writes["invalid"] == []
    finally:
        rh.configure_snapshot_environment()  # 恢复默认归档环境
        shutil.rmtree(root, ignore_errors=True)


def test_migration_failure_and_retry():
    """迁移失败可重试，已完成项幂等跳过。"""
    root, snaps, _, _ = _make_env()
    try:
        _seed_full_records(snaps, count=2)
        with open(os.path.join(snaps, "page_x9.json"), "w", encoding="utf-8") as f:
            f.write("{not-a-valid-json")  # 损坏的旧快照
        repo = SnapshotRepository(snaps)
        migrator = SnapshotMigrator(repository=repo)

        first = migrator.migrate_all()
        assert sorted(first["migrated"]) == ["page_b1", "page_b2"]
        assert list(first["failed"]) == ["page_x9"]
        status = migrator.status()
        assert status["done"] == 2 and status["failed"] == 1
        # 损坏文件不计入有效页面
        assert [r.snapshot_id for r in repo.valid_pages()] == ["page_b1", "page_b2"]

        # 幂等：已完成的不重复迁移；失败再试仍失败但不产生新档案
        again = migrator.migrate_all()
        assert again["migrated"] == []
        assert migrator.status()["done"] == 2
        assert repo.verify_writes()["migration"]["archives"] == 2

        # 修复后重试失败项
        _write_legacy(snaps, "x9", "https://t1.example.com/x9", BATCH,
                      raw_dom="<html>fixed</html>")
        retried = migrator.retry_failed()
        assert retried["migrated"] == ["page_x9"]
        assert retried["failed"] == {}
        status = migrator.status()
        assert status["done"] == 3 and status["failed"] == 0
        assert repo.read_snapshot("page_x9")["raw_dom"] == "<html>fixed</html>"

        # 失败与重试都留有审计记录
        journal = _read_jsonl(os.path.join(snaps, SnapshotMigrator.JOURNAL_FILE))
        x9_states = [j["state"] for j in journal if j["snapshot_id"] == "page_x9"]
        assert x9_states == ["failed", "failed", "done"], x9_states
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_predeployment_report_refs_resolve_after_migration():
    """部署前旧报告按旧文件名关联快照，迁移后引用仍可解析。"""
    root, snaps, _, reports = _make_env()
    try:
        preview_seeded = _seed_preview_only(snaps)
        full_seeded = _seed_full_records(snaps)
        repo = SnapshotRepository(snaps)

        legacy_refs = [f"page_{h}.json" for h in list(preview_seeded) + list(full_seeded)]
        old_report = {
            "trigger": "run_history_confirm",
            "run_id": 999,
            "timestamp": "2026-11-01T08:00:00",
            "snapshot_refs": legacy_refs,
        }
        with open(os.path.join(reports, "dedup_report.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(old_report, ensure_ascii=False) + "\n")

        SnapshotMigrator(repository=repo).migrate_all()
        expected = {**preview_seeded, **full_seeded}
        for ref in old_report["snapshot_refs"]:
            resolved = repo.resolve_reference(ref)
            assert resolved is not None, f"旧报告引用失效: {ref}"
            assert resolved.source == "dual", "迁移后两份物理档案并存"
            original = expected[resolved.snapshot_id.replace("page_", "")]
            payload = repo.read_snapshot(resolved.snapshot_id)
            assert payload["dom_preview"] == original["dom_preview"]
            if "raw_dom" in original:
                assert payload["raw_dom"] == original["raw_dom"]
        # 迁移不删除旧文件，旧物理引用保持有效
        for ref in legacy_refs:
            assert os.path.exists(os.path.join(snaps, ref))
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    test_archive_format_version_and_compression()
    print("PASS: test_archive_format_version_and_compression")
    test_preview_only_snapshots_keep_preview_level()
    print("PASS: test_preview_only_snapshots_keep_preview_level")
    test_inprogress_archive_invisible_until_committed()
    print("PASS: test_inprogress_archive_invisible_until_committed")
    test_dual_physical_archives_merged()
    print("PASS: test_dual_physical_archives_merged")
    test_run_confirmation_mid_migration()
    print("PASS: test_run_confirmation_mid_migration")
    test_migration_failure_and_retry()
    print("PASS: test_migration_failure_and_retry")
    test_predeployment_report_refs_resolve_after_migration()
    print("PASS: test_predeployment_report_refs_resolve_after_migration")
    print("\nALL 7 MIGRATION TESTS PASSED")
