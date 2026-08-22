"""AST-level internal import checker for a split project.

Usage: python check_imports.py <project_root> <package_name>
  e.g. python check_imports.py split/omnigraph omnigraph
       python check_imports.py split/radai backend

Verifies every `import pkg.*` / `from pkg.* import ...` resolves to a real
module file inside <project_root>/<package_name>. External dependencies are
ignored. Exit code 1 when unresolved imports remain.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def resolve(module: str, pkg_root: Path, pkg_name: str) -> bool:
    rel = module[len(pkg_name):].lstrip(".").replace(".", "/")
    candidates = [
        pkg_root / (rel + ".py"),
        pkg_root / rel / "__init__.py",
        pkg_root / rel,  # namespace package (dir without __init__)
    ]
    return any(c.exists() for c in candidates)


def main() -> int:
    project = Path(sys.argv[1]).resolve()
    pkg_name = sys.argv[2]
    pkg_root = project / pkg_name
    allowlist: set[str] = set()
    allow_file = project / "tools" / "import_allowlist.txt"
    if allow_file.exists():
        for line in allow_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                allowlist.add(line)
    problems: list[str] = []
    files = sorted(
        list(pkg_root.rglob("*.py"))
        + list((project / "scripts").rglob("*.py"))
        + list((project / "tests").rglob("*.py"))
    )
    for py in files:
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError as exc:
            if "f-string" in str(exc.msg) or "unmatched" in str(exc.msg):
                # Python 3.12+ nested-quote f-strings: valid on the target
                # runtime (3.14), unparsable by this host's older interpreter.
                problems.append(f"SKIPPED-PY312 {py.relative_to(project)}: {exc}")
            else:
                problems.append(f"SYNTAX {py.relative_to(project)}: {exc}")
            continue
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == pkg_name or alias.name.startswith(pkg_name + "."):
                        modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.module:
                    # relative import: resolve against file location
                    base = py.parent
                    for _ in range(node.level - 1):
                        base = base.parent
                    mod = ".".join(
                        base.relative_to(project).parts
                        + tuple(node.module.split(".") if node.module else ())
                    )
                    if mod.startswith(pkg_name + "."):
                        modules.add(mod)
                elif node.module and (node.module == pkg_name or node.module.startswith(pkg_name + ".")):
                    modules.add(node.module)
        for mod in sorted(modules):
            if mod not in allowlist and not resolve(mod, pkg_root, pkg_name):
                problems.append(f"UNRESOLVED {py.relative_to(project)}: {mod}")

    if problems:
        print(f"\n{len(problems)} problem(s) in {project.name}:")
        for line in problems:
            print("  " + line)
        return 1
    print(f"OK: all internal imports resolve in {project.name} ({len(files)} files checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
