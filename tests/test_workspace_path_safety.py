"""Tests for workspace_path's traversal guard.

workspace_path is the single choke point that maps a logical file path
(as supplied by the file-viewer endpoint, ultimately from a URL path
segment) to the absolute sandbox path that read_workspace_file /
write_workspace_file run as root. A logical path with ``..`` segments
would otherwise normalize to a root-owned file outside /workspace and
/data; the guard rejects it with ValueError so the route returns 404
without any root-level filesystem access.
"""

import pytest

from rhiza_agents.agents.tools.sandbox import (
    SANDBOX_DATA,
    SANDBOX_WORKSPACE,
    workspace_path,
)


def test_plain_workspace_path_maps_under_workspace():
    assert workspace_path("/foo.py") == f"{SANDBOX_WORKSPACE}/foo.py"


def test_nested_workspace_path():
    assert workspace_path("/sub/dir/bar.csv") == f"{SANDBOX_WORKSPACE}/sub/dir/bar.csv"


def test_data_path_kept_as_is():
    assert workspace_path("/data/forecast.parquet") == f"{SANDBOX_DATA}/forecast.parquet"


def test_data_root_kept_as_is():
    assert workspace_path("/data") == SANDBOX_DATA


@pytest.mark.parametrize(
    "logical",
    [
        "/../etc/passwd",
        "/../../etc/passwd",
        "/sub/../../etc/passwd",
        "/foo/../../bar",
    ],
)
def test_workspace_traversal_rejected(logical):
    # Escapes /workspace via .. segments — must raise, not resolve to a
    # root-owned file the read/write helpers would touch as root.
    with pytest.raises(ValueError):
        workspace_path(logical)


@pytest.mark.parametrize(
    "logical",
    [
        "/data/../../etc/x",
        "/data/../etc/passwd",
        "/data/sub/../../../etc/passwd",
    ],
)
def test_data_traversal_rejected(logical):
    # The /data prefix must be protected too: /data/../../etc/x escapes
    # the data root and must be rejected.
    with pytest.raises(ValueError):
        workspace_path(logical)


def test_dot_segments_within_workspace_allowed():
    # A .. that stays within /workspace normalizes cleanly and is fine.
    assert workspace_path("/sub/../foo.py") == f"{SANDBOX_WORKSPACE}/foo.py"


def test_dot_segments_within_data_allowed():
    assert workspace_path("/data/sub/../forecast.parquet") == f"{SANDBOX_DATA}/forecast.parquet"
