from pathlib import Path
from aston.scanner import parser
from aston.watcher import watcher

def repo_map(repo_path: str) -> list:

    rel_path = Path(repo_path)

    root = rel_path.expanduser().resolve()

    repo_index = {}

    dependencies = watcher(repo_path)

    files = []

    py_files = [
        f for f in root.rglob("*.py") 
        if f.name != "__init__.py"
    ]

    for py_file in py_files:
        usages = parser(py_file)
        relative_path = py_file.relative_to(root)
        files.append({
            relative_path : usages
        })

    repo_index["root"] = root
    repo_index["dependencies"] = dependencies
    repo_index["files"] = files

    return repo_index


