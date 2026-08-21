"""Tests for the recoverable Git-to-HERMES_HOME LMI overlay sync."""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/ops/lmi_media_overlay_sync.py"
PINNED_MEDIA = Path(
    "/Users/saksham/Documents/Codex/2026-08-21/j/work/lmi-dashboard/vm/leadgen/unipile_media_followup.py"
)


def load_sync_module():
    spec = importlib.util.spec_from_file_location("lmi_media_overlay_sync_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_apply_copies_exact_managed_hashes_and_creates_backup(tmp_path):
    sync = load_sync_module()
    data_root = tmp_path / "data"
    backup_root = tmp_path / "backups"
    (data_root / "plugins/platforms").mkdir(parents=True)
    module_path = data_root / "approved/unipile_media_followup.py"
    module_path.parent.mkdir(parents=True)
    shutil.copy2(PINNED_MEDIA, module_path)
    (data_root / "runtime.env").write_text(
        f"LMI_MEDIA_FOLLOWUP_MODULE_PATH={module_path}\n", encoding="utf-8"
    )

    result = sync.synchronize(
        repo_root=REPO,
        data_root=data_root,
        backup_root=backup_root,
    )

    assert result["mode"] == "apply"
    assert len(result["target_hashes"]) == len(sync.EXPECTED_FILES)
    backup = Path(result["backup"])
    assert (backup / "MANIFEST.json").is_file()
    manifest = json.loads((backup / "MANIFEST.json").read_text())
    assert set(manifest["files"]) == set(sync.EXPECTED_FILES)

    check = sync.synchronize(
        repo_root=REPO,
        data_root=data_root,
        backup_root=backup_root,
        check_only=True,
    )
    assert check["mode"] == "check"
    assert check["target_hashes"] == result["target_hashes"]


def test_wrong_dashboard_module_hash_blocks_without_writing(tmp_path):
    sync = load_sync_module()
    data_root = tmp_path / "data"
    bad_module = data_root / "bad/unipile_media_followup.py"
    bad_module.parent.mkdir(parents=True)
    bad_module.write_text("not the reviewed source\n", encoding="utf-8")
    (data_root / "runtime.env").write_text(
        f"LMI_MEDIA_FOLLOWUP_MODULE_PATH={bad_module}\n", encoding="utf-8"
    )

    with pytest.raises(sync.SyncBlocked, match="module hash"):
        sync.synchronize(
            repo_root=REPO,
            data_root=data_root,
            backup_root=tmp_path / "backups",
        )
    assert not (data_root / "plugins/platforms/lmi_unipile_overlay").exists()
