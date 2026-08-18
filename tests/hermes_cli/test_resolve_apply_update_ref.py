"""_resolve_apply_update_ref: missing or diverged origin/<branch>.

Backend Update used to crash with `git rev-list HEAD..origin/main --count`
exit 128 when remote.origin.fetch was narrowed (oc-branding-only). A present
but diverged origin/main is worse: ff-only fails and the apply path
reset --hard origin/main would rewind a checkout that tracks official
upstream/main onto a stale fork tip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli.update_cmd import _resolve_apply_update_ref


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path, *, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", branch)
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")


def _commit(path: Path, name: str) -> str:
    (path / name).write_text(name + "\n", encoding="utf-8")
    _git(path, "add", name)
    _git(path, "commit", "-m", name)
    return _git(path, "rev-parse", "HEAD")


def test_missing_origin_main_uses_upstream(tmp_path: Path):
    upstream = tmp_path / "upstream"
    local = tmp_path / "local"
    _init_repo(upstream)
    base = _commit(upstream, "base")
    _git(tmp_path, "clone", str(upstream), str(local))
    _git(local, "remote", "rename", "origin", "upstream")
    # origin exists but only publishes another branch — no origin/main.
    other = tmp_path / "origin-other"
    _init_repo(other)
    _commit(other, "unrelated")
    _git(other, "checkout", "-b", "oc-branding")
    _git(local, "remote", "add", "origin", str(other))
    _git(local, "config", "--unset-all", "remote.origin.fetch")
    _git(
        local,
        "config",
        "--add",
        "remote.origin.fetch",
        "+refs/heads/oc-branding:refs/remotes/origin/oc-branding",
    )
    _git(local, "fetch", "origin")
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "origin/main"],
        cwd=local,
        capture_output=True,
    ).returncode != 0

    chosen = _resolve_apply_update_ref(["git"], local, "main")
    assert chosen == "upstream/main"
    assert _git(local, "rev-parse", chosen) == base


def test_fast_forward_origin_is_preferred(tmp_path: Path):
    origin = tmp_path / "origin"
    local = tmp_path / "local"
    _init_repo(origin)
    _commit(origin, "base")
    _git(tmp_path, "clone", str(origin), str(local))
    _commit(origin, "ahead")
    _git(local, "fetch", "origin")

    chosen = _resolve_apply_update_ref(["git"], local, "main")
    assert chosen == "origin/main"


def test_diverged_origin_prefers_upstream(tmp_path: Path):
    upstream = tmp_path / "upstream"
    origin = tmp_path / "origin"
    local = tmp_path / "local"
    _init_repo(upstream)
    _commit(upstream, "shared")
    _git(tmp_path, "clone", str(upstream), str(origin))
    _git(origin, "config", "user.email", "test@example.com")
    _git(origin, "config", "user.name", "Test")
    _commit(origin, "fork-only")
    _commit(upstream, "official")
    _git(tmp_path, "clone", str(upstream), str(local))
    _git(local, "config", "user.email", "test@example.com")
    _git(local, "config", "user.name", "Test")
    _git(local, "remote", "rename", "origin", "upstream")
    _git(local, "remote", "add", "origin", str(origin))
    _git(local, "fetch", "origin")
    _git(local, "fetch", "upstream")

    # Local tracks official; origin/main has diverged and is not a ff.
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"],
        cwd=local,
        capture_output=True,
    ).returncode != 0
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", "HEAD", "upstream/main"],
        cwd=local,
        capture_output=True,
    ).returncode == 0

    chosen = _resolve_apply_update_ref(["git"], local, "main")
    assert chosen == "upstream/main"


def test_missing_everywhere_exits(tmp_path: Path):
    local = tmp_path / "local"
    _init_repo(local)
    _commit(local, "only")
    _git(local, "remote", "add", "origin", str(tmp_path / "missing.git"))

    with pytest.raises(SystemExit) as exc:
        _resolve_apply_update_ref(["git"], local, "main")
    assert exc.value.code == 1
