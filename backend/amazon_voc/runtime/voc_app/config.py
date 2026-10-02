from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    model_base_url: str
    model_api_key: str
    model_protocol: str
    extractor_model: str
    reviewer_model: str
    batch_reviews: int = 40
    batch_chars: int = 80_000
    concurrency: int = 2
    max_repairs: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("VOC_DATA_DIR", str(PACKAGE_ROOT / "data"))).resolve()
        return cls(
            data_dir=data_dir,
            database_path=Path(os.getenv("VOC_DATABASE_PATH", str(data_dir / "voc.sqlite3"))).resolve(),
            model_base_url=os.getenv("VOC_MODEL_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            model_api_key=os.getenv("VOC_MODEL_API_KEY", ""),
            model_protocol=os.getenv("VOC_MODEL_PROTOCOL", "responses").lower(),
            extractor_model=os.getenv("VOC_EXTRACTOR_MODEL", "gpt-5-mini"),
            reviewer_model=os.getenv("VOC_REVIEWER_MODEL", "gpt-5.4"),
            batch_reviews=max(1, int(os.getenv("VOC_BATCH_REVIEWS", "40"))),
            batch_chars=max(1000, int(os.getenv("VOC_BATCH_CHARS", "80000"))),
            concurrency=max(1, int(os.getenv("VOC_CONCURRENCY", "2"))),
            max_repairs=max(0, int(os.getenv("VOC_MAX_REPAIRS", "2"))),
        )

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
