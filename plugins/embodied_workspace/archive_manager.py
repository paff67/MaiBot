from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


class WorkspaceArchiveManager:
    """
    直接向已挂载的 /mnt/gdrive/companion_vault/workspace_archives/<chat_safe_id>/<YYYY-MM-DD>_<slug>/
    写入衍生文件与 manifest.json，由 rclone VFS FUSE 层自动异步同步至 Google Drive，避免二次 rclone copy 锁竞争。
    """

    def __init__(self, gdrive_archive_root: Path) -> None:
        self.gdrive_archive_root = gdrive_archive_root

    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    async def archive_job_artifacts(
        self,
        *,
        chat_safe_id: str,
        project_slug: str,
        title: str,
        task_prompt: str,
        short_url: str,
        dist_dir: Path,
        declared_files: list[str] | None = None,
    ) -> list[str]:
        dist_root = dist_dir.resolve()
        files_dir = (dist_root / "files").resolve()
        candidates: list[tuple[Path, Path]] = []
        seen_sources: set[Path] = set()

        if files_dir.is_dir():
            for item in sorted(files_dir.rglob("*")):
                if item.is_file():
                    resolved_item = item.resolve()
                    if resolved_item.is_relative_to(files_dir):
                        rel_dest = resolved_item.relative_to(files_dir)
                        candidates.append((resolved_item, rel_dest))
                        seen_sources.add(resolved_item)

        for rel in declared_files or []:
            clean_rel = str(rel or "").strip().lstrip("/\\")
            if not clean_rel:
                continue
            target = (dist_root / clean_rel).resolve()
            if target.is_relative_to(dist_root) and target.is_file() and target not in seen_sources:
                rel_dest = (
                    target.relative_to(files_dir)
                    if target.is_relative_to(files_dir)
                    else target.relative_to(dist_root)
                )
                candidates.append((target, rel_dest))
                seen_sources.add(target)

        if not candidates:
            return []

        date_str = datetime.date.today().isoformat()
        folder_name = f"{date_str}_{project_slug}"
        dest_dir = (self.gdrive_archive_root / chat_safe_id / folder_name).resolve()
        await asyncio.to_thread(dest_dir.mkdir, parents=True, exist_ok=True)

        archived_names: list[str] = []
        manifest_items: list[dict[str, Any]] = []

        for src_file, rel_dest in candidates:
            dest_file = (dest_dir / rel_dest).resolve()
            if not dest_file.is_relative_to(dest_dir):
                continue
            await asyncio.to_thread(dest_file.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copy2, src_file, dest_file)
            digest = await asyncio.to_thread(self._sha256_file, dest_file)
            rel_posix = rel_dest.as_posix()
            archived_names.append(rel_posix)
            manifest_items.append(
                {
                    "file_name": rel_posix,
                    "size_bytes": dest_file.stat().st_size,
                    "sha256": digest,
                }
            )

        manifest_data = {
            "archived_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "chat_safe_id": chat_safe_id,
            "project_slug": project_slug,
            "title": title,
            "task_prompt": task_prompt,
            "short_url": short_url,
            "files": manifest_items,
        }
        manifest_path = dest_dir / "manifest.json"
        await asyncio.to_thread(
            manifest_path.write_text,
            json.dumps(manifest_data, ensure_ascii=False, indent=2),
            "utf-8",
        )
        return archived_names
