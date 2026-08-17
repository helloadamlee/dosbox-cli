import filecmp
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from assemble_bundle import assemble  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "host_control_client.py").write_text("# canonical client\n")
    (repo / "mcp-server" / "dosbox_mcp" / "_vendor").mkdir(parents=True)
    (repo / "mcp-server" / "pyproject.toml").write_text("[project]\nname='x'\n")
    (repo / "mcp-server" / "dosbox_mcp" / "__init__.py").write_text("")
    (repo / "mcp-server" / "dosbox_mcp" / "_vendor" / "__init__.py").write_text("")
    (repo / "mcp-server" / "dosbox_mcp" / "__pycache__").mkdir()
    (repo / "mcp-server" / "dosbox_mcp" / "__pycache__" / "junk.pyc").write_text("x")
    (repo / "mcp-server" / "tests").mkdir()
    (repo / "mcp-server" / "tests" / "test_x.py").write_text("")
    (repo / "contrib" / "bundle").mkdir(parents=True)
    (repo / "contrib" / "bundle" / "QUICKSTART.md").write_text("# quickstart\n")
    (repo / "COPYING").write_text("GPL-2.0\n")

    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "dosbox-x.exe").write_text("MZ fake binary")
    (payload / "shaders").mkdir()
    (payload / "shaders" / "a.glsl").write_text("shader")

    return repo, payload


def test_creates_expected_layout(fake_repo, tmp_path):
    repo, payload = fake_repo

    root = assemble(
        repo_root=repo, payload_dir=payload,
        version="0.1.0", platform="win64", out_dir=tmp_path / "out",
    )

    assert root.name == "dosbox-cli-0.1.0-win64"
    assert (root / "dosbox-x" / "dosbox-x.exe").is_file()
    assert (root / "dosbox-x" / "shaders" / "a.glsl").is_file()
    assert (root / "mcp-server" / "pyproject.toml").is_file()
    assert (root / "scripts" / "host_control_client.py").is_file()
    assert (root / "QUICKSTART.md").is_file()
    assert (root / "COPYING").is_file()


def test_vendors_client_identically(fake_repo, tmp_path):
    repo, payload = fake_repo

    root = assemble(
        repo_root=repo, payload_dir=payload,
        version="0.1.0", platform="win64", out_dir=tmp_path / "out",
    )

    canonical = root / "scripts" / "host_control_client.py"
    vendored = root / "mcp-server" / "dosbox_mcp" / "_vendor" / "host_control_client.py"
    assert vendored.is_file()
    assert filecmp.cmp(canonical, vendored, shallow=False)


def test_excludes_pycache_and_tests(fake_repo, tmp_path):
    repo, payload = fake_repo

    root = assemble(
        repo_root=repo, payload_dir=payload,
        version="0.1.0", platform="win64", out_dir=tmp_path / "out",
    )

    assert not (root / "mcp-server" / "tests").exists()
    assert not list(root.rglob("__pycache__"))
    assert not list(root.rglob("*.pyc"))


def test_missing_payload_is_an_error(fake_repo, tmp_path):
    repo, _ = fake_repo

    with pytest.raises(FileNotFoundError):
        assemble(
            repo_root=repo, payload_dir=tmp_path / "nope",
            version="0.1.0", platform="win64", out_dir=tmp_path / "out",
        )
