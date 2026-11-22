"""页面快照归档格式：带格式版本与gzip压缩内容的快照档案读写。

物理布局（snapshots 目录内）：
  page_<hash>.json                          旧版单JSON快照（视为 format_version 1）
  page_<hash>.snapgz                        新版压缩快照档案（format_version 2）
  page_<hash>.snapgz.inprogress-<pid>-<n>   写入中的临时档案，提交前对读取方不可见

归档信封（gzip 解压后的 JSON）：
  {
    "format": "page-snapshot-archive",
    "format_version": 2,
    "snapshot_id": "page_<hash>",
    "content_level": "full" | "preview",   # 既有数据只有预览时保持 preview 层级
    "origin": "migration" | "collector",   # 写入来源，供并发写入分别核对
    "archived_at": "ISO时间",
    "payload": { ... 原始快照内容 ... }
  }

所有写入先落到 inprogress 临时文件，再用 os.replace 原子提交，
因此读取方永远不会看到写了一半的档案。
"""
from __future__ import annotations

import gzip
import json
import os
import uuid
from typing import Any

ARCHIVE_FORMAT = "page-snapshot-archive"
ARCHIVE_VERSION = 2
ARCHIVE_SUFFIX = ".snapgz"
IN_PROGRESS_MARK = ".inprogress-"

LEGACY_SUFFIX = ".json"
LEGACY_PREFIX = "page_"


class ArchiveFormatError(ValueError):
    """归档信封格式或版本不受支持。"""


def content_level_of(payload: dict[str, Any]) -> str:
    """按载荷内容判定层级：含完整DOM为 full，仅有旧版本预览为 preview。"""
    return "full" if payload.get("raw_dom") else "preview"


def archive_name(snapshot_id: str) -> str:
    return f"{snapshot_id}{ARCHIVE_SUFFIX}"


def is_inprogress_name(name: str) -> bool:
    return f"{ARCHIVE_SUFFIX}{IN_PROGRESS_MARK}" in name


def snapshot_id_from_name(name: str) -> str:
    """从物理文件名（旧版JSON / 压缩档案 / 裸ID）还原快照ID。"""
    base = os.path.basename(name)
    if is_inprogress_name(base):
        base = base.split(IN_PROGRESS_MARK, 1)[0]
    if base.endswith(ARCHIVE_SUFFIX):
        return base[: -len(ARCHIVE_SUFFIX)]
    if base.endswith(LEGACY_SUFFIX):
        return base[: -len(LEGACY_SUFFIX)]
    return base


def make_envelope(
    snapshot_id: str,
    payload: dict[str, Any],
    *,
    origin: str,
    content_level: str | None = None,
    archived_at: str = "",
) -> dict[str, Any]:
    return {
        "format": ARCHIVE_FORMAT,
        "format_version": ARCHIVE_VERSION,
        "snapshot_id": snapshot_id,
        "content_level": content_level or content_level_of(payload),
        "origin": origin,
        "archived_at": archived_at,
        "payload": payload,
    }


def validate_envelope(envelope: dict[str, Any]) -> None:
    if envelope.get("format") != ARCHIVE_FORMAT:
        raise ArchiveFormatError(f"未知归档格式: {envelope.get('format')!r}")
    version = envelope.get("format_version")
    if version != ARCHIVE_VERSION:
        raise ArchiveFormatError(f"不支持的归档版本: {version!r}")
    if not envelope.get("snapshot_id"):
        raise ArchiveFormatError("归档缺少 snapshot_id")
    if not isinstance(envelope.get("payload"), dict):
        raise ArchiveFormatError("归档缺少 payload")


def begin_archive(snapshots_dir: str, snapshot_id: str) -> str:
    """开启一份写入中的档案（临时文件），返回临时路径。"""
    os.makedirs(snapshots_dir, exist_ok=True)
    name = (
        f"{snapshot_id}{ARCHIVE_SUFFIX}{IN_PROGRESS_MARK}"
        f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    path = os.path.join(snapshots_dir, name)
    with open(path, "wb"):
        pass
    return path


def write_envelope_file(path: str, envelope: dict[str, Any]) -> None:
    """把信封以gzip压缩JSON写入指定路径（调用方保证路径是临时文件）。"""
    validate_envelope(envelope)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(envelope, f, ensure_ascii=False, default=str)


def commit_archive(tmp_path: str) -> str:
    """原子提交写入中的档案，返回正式档案路径。"""
    final_path = tmp_path.split(IN_PROGRESS_MARK, 1)[0]
    os.replace(tmp_path, final_path)
    return final_path


def abort_archive(tmp_path: str) -> None:
    if os.path.exists(tmp_path):
        os.remove(tmp_path)


def write_archive(
    snapshots_dir: str,
    snapshot_id: str,
    payload: dict[str, Any],
    *,
    origin: str,
    content_level: str | None = None,
    archived_at: str = "",
) -> str:
    """原子写入一份压缩快照档案，返回档案文件名。"""
    envelope = make_envelope(
        snapshot_id, payload,
        origin=origin, content_level=content_level, archived_at=archived_at,
    )
    tmp_path = begin_archive(snapshots_dir, snapshot_id)
    try:
        write_envelope_file(tmp_path, envelope)
    except Exception:
        abort_archive(tmp_path)
        raise
    commit_archive(tmp_path)
    return archive_name(snapshot_id)


def read_envelope(path: str) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        envelope = json.load(f)
    validate_envelope(envelope)
    return envelope


def read_archive(snapshots_dir: str, filename: str) -> dict[str, Any]:
    return read_envelope(os.path.join(snapshots_dir, filename))


def list_archives(snapshots_dir: str) -> list[str]:
    """已提交的压缩档案（写入中的临时档案不可见）。"""
    if not os.path.isdir(snapshots_dir):
        return []
    return sorted(
        name for name in os.listdir(snapshots_dir)
        if name.endswith(ARCHIVE_SUFFIX) and not is_inprogress_name(name)
    )


def inprogress_archives(snapshots_dir: str) -> list[str]:
    """写入中、尚未提交的临时档案。"""
    if not os.path.isdir(snapshots_dir):
        return []
    return sorted(
        name for name in os.listdir(snapshots_dir)
        if is_inprogress_name(name)
    )


def list_legacy(snapshots_dir: str) -> list[str]:
    """旧版单JSON快照文件。"""
    if not os.path.isdir(snapshots_dir):
        return []
    return sorted(
        name for name in os.listdir(snapshots_dir)
        if name.startswith(LEGACY_PREFIX) and name.endswith(LEGACY_SUFFIX)
    )
