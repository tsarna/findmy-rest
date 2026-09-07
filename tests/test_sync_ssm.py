"""Tests for the SSM/1Password sync tool.

The tool lives in scripts/ rather than the package, so it is loaded by path.
Only the pure parts are tested here -- nothing that talks to AWS or 1Password.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sync_ssm.py"

spec = importlib.util.spec_from_file_location("sync_ssm", SCRIPT)
sync_ssm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_ssm)


@pytest.mark.parametrize(
    ("ssm_path", "expected"),
    [
        (
            "/my-cluster/apps/findmy-rest/accessories/jane.keys",
            "MY__CLUSTER_APPS_FINDMY__REST_ACCESSORIES_JANE.KEYS",
        ),
        ("/my-cluster/apps/findmy-rest/password", "MY__CLUSTER_APPS_FINDMY__REST_PASSWORD"),
        # No leading underscore, however many the path starts with.
        ("/a/b", "A_B"),
        # A dash becomes two underscores, so the transform stays reversible.
        ("/x-y", "X__Y"),
        # Already-present underscores are left alone, which is what makes the
        # doubled-dash rule unambiguous.
        ("/a_b-c", "A_B__C"),
    ],
)
def test_op_title(ssm_path, expected):
    assert sync_ssm.op_title(ssm_path) == expected


def test_op_title_distinguishes_dash_from_separator():
    """The doubled underscore is what keeps these two paths apart."""
    assert sync_ssm.op_title("/a/b-c") != sync_ssm.op_title("/a/b/c")
    assert sync_ssm.op_title("/a/b-c") == "A_B__C"
    assert sync_ssm.op_title("/a/b/c") == "A_B_C"


def test_env_file_does_not_override_real_environment(tmp_path, monkeypatch):
    """A .env is a default. An explicit export must win."""
    (tmp_path / ".env").write_text("FINDMY_OP_VAULT=FROM_FILE\nFINDMY_SSM_BASE=/from/file\n")
    monkeypatch.setenv("FINDMY_OP_VAULT", "FROM_SHELL")
    monkeypatch.delenv("FINDMY_SSM_BASE", raising=False)

    sync_ssm.load_env(tmp_path, None)

    import os

    assert os.environ["FINDMY_OP_VAULT"] == "FROM_SHELL"
    assert os.environ["FINDMY_SSM_BASE"] == "/from/file"


def test_env_file_strips_quotes_and_ignores_comments(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        '# a comment\n\nFINDMY_OP_ACCOUNT="quoted.example.com"\nbad line\n'
    )
    monkeypatch.delenv("FINDMY_OP_ACCOUNT", raising=False)

    sync_ssm.load_env(tmp_path, None)

    import os

    assert os.environ["FINDMY_OP_ACCOUNT"] == "quoted.example.com"
