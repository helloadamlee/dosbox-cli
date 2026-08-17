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


def test_env_var_used_when_no_explicit_path(monkeypatch, tmp_path):
    fake_binary = tmp_path / "dosbox-x"
    fake_binary.write_text("not a real binary")
    monkeypatch.setenv("DOSBOX_X_BINARY", str(fake_binary))

    result = resolve_dosbox_binary()

    assert result == fake_binary


def test_explicit_path_beats_env_var(monkeypatch, tmp_path):
    from_env = tmp_path / "from-env"
    from_env.write_text("not a real binary")
    explicit = tmp_path / "explicit"
    explicit.write_text("not a real binary")
    monkeypatch.setenv("DOSBOX_X_BINARY", str(from_env))

    result = resolve_dosbox_binary(explicit_path=str(explicit))

    assert result == explicit


def test_env_var_pointing_at_missing_file_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("DOSBOX_X_BINARY", str(tmp_path / "nope"))

    with pytest.raises(BinaryNotFoundError) as excinfo:
        resolve_dosbox_binary()

    assert "DOSBOX_X_BINARY" in str(excinfo.value)


def test_env_var_beats_candidate_paths(monkeypatch, tmp_path):
    candidate = tmp_path / "candidate"
    candidate.write_text("not a real binary")
    from_env = tmp_path / "from-env"
    from_env.write_text("not a real binary")
    monkeypatch.setattr(
        "dosbox_mcp.binary_discovery._candidate_paths", lambda: iter([candidate])
    )
    monkeypatch.setenv("DOSBOX_X_BINARY", str(from_env))

    result = resolve_dosbox_binary()

    assert result == from_env


def test_not_found_message_warns_about_stock_dosbox(monkeypatch):
    monkeypatch.delenv("DOSBOX_X_BINARY", raising=False)
    monkeypatch.setattr(
        "dosbox_mcp.binary_discovery._candidate_paths", lambda: iter([])
    )
    monkeypatch.setattr("dosbox_mcp.binary_discovery.shutil.which", lambda _: None)

    with pytest.raises(BinaryNotFoundError) as excinfo:
        resolve_dosbox_binary()

    message = str(excinfo.value)
    assert "host-control" in message
    assert "DOSBOX_X_BINARY" in message
