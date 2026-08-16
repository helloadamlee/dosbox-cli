from pathlib import Path

import pytest

from dosbox_mcp.binary_discovery import BinaryNotFoundError, resolve_dosbox_binary


def test_explicit_path_returned_when_it_exists(tmp_path):
    fake_binary = tmp_path / "dosbox-x.exe"
    fake_binary.write_text("not a real binary")

    result = resolve_dosbox_binary(explicit_path=str(fake_binary))

    assert result == fake_binary


def test_explicit_path_raises_when_missing(tmp_path):
    missing = tmp_path / "does-not-exist.exe"

    with pytest.raises(BinaryNotFoundError):
        resolve_dosbox_binary(explicit_path=str(missing))


def test_falls_back_to_candidate_location(monkeypatch, tmp_path):
    fake_binary = tmp_path / "dosbox-x"
    fake_binary.write_text("not a real binary")

    monkeypatch.setattr(
        "dosbox_mcp.binary_discovery._candidate_paths",
        lambda: iter([tmp_path / "nope", fake_binary]),
    )

    result = resolve_dosbox_binary()

    assert result == fake_binary


def test_falls_back_to_path_when_no_candidate_matches(monkeypatch, tmp_path):
    on_path = tmp_path / "dosbox-x"
    on_path.write_text("not a real binary")

    monkeypatch.setattr(
        "dosbox_mcp.binary_discovery._candidate_paths",
        lambda: iter([tmp_path / "nope"]),
    )
    monkeypatch.setattr(
        "dosbox_mcp.binary_discovery.shutil.which",
        lambda name: str(on_path),
    )

    result = resolve_dosbox_binary()

    assert result == on_path


def test_raises_when_nothing_found(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "dosbox_mcp.binary_discovery._candidate_paths",
        lambda: iter([tmp_path / "nope"]),
    )
    monkeypatch.setattr(
        "dosbox_mcp.binary_discovery.shutil.which",
        lambda name: None,
    )

    with pytest.raises(BinaryNotFoundError):
        resolve_dosbox_binary()
