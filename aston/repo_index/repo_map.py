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

    # Populate watched_usage_ids into symbols based on direct usages
    for file_path, file_data in all_files_data.items():
        for usage in file_data["usages"]:
            enclosing = usage.get("enclosing_symbol")
            if enclosing:
                sym_key = f"{file_path}::{enclosing}"
                if sym_key in global_symbols:
                    if usage["id"] not in global_symbols[sym_key]["watched_usage_ids"]:
                        global_symbols[sym_key]["watched_usage_ids"].append(usage["id"])

    # Resolve Relationships across files with correct span & classification
    relationships = []
    for call in all_local_calls:
        target_symbol_key = None
        target_file = None
        
        for sym_key, sym_data in global_symbols.items():
            if sym_data["qualified_name"] == call["callee_name"]:
                target_symbol_key = sym_data["qualified_name"]
                target_file = sym_data["file"]
                break

        if call["is_parameter"]:
            relationships.append({
                "kind": "local_call",
                "source_file": call["source_file"],
                "source_symbol": call["source_symbol"],
                "line": call["line"],
                "column": call["column"],
                "end_line": call["end_line"],
                "end_column": call["end_column"],
                "target_file": None,
                "target_symbol": None,
                "resolution_status": "unresolved",
                "reason": "callee_is_parameter"
            })
        elif target_symbol_key and target_file:
            relationships.append({
                "kind": "local_call",
                "source_file": call["source_file"],
                "source_symbol": call["source_symbol"],
                "line": call["line"],
                "column": call["column"],
                "end_line": call["end_line"],
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
                "end_line": call["end_line"],
                "end_column": call["end_column"],
                "target_file": None,
                "target_symbol": None,
                "resolution_status": "unresolved",
                "reason": "unknown_symbol"
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

    print(json.dumps(repo_index, indent=3))

    return repo_index

repo_map('./demo')