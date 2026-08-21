"""Focused regression tests for the updater's deployment-safety flags."""

import argparse
import os
import subprocess
from pathlib import Path

from hermes_cli.subcommands.update import build_update_parser
from hermes_cli.update_cmd import _finalize_update_stash, _git_cmd_for_repo


def _update_parser():
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_update_parser(subparsers, cmd_update=lambda args: args)
    return parser


def test_update_parser_accepts_keep_stash():
    args = _update_parser().parse_args(["update", "--keep-stash"])

    assert args.keep_stash is True


def test_update_parser_defaults_keep_stash_to_false():
    args = _update_parser().parse_args(["update"])

    assert args.keep_stash is False


def test_git_command_scopes_safe_directory_to_checkout(tmp_path):
    command = _git_cmd_for_repo(tmp_path)

    assert command == ["git", "-c", f"safe.directory={tmp_path.resolve()}"]


def test_dashboard_branch_probe_scopes_safe_directory(monkeypatch, tmp_path):
    import hermes_cli.web_server as ws

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="oc-branding\n")

    monkeypatch.setattr(ws.subprocess, "run", fake_run)

    assert ws._fs_git_branch(str(tmp_path)) == "oc-branding"
    assert calls[0][0:3] == [
        "git",
        "-c",
        f"safe.directory={tmp_path.resolve()}",
    ]


def test_keep_stash_does_not_apply_or_drop(monkeypatch, tmp_path, capsys):
    applied = []
    dropped = []

    monkeypatch.setattr(
        "hermes_cli.update_cmd._restore_stashed_changes",
        lambda *args, **kwargs: applied.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "hermes_cli.update_cmd._discard_stashed_changes",
        lambda *args, **kwargs: dropped.append((args, kwargs)),
    )

    _finalize_update_stash(
        ["git"],
        tmp_path,
        "stash@{0}",
        keep_stash=True,
        discard_local_changes=True,
    )

    assert applied == []
    assert dropped == []
    assert "preserved in git stash" in capsys.readouterr().out


def test_autosave_leaves_saved_changes_unstaged(tmp_path):
    """The autosave branch must not leave every saved file staged."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=repo, check=check, capture_output=True, text=True
        )

    git("init", "-q", "-b", "main")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.invalid")
    (repo / "staged.txt").write_text("before\n")
    (repo / "unstaged.txt").write_text("before\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    (repo / "staged.txt").write_text("staged local edit\n")
    git("add", "staged.txt")
    (repo / "unstaged.txt").write_text("unstaged local edit\n")
    (repo / "new.txt").write_text("untracked local file\n")

    script = Path(__file__).resolve().parents[2] / "scripts/ops/oc-autosave.sh"
    result = subprocess.run(
        [str(script)],
        cwd=repo,
        env={
            **os.environ,
            "REPO": str(repo),
            "LOG": str(tmp_path / "autosave.log"),
            "TOKEN_FILE": str(tmp_path / "missing-token"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    status = git("status", "--porcelain").stdout.splitlines()
    assert " M staged.txt" in status
    assert " M unstaged.txt" in status
    assert "?? new.txt" in status
    assert git("diff", "--cached", "--quiet", check=False).returncode == 0
    assert git("branch", "--list", "autosave/vm-*").stdout.strip()


def test_autosave_commit_failure_does_not_leave_a_dirty_index(tmp_path):
    """A rejected autosave commit must preserve content but clear `git add -A`."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=repo, check=check, capture_output=True, text=True
        )

    git("init", "-q", "-b", "main")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.invalid")
    tracked = repo / "tracked.txt"
    tracked.write_text("before\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    tracked.write_text("preserve me\n")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    script = Path(__file__).resolve().parents[2] / "scripts/ops/oc-autosave.sh"
    result = subprocess.run(
        [str(script)],
        cwd=repo,
        env={
            **os.environ,
            "REPO": str(repo),
            "LOG": str(tmp_path / "autosave.log"),
            "TOKEN_FILE": str(tmp_path / "missing-token"),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert tracked.read_text() == "preserve me\n"
    assert git("diff", "--cached", "--quiet", check=False).returncode == 0
    assert " M tracked.txt" in git("status", "--porcelain").stdout.splitlines()
