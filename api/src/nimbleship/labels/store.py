"""The Label Store (CONTEXT.md): private on-disk home of label PDFs.

Labels carry recipient PII, so they are served only through the API and
pruned after 30 days."""

import time
from pathlib import Path

from nimbleship.config import get_settings

PRUNE_AFTER_DAYS = 30


class LabelStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def _path(self, order_number: str) -> Path:
        if Path(order_number).name != order_number:
            raise ValueError("order number must not contain path separators")
        return self._directory / f"{order_number}.pdf"

    def save(self, order_number: str, pdf: bytes) -> None:
        self._path(order_number).write_bytes(pdf)

    def load(self, order_number: str) -> bytes | None:
        path = self._path(order_number)
        return path.read_bytes() if path.exists() else None

    def prune(self, older_than_days: int = PRUNE_AFTER_DAYS) -> int:
        cutoff = time.time() - older_than_days * 86400
        pruned = 0
        for path in self._directory.glob("*.pdf"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                pruned += 1
        return pruned


def get_label_store() -> LabelStore:
    return LabelStore(get_settings().labels_dir)
