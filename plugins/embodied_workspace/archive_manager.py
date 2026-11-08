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
        candidates: list[Path] = []
        files_dir = dist_dir / "files"
        if files_dir.is_dir():
            for item in sorted(files_dir.rglob("*")):
                if item.is_file():
                    candidates.append(item)

        for rel in declared_files or []:
            target = (dist_dir / rel.lstrip("/")).resolve()
            if str(target).startswith(str(dist_dir.resolve())) and target.is_file():
                if target not in candidates:
                    candidates.append(target)

        if not candidates:
            return []

        date_str = datetime.date.today().isoformat()
        folder_name = f"{date_str}_{project_slug}"
        dest_dir = self.gdrive_archive_root / chat_safe_id / folder_name
        await asyncio.to_thread(dest_dir.mkdir, parents=True, exist_ok=True)

        archived_names: list[str] = []
        manifest_items: list[dict[str, Any]] = []

        for src_file in candidates:
            dest_file = dest_dir / src_file.name
            await asyncio.to_thread(shutil.copy2, src_file, dest_file)
            digest = await asyncio.to_thread(self._sha256_file, dest_file)
            archived_names.append(src_file.name)
            manifest_items.append(
                {
                    "file_name": src_file.name,
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
