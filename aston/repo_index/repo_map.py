from pathlib import Path
from aston.scanner import parser
from aston.watcher import watcher
import json
import jedi

def repo_map(repo_path: str) -> dict:
    rel_path = Path(repo_path)
    root = rel_path.expanduser().resolve()

    dependencies = watcher(repo_path)
    files = []

    py_files = [
        f for f in root.rglob("*.py") 
        if f.name != "__init__.py"
    ]

    # Initialize ONE shared Jedi project for the entire repo scan
    shared_project = jedi.Project(path=str(root))

    for py_file in py_files:
        # Pass the shared project down
        usages = parser(py_file, project=shared_project)
        relative_path = str(py_file.relative_to(root))
        files.append({
            relative_path: usages
        })

    repo_index = {
        "root": str(root),
        "dependencies": dependencies,
        "files": files
    }

    print(repo_index)
    return repo_index


repo_map('./demo')