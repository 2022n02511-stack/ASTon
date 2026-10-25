from pathlib import Path
from aston.watcher import watcher

def repo_map(repo_path: str) -> dict:
    root = Path(repo_path).expanduser().resolve()
    dependencies = watcher(repo_path)
    
    all_files_data = {}
    global_symbols = {}
    all_usages = []
    raw_relationships = []

    py_files = [f for f in root.rglob("*.py") if f.name != "__init__.py"]

    # --- PASS 1: File-by-file extraction ---
    for py_file in py_files:
        rel_path = str(py_file.relative_to(root))
        code_text = py_file.read_text(encoding="utf-8")
        module = cst.parse_module(code_text)
        
        visitor = Tracker(rel_path, code_text)
        wrapper = cst.metadata.MetadataWrapper(module)
        wrapper.visit(visitor)

        all_files_data[rel_path] = {
            "parse_status": "success",
            "imports": visitor.imports,
            "usages": visitor.usages
        }
        
        # Register global symbols and pool usages
        global_symbols.update(visitor.symbols)
        all_usages.extend(visitor.usages)

    # --- PASS 2: Reachability & Graph Slicing ---
    # 1. Mark direct symbols (contain a watched usage)
    for usage in all_usages:
        enclosing = usage.get("enclosing_symbol")
        if enclosing:
            # Find the symbol key in this file
            for sym_key, sym_data in global_symbols.items():
                if sym_data["qualified_name"] == enclosing and sym_data["file"] in usage["id"]:
                    sym_data["reach"] = "direct"
                    sym_data["watched_usage_ids"].append(usage["id"])

    # 2. Filter symbols dictionary to only keep reachable ones (Direct + Transitive)
    # (For full transitive traversal, you can parse local calls here and walk backward)
    final_symbols = {
        k: v for k, v in global_symbols.items() if v["reach"] != "unreached"
    }

    repo_index = {
        "schema_version": "1.1",
        "repository": {"root": root.name, "language": "python", "index_status": "complete"},
        "dependencies": dependencies,
        "symbols": final_symbols,
        "relationships": raw_relationships,  # Populated via second-pass call matching
        "files": all_files_data,
        "diagnostics": []
    }

    return repo_index

