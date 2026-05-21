"""Walk the iROAM repo and emit modules.json for the doc renderer.

Pure stdlib. No third-party deps. Re-run with `python docs/_build/extract.py`.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "docs" / "_build" / "modules.json"

PACKAGES: list[dict[str, Any]] = [
    {
        "id": "apps-collector",
        "name": "apps/collector",
        "path": "apps/collector",
        "label": "Collector",
        "summary": "GTFS-Realtime acquisition: fetch, parse, normalize, persist.",
        "entry_points": ["python -m apps.collector.main --once", "python -m apps.collector.main --loop"],
        "owns_tables": ["feed_fetch_logs", "raw_gtfsrt_snapshots", "vehicle_positions"],
    },
    {
        "id": "apps-analytics",
        "name": "apps/analytics",
        "path": "apps/analytics",
        "label": "Analytics",
        "summary": "GTFS-aware trajectory reconstruction and anomaly inputs.",
        "entry_points": [
            "python -m apps.analytics.main --date YYYY-MM-DD [--route R] [--export-csv DIR]",
            "python -m apps.analytics.worker",
        ],
        "owns_tables": ["analytics_runs", "trip_trajectories"],
    },
    {
        "id": "apps-api",
        "name": "apps/api",
        "path": "apps/api",
        "label": "API",
        "summary": "FastAPI read-only REST surface; vehicle, route, trajectory, and forecast endpoints.",
        "entry_points": ["uvicorn apps.api.main:app --host 0.0.0.0 --port 8000"],
        "owns_tables": [],
    },
    {
        "id": "apps-dashboard",
        "name": "apps/dashboard",
        "path": "apps/dashboard",
        "label": "Dashboard",
        "summary": "Streamlit multipage operator UI; talks only to the API, never the DB directly.",
        "entry_points": ["streamlit run apps/dashboard/Home.py"],
        "owns_tables": [],
    },
    {
        "id": "core",
        "name": "core",
        "path": "core",
        "label": "Core",
        "summary": "Settings, logging, time helpers, and GTFS-RT enum constants shared by every app.",
        "entry_points": [],
        "owns_tables": [],
    },
    {
        "id": "db",
        "name": "db",
        "path": "db",
        "label": "Database",
        "summary": "SQLAlchemy 2.x ORM models, query helpers, and session factory.",
        "entry_points": [],
        "owns_tables": [
            "feed_fetch_logs",
            "raw_gtfsrt_snapshots",
            "vehicle_positions",
            "analytics_runs",
            "trip_trajectories",
        ],
    },
    {
        "id": "scripts",
        "name": "scripts",
        "path": "scripts",
        "label": "Scripts",
        "summary": "Operational CLIs: capture fixture, reset DB, run migrations.",
        "entry_points": [
            "python -m scripts.capture_sample",
            "python -m scripts.db_reset [--yes-i-am-sure]",
            "python -m scripts.run_migrations",
        ],
        "owns_tables": [],
    },
    {
        "id": "deployment-bunching",
        "name": "deployment/bunching_lightgbm",
        "path": "deployment/bunching_lightgbm",
        "label": "Bunching predictor",
        "summary": "LightGBM model bundle (30 per-horizon boosters) and retraining glue served by the API.",
        "entry_points": [],
        "owns_tables": [],
    },
    {
        "id": "legacy",
        "name": "data_process/arch_legacy",
        "path": "data_process/arch_legacy",
        "label": "Legacy",
        "summary": "Archived proof-of-concept pipeline. Not used by the live system. Kept for reference.",
        "entry_points": [],
        "owns_tables": [],
        "archived": True,
    },
]


SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", "node_modules", "versions"}


def find_py_files(root: Path) -> list[Path]:
    """Return .py files under root, excluding caches and Alembic versions/."""
    files: list[Path] = []
    if not root.exists():
        return files
    for p in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        files.append(p)
    return files


def first_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.strip().splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def render_annotation(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return "?"


def render_default(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return "?"


def render_signature(name: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = fn.args
    parts: list[str] = []

    # positional-only
    posonly = list(args.posonlyargs)
    regular = list(args.args)
    all_pos = posonly + regular
    defaults = list(args.defaults)
    pad = [None] * (len(all_pos) - len(defaults)) + defaults

    for i, a in enumerate(posonly):
        ann = render_annotation(a.annotation)
        s = a.arg + (f": {ann}" if ann else "")
        d = pad[i]
        if d is not None:
            s += f" = {render_default(d)}"
        parts.append(s)
    if posonly:
        parts.append("/")
    for i, a in enumerate(regular):
        ann = render_annotation(a.annotation)
        s = a.arg + (f": {ann}" if ann else "")
        d = pad[len(posonly) + i]
        if d is not None:
            s += f" = {render_default(d)}"
        parts.append(s)
    if args.vararg:
        ann = render_annotation(args.vararg.annotation)
        parts.append("*" + args.vararg.arg + (f": {ann}" if ann else ""))
    elif args.kwonlyargs:
        parts.append("*")
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        ann = render_annotation(a.annotation)
        s = a.arg + (f": {ann}" if ann else "")
        if d is not None:
            s += f" = {render_default(d)}"
        parts.append(s)
    if args.kwarg:
        ann = render_annotation(args.kwarg.annotation)
        parts.append("**" + args.kwarg.arg + (f": {ann}" if ann else ""))

    ret = render_annotation(fn.returns)
    prefix = "async def " if isinstance(fn, ast.AsyncFunctionDef) else "def "
    suffix = f" -> {ret}" if ret else ""
    return f"{prefix}{name}({', '.join(parts)}){suffix}"


def render_class_signature(name: str, cls: ast.ClassDef) -> str:
    bases = [render_annotation(b) for b in cls.bases]
    decorators = [f"@{render_annotation(d)}" for d in cls.decorator_list]
    head = "class " + name
    if bases:
        head += "(" + ", ".join(bases) + ")"
    if decorators:
        return "\n".join(decorators + [head])
    return head


def is_public(name: str) -> bool:
    return not name.startswith("_") or name == "__init__"


def extract_methods(cls: ast.ClassDef) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_") and node.name != "__init__":
                continue
            sig = render_signature(node.name, node)
            out.append(
                {
                    "name": node.name,
                    "signature": sig,
                    "docstring": ast.get_docstring(node) or "",
                }
            )
    return out


def extract_file(path: Path) -> dict[str, Any]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        print(f"SKIP {path}: {e}", file=sys.stderr)
        return {
            "path": str(path.relative_to(REPO)),
            "docstring": "",
            "summary": "(file failed to parse)",
            "symbols": [],
            "imports": [],
        }

    module_doc = ast.get_docstring(tree) or ""
    symbols: list[dict[str, Any]] = []
    imports: list[str] = []

    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imports.append(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not is_public(node.name):
                continue
            symbols.append(
                {
                    "kind": "function",
                    "name": node.name,
                    "signature": render_signature(node.name, node),
                    "docstring": ast.get_docstring(node) or "",
                    "decorators": [render_annotation(d) for d in node.decorator_list],
                }
            )
        elif isinstance(node, ast.ClassDef):
            if not is_public(node.name):
                continue
            symbols.append(
                {
                    "kind": "class",
                    "name": node.name,
                    "signature": render_class_signature(node.name, node),
                    "docstring": ast.get_docstring(node) or "",
                    "decorators": [render_annotation(d) for d in node.decorator_list],
                    "methods": extract_methods(node),
                }
            )

    return {
        "path": str(path.relative_to(REPO)),
        "docstring": module_doc,
        "summary": first_line(module_doc),
        "symbols": symbols,
        "imports": sorted(set(imports)),
    }


def build_imports_index(packages: list[dict[str, Any]]) -> dict[str, list[str]]:
    """For every (module, symbol) defined in the project, list the files that
    `from module import symbol` or reference `module.symbol`."""
    defined: dict[str, list[str]] = {}
    for pkg in packages:
        for f in pkg["files"]:
            mod = f["path"].removesuffix(".py").replace("/", ".")
            for sym in f["symbols"]:
                key = f"{mod}.{sym['name']}"
                defined.setdefault(key, [])

    for pkg in packages:
        for f in pkg["files"]:
            for imp in f["imports"]:
                if imp in defined and f["path"] not in defined[imp]:
                    defined[imp].append(f["path"])
    return defined


def main() -> int:
    out: dict[str, Any] = {"packages": []}
    for pkg_meta in PACKAGES:
        pkg_root = REPO / pkg_meta["path"]
        files = find_py_files(pkg_root)
        file_records = []
        for fp in files:
            # Skip Alembic-generated version files; mention the directory in db/ page.
            if "migrations/versions" in str(fp):
                continue
            file_records.append(extract_file(fp))
        pkg = dict(pkg_meta)
        pkg["files"] = file_records
        out["packages"].append(pkg)

    out["imports_index"] = build_imports_index(out["packages"])

    # Collect migration filenames (don't extract code from them).
    mig_dir = REPO / "db" / "migrations" / "versions"
    out["migrations"] = sorted(p.name for p in mig_dir.glob("*.py")) if mig_dir.exists() else []

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    total_files = sum(len(p["files"]) for p in out["packages"])
    total_symbols = sum(len(f["symbols"]) for p in out["packages"] for f in p["files"])
    print(f"Wrote {OUT.relative_to(REPO)}: {len(out['packages'])} packages, "
          f"{total_files} files, {total_symbols} symbols")
    return 0


if __name__ == "__main__":
    sys.exit(main())
