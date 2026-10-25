from pathlib import Path
from aston.scanner import parser
from aston.watcher import watcher
import json

def repo_map(repo_path: str) -> dict:
    rel_path = Path(repo_path)
    root = rel_path.expanduser().resolve()

    dependencies = watcher(repo_path)
    
    all_files_data = {}
    global_symbols = {}
    all_local_calls = []
    all_diagnostics = []

    py_files = [
        f for f in root.rglob("*.py") 
        if f.name != "__init__.py"
    ]

    for py_file in py_files:
        relative_path = str(py_file.relative_to(root))
        parsed = parser(py_file, relative_path=relative_path)
        
        all_files_data[relative_path] = {
            "parse_status": parsed["file_meta"]["parse_status"],
            "sha256": parsed["file_meta"]["sha256"],
            "imports": parsed["imports"],
            "usages": parsed["usages"],
            "diagnostics": parsed["diagnostics"]
        }
        
        global_symbols.update(parsed["symbols"])
        all_local_calls.extend(parsed["local_calls"])
        all_diagnostics.extend(parsed["diagnostics"])

    # Resolve Relationships across files
    relationships = []
    for call in all_local_calls:
        target_symbol_key = None
        target_file = None
        
        for sym_key, sym_data in global_symbols.items():
            if sym_data["qualified_name"] == call["callee_name"]:
                target_symbol_key = sym_data["qualified_name"]
                target_file = sym_data["file"]
                break

        if target_symbol_key and target_file:
            relationships.append({
                "kind": "local_call",
                "source_file": call["source_file"],
                "source_symbol": call["source_symbol"],
                "line": call["line"],
                "column": call["column"],
                "end_column": call["end_column"],
                "target_file": target_file,
                "target_symbol": target_symbol_key,
                "resolution_status": "resolved"
            })
        else:
            relationships.append({
                "kind": "local_call",
                "source_file": call["source_file"],
                "source_symbol": call["source_symbol"],
                "line": call["line"],
                "column": call["column"],
                "end_column": call["end_column"],
                "target_file": None,
                "target_symbol": None,
                "resolution_status": "unresolved",
                "reason": "callee_is_parameter"
            })

    repo_index = {
        "schema_version": "1.1",
        "repository": {
            "root": root.name,
            "language": "python",
            "index_status": "complete"
        },
        "dependencies": dependencies,
        "symbols": global_symbols,
        "relationships": relationships,
        "files": all_files_data,
        "diagnostics": all_diagnostics
    }

    return repo_index

repo_map('./demo')