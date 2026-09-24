import ast
import libcst as cst
from libcst.display import dump
from libcst.metadata import VisitorMetadataProvider, MetadataWrapper, PositionProvider
from pathlib import Path
from libcst import matchers as m
import json
import jedi
from typing import Sequence


class Tracker(VisitorMetadataProvider):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, code_text: str):
        super().__init__()
        self.code_text = code_text
        self.code_lines = code_text.splitlines()
        self.jedi_script = jedi.Script(code_text)
        
        # State Registries
        self.imports = {}    # local_alias -> package
        self.instances = {}  # instance_var -> {"class_alias": ..., "statement": ...}
        self.results = []

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

        # Safely extract target variable name (e.g., "client")
        for target_node in node.targets:
            if isinstance(target_node.target, cst.Name):
                var_name = target_node.target.value
                
                # Extract instantiated class name (e.g., "OpenAI")
                if isinstance(node.value, cst.Call):
                    func_code = cst.Module([]).code_for_node(node.value.func).strip()
                    self.instances[var_name] = {
                        "class_alias": func_code,
                        "statement": stmt_text
                    }

    def visit_Call(self, node: cst.Call) -> None:
        # 1. Guard: Only track method calls (e.g. client.chat.create), ignore bare calls (OpenAI())
        if not isinstance(node.func, cst.Attribute):
            return

        pos = self.get_metadata(PositionProvider, node)
        line = pos.start.line
        col = pos.start.column

        call_expr = cst.Module([]).code_for_node(node.func).strip()

        # 2. Extract Kwargs
        kwargs = {}
        for arg in node.args:
            if arg.keyword:
                key = arg.keyword.value
                val = cst.Module([]).code_for_node(arg.value).strip()
                kwargs[key] = val

        # 3. Find root base caller ("client" from "client.chat.completions.create")
        curr = node.func
        while isinstance(curr, cst.Attribute):
            curr = curr.value
        base_caller = curr.value if isinstance(curr, cst.Name) else None

        package = None
        imported_name = None
        local_alias = None
        instance_stmt = None

        # PATH A: Local LibCST Registry Lookup (Fast & Independent of environment)
        if base_caller and base_caller in self.instances:
            inst_info = self.instances[base_caller]
            instance_stmt = inst_info["statement"]
            cls_alias = inst_info["class_alias"]
            
            if cls_alias in self.imports:
                imp_info = self.imports[cls_alias]
                package = imp_info["package"]
                imported_name = imp_info["imported_name"]
                local_alias = imp_info["alias"]

        # PATH B: Direct Module Import (e.g., openai.ChatCompletion.create)
        elif base_caller and base_caller in self.imports:
            imp_info = self.imports[base_caller]
            package = imp_info["package"]
            imported_name = imp_info["imported_name"]
            local_alias = imp_info["alias"]

        # PATH C: Jedi Fallback (Only if LibCST state missed)
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

        # If resolved successfully, record result!
        if package:
            self.results.append({
                "package": package,
                "imported": imported_name,
                "local_alias": local_alias,
                "instance": instance_stmt,
                "api_usage": call_expr,
                "line": line,
                "kwargs": kwargs
            })
             


def parser():

    path = Path("sample.py")

    code_text = path.read_text(encoding="utf-8")

    module = cst.parse_module(code_text)

# print(dump(module))

    visitor = Tracker(code_text)

    wrapper = MetadataWrapper(module)
    wrapper.visit(visitor)




    # print(json.dumps(visitor.results, indent=3))

    return visitor.results


# parser()





