"""Static import closure of an image's entry points against its build context.

Follows every import (also inside functions, so lazy imports count) from the entry modules through
the packages copied into the context, and reports any module that is neither in the context's
src/, the installed requirements nor the standard library. A missing one is a Pod that starts and
then dies (#546: bravo_apply was imported by the workflow host but not copied into its image).

    python3 scripts/image_import_closure.py --src CONTEXT/src --site SITE ENTRY [ENTRY ...]
"""

import argparse
import ast
import json
import sys
from pathlib import Path

OWN = ("bravo_", "rehearsal_runner")


def module_file(src, name):
    base = src.joinpath(*name.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


def imports(path, name):
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                module = base + ("." + node.module if node.module else "")
            else:
                module = node.module
            yield module, node.lineno
            for alias in node.names:
                yield module + "." + alias.name, node.lineno  # submodule or attribute


def closure(src, available, entries):
    # Only our packages are followed; a third-party package's own optional imports are its business.
    internal = {p.name for p in src.iterdir() if p.name.startswith(OWN)}
    seen, missing, todo = set(), {}, list(entries)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        path = module_file(src, name)
        if path is None:
            continue
        for target, line in imports(path, name):
            top = target.split(".")[0]
            if top in internal:
                if module_file(src, target) is not None:
                    todo.append(target)
                elif module_file(src, target.rpartition(".")[0]) is None and "." in target:
                    parent = target.rpartition(".")[0]
                    if module_file(src, parent.rpartition(".")[0] or parent) is None:
                        missing.setdefault(target, f"{path.relative_to(src)}:{line}")
            elif top not in available and top not in sys.stdlib_module_names:
                missing.setdefault(top, f"{path.relative_to(src)}:{line}")
    return sorted(seen), missing


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--src", type=Path, required=True)
    p.add_argument("--site", type=Path, required=True)
    p.add_argument("--allow", action="append", default=[], help="top-level module knowingly absent")
    p.add_argument("entries", nargs="+")
    a = p.parse_args()
    available = {x.name.split(".")[0] for x in a.site.iterdir()} | {x.name for x in a.src.iterdir()}
    seen, missing = closure(a.src, available, a.entries)
    missing = {k: v for k, v in missing.items() if k.split(".")[0] not in a.allow}
    print(json.dumps({"modules": len(seen), "missing": missing}, indent=1))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
