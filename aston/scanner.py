import ast
import libcst as cst
from libcst.display import dump
from libcst.metadata import VisitorMetadataProvider, MetadataWrapper, PositionProvider, ParentNodeProvider
from pathlib import Path
from libcst import matchers as m
import json
import jedi
from typing import Sequence


class Tracker(VisitorMetadataProvider):
    METADATA_DEPENDENCIES = (PositionProvider, ParentNodeProvider)

    def __init__(self, code_text: str, file_path: Path | None = None, project: jedi.Project | None = None):
        super().__init__()
        self.code_text = code_text
        self.code_lines = code_text.splitlines()
        self.file_path = file_path
        self.project = project
        self._jedi_script = None  # Lazy loaded
        
        self.imports = {}    
        self.instances = {}  
        self.results = []
        self._recorded_usages = set()

    @property
    def jedi_script(self):
        if self._jedi_script is None:
            self._jedi_script = jedi.Script(self.code_text)
        return self._jedi_script

    def _get_root_name(self, node) -> str | None:
        curr = node
        while isinstance(curr, cst.Attribute):
            curr = curr.value
        if isinstance(curr, cst.Name):
            return curr.value
        return None

    def _resolve_package(self, root_name: str):
        package = None
        imported_name = None
        local_alias = None
        instance_stmt = None

        if root_name in self.instances:
            inst_info = self.instances[root_name]
            instance_stmt = inst_info["statement"]
            cls_alias = inst_info["class_alias"]
            if cls_alias in self.imports:
                imp_info = self.imports[cls_alias]
                package = imp_info["package"]
                imported_name = imp_info["imported_name"]
                local_alias = imp_info["alias"]
        elif root_name in self.imports:
            imp_info = self.imports[root_name]
            package = imp_info["package"]
            imported_name = imp_info["imported_name"]
            local_alias = imp_info["alias"]
        
        return package, imported_name, local_alias, instance_stmt

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if node.module and not isinstance(node.names, cst.ImportStar):
            pkg = cst.Module([]).code_for_node(node.module).strip()
            for alias in node.names:
                imported_name = alias.name.value
                local_alias = alias.asname.name.value if alias.asname else imported_name
                self.imports[local_alias] = {
                    "package": pkg,
                    "imported_name": imported_name,
                    "alias": local_alias
                }

    def visit_Import(self, node: cst.Import) -> None:
        for alias in node.names:
            imported_name = cst.Module([]).code_for_node(alias.name).strip()
            local_alias = alias.asname.name.value if alias.asname else imported_name
            self.imports[local_alias] = {
                "package": imported_name,
                "imported_name": imported_name,
                "alias": local_alias
            }

    def visit_Assign(self, node: cst.Assign) -> None:
        pos = self.get_metadata(PositionProvider, node)
        stmt_text = self.code_lines[pos.start.line - 1].strip()

        for target_node in node.targets:
            if isinstance(target_node.target, cst.Name):
                var_name = target_node.target.value
                if isinstance(node.value, cst.Call):
                    func_code = cst.Module([]).code_for_node(node.value.func).strip()
                    self.instances[var_name] = {
                        "class_alias": func_code,
                        "statement": stmt_text
                    }

    def visit_Call(self, node: cst.Call) -> None:
        if not isinstance(node.func, cst.Attribute):
            return

        pos = self.get_metadata(PositionProvider, node)
        line = pos.start.line
        col = pos.start.column

        call_expr = cst.Module([]).code_for_node(node.func).strip()

        kwargs = {}
        for arg in node.args:
            if arg.keyword:
                key = arg.keyword.value
                val = cst.Module([]).code_for_node(arg.value).strip()
                kwargs[key] = val

        root_name = self._get_root_name(node.func)
        package, imported_name, local_alias, instance_stmt = self._resolve_package(root_name) if root_name else (None, None, None, None)

        # Jedi Fallback if LibCST state missed
        if not package:
            defs = self.jedi_script.goto(line=line, column=col) or self.jedi_script.infer(line=line, column=col)
            if defs:
                target = defs[0]
                if target.module_name not in ("builtins", "__main__"):
                    package = target.module_name
                    imported_name = target.name
                    local_alias = target.name
                    if target.line and target.line <= len(self.code_lines):
                        instance_stmt = self.code_lines[target.line - 1].strip()

        if package:
            usage_key = (line, call_expr)
            if usage_key not in self._recorded_usages:
                self._recorded_usages.add(usage_key)
                self.results.append({
                    "package": package,
                    "imported": imported_name,
                    "local_alias": local_alias,
                    "instance": instance_stmt,
                    "api_usage": call_expr,
                    "line": line,
                    "kwargs": kwargs
                })

    def _attribute_belongs_to_call(self, node) -> bool:
        current = node
        while True:
            parent = self.get_metadata(ParentNodeProvider, current)
            if isinstance(parent, cst.Attribute):
                current = parent
                continue
            if isinstance(parent, cst.Call) and parent.func is current:
                return True
            return False

    def visit_Attribute(self, node: cst.Attribute) -> None:
        """
        Track non-call API references while filtering out intermediate call attributes.
        """
        if self._attribute_belongs_to_call(node):
            return

        parent = self.get_metadata(ParentNodeProvider, node)
        if isinstance(parent, cst.Attribute):
            return

        api_usage = cst.Module([]).code_for_node(node).strip()
        root_name = self._get_root_name(node)

        if not root_name:
            return

        package, imported_name, local_alias, instance_stmt = self._resolve_package(root_name)
        if not package:
            return

        pos = self.get_metadata(PositionProvider, node)
        line = pos.start.line
        usage_key = (line, api_usage)

        if usage_key not in self._recorded_usages:
            self._recorded_usages.add(usage_key)
            self.results.append({
                "package": package,
                "imported": imported_name,
                "local_alias": local_alias,
                "instance": instance_stmt,
                "api_usage": api_usage,
                "line": line,
                "kwargs": {}
            })
             


def parser(path: str | Path, project: jedi.Project | None = None):
    path = Path(path)
    if not path.exists():
        path = Path("before.py")

    code_text = path.read_text(encoding="utf-8")
    module = cst.parse_module(code_text)

    visitor = Tracker(code_text, file_path=path, project=project)
    wrapper = MetadataWrapper(module)
    wrapper.visit(visitor)

    return visitor.results


# parser()