"""Build and verify a compressed, single-file rtpmonIPO executable.

Usage:

    uv run python scripts/build_single_file.py [options]
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import subprocess
import sys
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "rtpmonIPO"
MODULE_ORDER = (
    "startup",
    "models",
    "protocol",
    "errors",
    "client",
    "db",
    "cli",
)
EXCLUDED_MODULES = {"__init__", "__main__"}
BUNDLE_NAME = "rtpmonIPO.py"
FLAT_NAME = "rtpmonIPO.flat.py"


class BuildError(Exception):
    """A single-file bundle build or verification failure."""


def _module_path(name: str) -> Path:
    return PACKAGE_DIR / f"{name}.py"


def _module_source(name: str) -> str:
    path = _module_path(name)
    if not path.is_file():
        raise BuildError(f"missing module file: {path}")
    return path.read_text(encoding="utf-8")


def _module_tree(name: str) -> ast.Module:
    try:
        return ast.parse(_module_source(name), filename=str(_module_path(name)))
    except SyntaxError as error:
        raise BuildError(
            f"cannot parse {_module_path(name)}: {error}"
        ) from error


def _check_module_order() -> None:
    actual = {
        path.stem
        for path in PACKAGE_DIR.glob("*.py")
        if path.stem not in EXCLUDED_MODULES
    }
    listed = set(MODULE_ORDER)
    if actual != listed:
        missing = sorted(actual - listed)
        extra = sorted(listed - actual)
        raise BuildError(
            f"MODULE_ORDER does not match package modules: "
            f"missing={missing}, extra={extra}"
        )


def _relative_imports(tree: ast.Module) -> set[str]:
    imports: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.level == 0:
            continue
        if node.module is None:
            imports.update(
                alias.name.split(".")[0]
                for alias in node.names
                if alias.name != "__version__"
            )
        else:
            imports.add(node.module.split(".")[0])
    return imports


def _check_dependencies() -> None:
    positions = {name: index for index, name in enumerate(MODULE_ORDER)}
    for name in MODULE_ORDER:
        for dependency in _relative_imports(_module_tree(name)):
            if dependency not in positions:
                raise BuildError(
                    f"rtpmonIPO.{name} imports unknown module {dependency!r}"
                )
            if positions[dependency] >= positions[name]:
                raise BuildError(
                    f"rtpmonIPO.{name} must appear after rtpmonIPO.{dependency}"
                )


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ):
            names.add(node.target.id)
    return names


def _check_name_collisions() -> None:
    owners: dict[str, str] = {}
    for module in MODULE_ORDER:
        for name in _top_level_names(_module_tree(module)):
            if name in owners:
                raise BuildError(
                    f"flat namespace collision: {name!r} appears in "
                    f"rtpmonIPO.{owners[name]} and rtpmonIPO.{module}"
                )
            owners[name] = module


def _strip_package_imports(name: str) -> str:
    source = _module_source(name)
    tree = _module_tree(name)
    removed_lines: set[int] = set()
    for node in tree.body:
        is_package_import = isinstance(node, ast.ImportFrom) and node.level > 0
        is_future_import = (
            isinstance(node, ast.ImportFrom) and node.module == "__future__"
        )
        if is_package_import or is_future_import:
            end = node.end_lineno or node.lineno
            removed_lines.update(range(node.lineno, end + 1))

    lines: list[str] = source.splitlines()
    kept: list[str] = [
        line
        for number, line in enumerate(lines, start=1)
        if number not in removed_lines
    ]
    return "\n".join(kept).strip() + "\n"


def _version() -> str:
    tree = ast.parse(_module_source("__init__"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(
            node.value.value, str
        ):
            return node.value.value
    raise BuildError("rtpmonIPO.__version__ is missing or is not a string")


def assemble_flat_source() -> str:
    """Return all runtime modules as one source file."""
    header: list[str] = [
        '"""Generated rtpmonIPO single-file source; do not edit."""',
        "from __future__ import annotations",
        f'__version__ = "{_version()}"',
    ]
    chunks: list[str] = ["\n".join(header)]
    for name in MODULE_ORDER:
        banner = f"# {'-' * 68}\n# rtpmonIPO.{name}\n# {'-' * 68}"
        chunks.append(f"{banner}\n{_strip_package_imports(name).rstrip()}")
    chunks.append('if __name__ == "__main__":\n    raise SystemExit(main())')
    return "\n\n\n".join(chunks) + "\n"


def wrap_launcher(flat_source: str, shebang: str | None) -> str:
    """Compress *flat_source* and return a Python launcher for it."""
    encoded = base64.b64encode(
        zlib.compress(flat_source.encode(), level=9)
    ).decode()
    payload = "\n".join(
        encoded[index : index + 76] for index in range(0, len(encoded), 76)
    )
    lines: list[str] = []
    if shebang:
        lines.append(f"#!{shebang}")
    lines.extend(
        [
            (
                '"""Generated rtpmonIPO bundle; requires aiohttp.\n\n'
                'Do not edit."""'
            ),
            "import base64",
            "import zlib",
            "",
            'PAYLOAD = """\\',
            payload,
            '"""',
            "",
            "",
            "def main() -> None:",
            "    source = zlib.decompress(base64.b64decode(PAYLOAD)).decode()",
            '    namespace = {"__name__": "__main__", "__file__": __file__}',
            '    code = compile(source, "<rtpmonIPO bundle>", "exec")',
            "    exec(code, namespace)",
            "",
            "",
            'if __name__ == "__main__":',
            "    main()",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_readme(output_dir: Path, shebang: str | None) -> None:
    interpreter = shebang or "python3"
    readme = (
        "# rtpmonIPO single-file bundle\n\n"
        "Run the bundle with Python 3.11+ in an environment that provides "
        "aiohttp (and aiosqlite when using --db):\n\n"
        f"```bash\n{interpreter} {BUNDLE_NAME} --help\n```\n"
    )
    (output_dir / "README.md").write_text(readme, encoding="utf-8")


