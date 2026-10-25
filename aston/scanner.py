import ast
import libcst as cst
from libcst.display import dump
from libcst.metadata import VisitorMetadataProvider, MetadataWrapper, PositionProvider, ParentNodeProvider
from pathlib import Path
from libcst import matchers as m
import json
import jedi
from typing import Sequence
import hashlib

class Tracker(VisitorMetadataProvider):
    METADATA_DEPENDENCIES = (PositionProvider, ParentNodeProvider)

    def __init__(self, relative_path: str, code_text: str, file_path: Path | None = None):
        super().__init__()
        self.relative_path = relative_path
        self.code_text = code_text
        self.code_lines = code_text.splitlines()
        self.file_path = file_path
        
        self._jedi_script = None  # Lazy-loaded Jedi instance
        
        self.usages = []
        self.symbols = {}
        self.imports = []
        self.scope_stack = []
        
        # Local state registries for LibCST resolution
        self.local_imports = {}
        self.instances = {}

    @property
    def jedi_script(self):
        """Lazy load Jedi only when a fallback is actually triggered."""
        if self._jedi_script is None:
            if self.file_path and self.file_path.exists():
                self._jedi_script = jedi.Script(path=str(self.file_path))
            else:
                self._jedi_script = jedi.Script(self.code_text)
        return self._jedi_script

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if node.module and not isinstance(node.names, cst.ImportStar):
            pkg = cst.Module([]).code_for_node(node.module).strip()
            for alias in node.names:
                imported_name = alias.name.value
                local_alias = alias.asname.name.value if alias.asname else imported_name
                self.local_imports[local_alias] = {"package": pkg, "imported": imported_name}
                
                self.imports.append({
                    "module": pkg,
                    "imported_name": imported_name,
                    "alias": alias.asname.name.value if alias.asname else None,
                    "line": self.get_metadata(PositionProvider, node).start.line,
                    "import_type": "from_import"
                })

    def visit_Import(self, node: cst.Import) -> None:
        for alias in node.names:
            pkg = cst.Module([]).code_for_node(alias.name).strip()
            local_alias = alias.asname.name.value if alias.asname else pkg
            self.local_imports[local_alias] = {"package": pkg, "imported": pkg}
            
            self.imports.append({
                "module": pkg,
                "imported_name": None,
                "alias": alias.asname.name.value if alias.asname else None,
                "line": self.get_metadata(PositionProvider, node).start.line,
                "import_type": "module"
            })

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        func_name = node.name.value
        pos = self.get_metadata(PositionProvider, node)
        params = [param.name.value for param in node.params.params]
        
        symbol_key = f"{self.relative_path}::{func_name}"
        self.symbols[symbol_key] = {
            "file": self.relative_path,
            "qualified_name": func_name,
            "kind": "function",
            "line_start": pos.start.line,
            "line_end": pos.end.line,
            "parameters": params,
            "reach": "unknown",  # Updated from "unreached" to "unknown"
            "watched_usage_ids": []
        }
        self.scope_stack.append(func_name)

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        if self.scope_stack:
            self.scope_stack.pop()

    def _get_result_use(self, node: cst.Call) -> str:
        current = node
        while True:
            parent = self.get_metadata(ParentNodeProvider, current)
            if parent is None:
                return "discarded"
            if isinstance(parent, cst.Return):
                return "returned"
            if isinstance(parent, cst.Assign):
                return "assigned"
            if isinstance(parent, cst.Arg):
                return "argument"
            if isinstance(parent, cst.Expr):
                return "discarded"
            current = parent

    def visit_Call(self, node: cst.Call) -> None:
        if not isinstance(node.func, cst.Attribute):
            return

        pos = self.get_metadata(PositionProvider, node)
        line = pos.start.line
        col = pos.start.column
        
        call_expr = cst.Module([]).code_for_node(node.func).strip()
        root_name = call_expr.split(".")[0]

        package = None
        imported = None

        # 1. Try LibCST static import resolution first
        if root_name in self.local_imports:
            package = self.local_imports[root_name]["package"]
            imported = self.local_imports[root_name]["imported"]

        # 2. Jedi Fallback if LibCST state missed the import/alias
        if not package:
            try:
                defs = self.jedi_script.goto(line=line, column=col) or self.jedi_script.infer(line=line, column=col)
                if defs:
                    target = defs[0]
                    if target.module_name and target.module_name not in ("builtins", "__main__"):
                        package = target.module_name
                        imported = target.name
            except Exception:
                pass  # Fail gracefully if Jedi cannot resolve

        # Only record if we successfully identified it as an OpenAI or targeted package usage
        if package and ("openai" in package or "openai" in call_expr):
            usage_id = f"{self.relative_path}:{line}:{col}"
            enclosing_symbol = self.scope_stack[-1] if self.scope_stack else None
            result_use = self._get_result_use(node)

            # Extract kwargs
            kwargs = {}
            for arg in node.args:
                if arg.keyword:
                    kwargs[arg.keyword.value] = cst.Module([]).code_for_node(arg.value).strip()

            self.usages.append({
                "id": usage_id,
                "package": package,
                "imported": imported,
                "local_alias": root_name,
                "api_usage": call_expr,
                "result_use": result_use,
                "enclosing_symbol": enclosing_symbol,
                "line": line,
                "column": col,
                "end_line": pos.end.line,
                "end_column": pos.end.column,
                "usage_kind": "call",
                "kwargs": kwargs
            })
             


def parser(path: str | Path, relative_path: str, project: jedi.Project | None = None):

    path = Path(path)
    if not path.exists():
        path = Path("before.py")

    code_text = path.read_text(encoding="utf-8")
    file_hash = hashlib.sha256(code_text.encode("utf-8")).hexdigest()
    
    module = cst.parse_module(code_text)
    visitor = Tracker(relative_path=relative_path, code_text=code_text, file_path=path)
    wrapper = MetadataWrapper(module)
    wrapper.visit(visitor)

    return {
        "file_meta": {
            "path": relative_path,
            "sha256": file_hash,
            "parse_status": "success"
        },
        "usages": visitor.usages,
        "imports": visitor.imports,
        "symbols": visitor.symbols,
        "diagnostics": [] # File-specific diagnostics if any
    }

# parser()