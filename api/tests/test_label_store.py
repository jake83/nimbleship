import os
import time
from pathlib import Path

import pytest

from nimbleship.labels.store import LabelStore


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    store = LabelStore(tmp_path)

    store.save("95000254580", b"%PDF-fake")

    assert store.load("95000254580") == b"%PDF-fake"


def test_missing_label_loads_as_none(tmp_path: Path) -> None:
    assert LabelStore(tmp_path).load("95000254580") is None


def test_order_numbers_cannot_traverse_paths(tmp_path: Path) -> None:
    store = LabelStore(tmp_path)

    with pytest.raises(ValueError, match="path separators"):
        store.save("../escape", b"nope")
    with pytest.raises(ValueError, match="path separators"):
        store.load("../../etc/passwd")


def test_prune_removes_only_labels_older_than_cutoff(tmp_path: Path) -> None:
    store = LabelStore(tmp_path)
    store.save("OLD", b"%PDF-old")
    store.save("FRESH", b"%PDF-fresh")
    ancient = time.time() - 40 * 86400
    os.utime(tmp_path / "OLD.pdf", (ancient, ancient))

    pruned = store.prune(older_than_days=30)

    assert pruned == 1
    assert store.load("OLD") is None
    assert store.load("FRESH") is not None