def _verify_bundle(bundle_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(bundle_path), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise BuildError(f"bundle verification failed:\n{result.stderr}")


def build(
    output_dir: Path,
    *,
    keep_flat: bool,
    shebang: str | None,
    with_readme: bool,
) -> Path:
    """Build, compile, and execute-check a single-file bundle."""
    _check_module_order()
    _check_dependencies()
    _check_name_collisions()

    flat_source = assemble_flat_source()
    compile(flat_source, "<rtpmonIPO flat source>", "exec")
    launcher = wrap_launcher(flat_source, shebang)
    compile(launcher, "<rtpmonIPO bundle>", "exec")

    if (
        hashlib.sha256(launcher.encode()).digest()
        != hashlib.sha256(wrap_launcher(flat_source, shebang).encode()).digest()
    ):
        raise BuildError("bundle output is not deterministic")

    output_dir.mkdir(parents=True, exist_ok=True)
    if keep_flat:
        (output_dir / FLAT_NAME).write_text(flat_source, encoding="utf-8")
    bundle_path = output_dir / BUNDLE_NAME
    bundle_path.write_text(launcher, encoding="utf-8")
    if shebang:
        bundle_path.chmod(bundle_path.stat().st_mode | 0o111)
    if with_readme:
        _write_readme(output_dir, shebang)
    _verify_bundle(bundle_path)
    return bundle_path


def main(argv: list[str] | None = None) -> int:
    """Build a single-file rtpmonIPO executable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "dist",
        help="directory for generated artifacts (default: dist)",
    )
    parser.add_argument(
        "--keep-flat",
        action="store_true",
        help="also write the readable, uncompressed flat source",
    )
    parser.add_argument(
        "--shebang",
        help="interpreter path to write in the bundle's first line",
    )
    parser.add_argument(
        "--with-readme",
        action="store_true",
        help="write deployment instructions alongside the bundle",
    )
    arguments = parser.parse_args(argv)
    try:
        bundle_path = build(
            arguments.output_dir,
            keep_flat=arguments.keep_flat,
            shebang=arguments.shebang,
            with_readme=arguments.with_readme,
        )
    except BuildError as error:
        print(f"build failed: {error}", file=sys.stderr)
        return 1

    print(f"wrote {bundle_path} ({bundle_path.stat().st_size / 1024:.1f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
