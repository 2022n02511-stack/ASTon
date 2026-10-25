from pathlib import Path
from aston.scanner import parser
from aston.watcher import watcher
import json

def repo_map(repo_path: str) -> dict:
    rel_path = Path(repo_path)
    root = rel_path.expanduser().resolve()

    # Collect dependencies
    dependencies = watcher(repo_path)
    
    all_files_data = {}
    global_symbols = {}
    all_usages = []

    py_files = [
        f for f in root.rglob("*.py") 
        if f.name != "__init__.py"
    ]

    for py_file in py_files:
        relative_path = str(py_file.relative_to(root))
        
        # Call parser directly instead of Tracker
        parsed_data = parser(py_file, relative_path=relative_path)
        
        # Store file-level metadata
        all_files_data[relative_path] = {
            "parse_status": "success",
            "imports": parsed_data.get("imports", []),
            "usages": parsed_data.get("usages", [])
        }
        
        # Aggregate global symbols and usages for cross-file graph building later
        global_symbols.update(parsed_data.get("symbols", {}))
        all_usages.extend(parsed_data.get("usages", []))

    repo_index = {
        "schema_version": "1.1",
        "repository": {
            "root": root.name,
            "language": "python",
            "index_status": "complete"
        },
        "dependencies": dependencies,
        "symbols": global_symbols,
        "relationships": [], # Will be populated in the relationship pass
        "files": all_files_data,
        "diagnostics": []
    }

    print(json.dumps(repo_index, indent=3))
    return repo_index

repo_map('./demo')