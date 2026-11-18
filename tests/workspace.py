import pytest
from pathlib import Path
from aston.paths import find_repo_root, get_aston_paths
from aston.cli.commands import handle_init, handle_status, handle_clean

@pytest.fixture
def mock_repo(tmp_path):
    """Create a temporary dummy repository structure for testing."""
    # Create a requirements.txt to satisfy root discovery
    req_file = tmp_path / "requirements.txt"
    req_file.write_text("openai==0.28.0\n", encoding="utf-8")
    
    # Create a sample source file
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    sample_file = src_dir / "main.py"
    sample_file.write_text("import openai\n", encoding="utf-8")
    
    return tmp_path


def test_find_repo_root_and_paths(mock_repo, monkeypatch):
    """Test CWD auto-discovery and centralized path helpers."""
    # Change current working directory into a nested subdirectory
    nested_dir = mock_repo / "src"
    monkeypatch.chdir(nested_dir)

    # Verify that find_repo_root successfully walks up to mock_repo
    root = find_repo_root()
    assert root.resolve() == mock_repo.resolve()

    # Verify centralized paths point inside .aston/
    paths = get_aston_paths(root)
    assert paths["root"] == mock_repo
    assert paths["aston_dir"] == mock_repo / ".aston"
    assert paths["staging"] == mock_repo / ".aston" / "staging"
    assert paths["runs"] == mock_repo / ".aston" / "runs"
    assert paths["reports"] == mock_repo / ".aston" / "reports"


def test_aston_init_workspace(mock_repo, monkeypatch):
    """Test `aston init` bootstrapping and .gitignore protection."""
    monkeypatch.chdir(mock_repo)

    # Run init
    handle_init()

    paths = get_aston_paths(mock_repo)
    
    # Check if directories were created
    assert paths["aston_dir"].is_dir()
    assert paths["staging"].is_dir()
    assert paths["runs"].is_dir()
    assert paths["reports"].is_dir()

    # Check if .gitignore was created and contains .aston/
    gitignore_path = mock_repo / ".gitignore"
    assert gitignore_path.is_file()
    assert ".aston/" in gitignore_path.read_text(encoding="utf-8")


def test_aston_status_and_clean(mock_repo, monkeypatch):
    """Test staging status reporting and workspace cleaning."""
    monkeypatch.chdir(mock_repo)
    handle_init()

    paths = get_aston_paths(mock_repo)

    # Simulate a staged migrated file
    staged_file = paths["staging"] / "src" / "main.py"
    staged_file.parent.mkdir(parents=True, exist_ok=True)
    staged_file.write_text("import openai\nfrom openai import OpenAI\n", encoding="utf-8")

    # Verify status detects the staged file without error
    handle_status()

    # Verify clean command wipes staging while keeping directory structure
    handle_clean()
    assert not any(paths["staging"].rglob("*.py"))
    assert paths["staging"].is_dir()