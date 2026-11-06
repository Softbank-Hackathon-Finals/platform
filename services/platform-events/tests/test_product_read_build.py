"""The image context comes from pinned regular Git blobs, never checkout debris."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("product_read_build",
    Path(__file__).resolve().parents[1] / "deploy/product-read/build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(build, "ROOT", repo)
    return repo


def commit(repo):
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
        "-c", "user.email=fixture@example.com", "commit", "-qm", "fixture"], check=True)
    return build.git("rev-parse", "HEAD").decode().strip()


def test_build_copies_only_original_committed_bytes(repository, tmp_path):
    root = repository / "src/package"
    root.mkdir(parents=True)
    (root / "module.py").write_text("original\n")
    revision = commit(repository)
    (root / "module.py").write_text("uncommitted change\n")
    (root / "private.json").write_text("untracked local data\n")
    target = tmp_path / "context"
    build.copy_committed_tree(revision, "src/package/", target)
    assert (target / "module.py").read_text() == "original\n"
    assert sorted(p.name for p in target.iterdir()) == ["module.py"]


def test_build_rejects_committed_symlink(repository, tmp_path):
    root = repository / "src/package"
    root.mkdir(parents=True)
    (root / "outside").symlink_to("../../private")
    revision = commit(repository)
    with pytest.raises(ValueError, match="regular_committed_sources_required"):
        build.copy_committed_tree(revision, "src/package/", tmp_path / "context")


def test_build_rejects_missing_package(repository, tmp_path):
    (repository / "README").write_text("fixture\n")
    revision = commit(repository)
    with pytest.raises(ValueError, match="committed_source_missing"):
        build.copy_committed_tree(revision, "src/package/", tmp_path / "context")
