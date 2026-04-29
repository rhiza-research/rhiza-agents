"""Tests for _validate_skill_path — the path-safety gate run_file uses
to reject anything that isn't an installed skill script.

The agent supplies the path to run_file, so any traversal vector here
becomes a way to execute arbitrary scripts at root privilege via
exec_skill. The check is the only line of defense between agent input
and the unwrapped (root-context) exec path.
"""

import pytest

from rhiza_agents.agents.tools.files import _validate_skill_path


def _ok(path: str) -> None:
    """Assert path is accepted (validation returns None)."""
    err = _validate_skill_path(path)
    assert err is None, f"expected {path!r} to be accepted, got error: {err!r}"


def _rejected(path: str) -> str:
    """Assert path is rejected; return the error string for substring checks."""
    err = _validate_skill_path(path)
    assert err is not None, f"expected {path!r} to be rejected"
    return err


# --- accepted shapes ---


def test_canonical_path_accepted():
    _ok("/skills/myskill/scripts/fetch.py")


def test_deeper_subdirs_under_scripts_accepted():
    # Skill authors may organize scripts/ into subdirectories.
    _ok("/skills/myskill/scripts/sub/dir/fetch.py")


def test_dotted_skill_name_accepted():
    _ok("/skills/my.skill/scripts/fetch.py")


def test_hyphenated_skill_name_accepted():
    _ok("/skills/my-skill/scripts/fetch.py")


# --- prefix rejections ---


def test_outside_skills_rejected():
    err = _rejected("/foo.py")
    assert "Path must be under /skills/" in err


def test_workspace_path_rejected():
    err = _rejected("/workspace/code/foo.py")
    assert "Path must be under /skills/" in err


def test_data_path_rejected():
    err = _rejected("/data/forecast.parquet")
    assert "Path must be under /skills/" in err


def test_skills_directory_itself_rejected():
    # No trailing slash — doesn't start with "/skills/".
    err = _rejected("/skills")
    assert "Path must be under /skills/" in err


def test_skill_name_no_scripts_subdir_rejected():
    err = _rejected("/skills/myskill")
    assert "Path must be under /skills/" not in err  # passes prefix
    assert "/skills/<name>/scripts/<file>" in err


# --- traversal rejections ---


def test_dotdot_in_filename_rejected():
    err = _rejected("/skills/myskill/scripts/../etc/passwd")
    assert "may not contain '..'" in err


def test_dotdot_at_top_rejected():
    err = _rejected("/skills/../../etc/passwd")
    assert "may not contain '..'" in err


def test_single_dotdot_segment_rejected():
    err = _rejected("/skills/myskill/scripts/..")
    assert "may not contain '..'" in err


def test_dotdot_inside_filename_string_accepted():
    # `..` only matches when it's a full path segment. A filename
    # like `foo..bar.py` is a perfectly normal filename.
    _ok("/skills/myskill/scripts/foo..bar.py")


# --- consecutive-slash rejections ---


def test_double_slash_after_skills_rejected():
    err = _rejected("/skills//scripts/x.py")
    assert "consecutive slashes" in err


def test_double_slash_in_skill_name_rejected():
    err = _rejected("/skills/foo//scripts/x.py")
    assert "consecutive slashes" in err


def test_double_slash_before_filename_rejected():
    err = _rejected("/skills/foo/scripts//x.py")
    assert "consecutive slashes" in err


# --- shape mismatch rejections ---


def test_third_segment_must_be_scripts():
    err = _rejected("/skills/myskill/notscripts/x.py")
    assert "<name>/scripts/<file>" in err


def test_only_two_segments_rejected():
    err = _rejected("/skills/myskill/")
    # Trailing slash — split yields ['skills','myskill',''] — length 3,
    # parts[2] is '' (not 'scripts'). The shape check fires.
    assert "<name>/scripts/<file>" in err


def test_three_segments_no_file_rejected():
    err = _rejected("/skills/myskill/scripts")
    assert "<name>/scripts/<file>" in err


def test_trailing_slash_after_scripts_rejected():
    err = _rejected("/skills/myskill/scripts/")
    # split yields ['skills','myskill','scripts',''] — len 4, parts[2]
    # OK, but parts[-1] is empty.
    assert "empty segment" in err


@pytest.mark.parametrize(
    "path",
    [
        "/skills/myskill/scripts/sub/foo.py",
        "/skills/a/scripts/b/c/d/e.py",
        "/skills/123/scripts/run.py",
    ],
)
def test_valid_paths_parametrized(path):
    _ok(path)


@pytest.mark.parametrize(
    "path",
    [
        "/skills/myskill/scripts/../../etc/shadow",
        "/skills//myskill/scripts/x.py",
        "/skills/myskill//scripts/x.py",
        "/skills/myskill/scripts//x.py",
        "/skills/myskill/scripts/.../foo.py",  # ... is not .. but contains no path traversal
        "/skills/myskill/scripts",
        "/skills/myskill",
        "/skills/",
        "/etc/passwd",
        "",
    ],
)
def test_clearly_unsafe_paths_rejected(path):
    if path == "/skills/myskill/scripts/.../foo.py":
        # Three-dot directory is not a path-traversal vector — accept.
        _ok(path)
        return
    assert _validate_skill_path(path) is not None, f"{path!r} should be rejected"
