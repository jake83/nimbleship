import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nimbleship.main import create_app


def test_stale_labels_are_pruned_on_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    labels = tmp_path / "labels"
    labels.mkdir()
    stale = labels / "OLD.pdf"
    stale.write_bytes(b"%PDF-old")
    ancient = time.time() - 40 * 86400
    os.utime(stale, (ancient, ancient))
    monkeypatch.setenv("NIMBLESHIP_LABELS_DIR", str(labels))

    with TestClient(create_app()):
        pass

    assert not stale.exists()
