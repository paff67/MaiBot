"""已弃用：请直接使用 AssetPipeline 与 quote_recall.load_original_descriptors。"""

from pathlib import Path
from typing import Tuple

from .asset_pipeline import AssetPipeline
from .quote_recall import load_original_descriptors
from .truth_db import CompanionTruthDB


class GDriveVaultManager:
    """向后兼容薄包装；内部统一委托给 AssetPipeline 原件管线。"""

    def __init__(self, vault_root: str = "/mnt/gdrive/companion_vault") -> None:
        self.vault_root = Path(vault_root)

    def ensure_fresh_gemini_uri(self, db: CompanionTruthDB, file_code: str, api_key: str) -> Tuple[str, str]:
        row = db.conn.execute("SELECT * FROM media_vault WHERE file_code = ?", (file_code,)).fetchone()
        if row is None:
            raise KeyError(file_code)
        from google.genai import Client

        assets = AssetPipeline(db, mount=str(self.vault_root))
        with Client(api_key=api_key) as client:
            descriptors = load_original_descriptors(assets, [dict(row)], client=client)
        first = descriptors[0]
        return first["file_uri"], first["mime_type"]
