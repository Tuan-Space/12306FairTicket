"""Verify that a Windows portable bundle contains this checkout's GUI code/assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import marshal
from pathlib import Path
import sys
from types import CodeType

from PyInstaller.archive.readers import CArchiveReader


ROOT = Path(__file__).resolve().parents[1]


def normalized(code: CodeType) -> CodeType:
    # PyInstaller shortens source paths. Preserve all executable content and
    # line metadata while ignoring only that environment-dependent filename.
    return code.replace(co_filename="", co_consts=tuple(
        normalized(value) if isinstance(value, CodeType) else value for value in code.co_consts))


def verify(exe: Path, source_root: Path = ROOT) -> dict:
    archive = CArchiveReader(str(exe))
    pyz_name = next(name for name in archive.toc if name.startswith("PYZ"))
    modules = archive.open_embedded_archive(pyz_name)
    checked = []
    for name in sorted(modules.toc):
        if name != "ticket_app" and not name.startswith("ticket_app."):
            continue
        path = source_root.joinpath(*name.split("."))
        path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
        expected = compile(path.read_bytes(), str(path), "exec", dont_inherit=True, optimize=0)
        actual = modules.extract(name)
        if normalized(actual) != normalized(expected):
            raise ValueError(f"Embedded module differs from source: {name}")
        checked.append(name)
    required = {"ticket_app.cart", "ticket_app.client", "ticket_app.runner", "ticket_app.configuration",
                "ticket_app.gui.app", "ticket_app.gui.cart_flow", "ticket_app.gui.cart_widgets",
                "ticket_app.gui.compat", "ticket_app.gui.validation", "ticket_app.gui.worker"}
    if not required.issubset(checked):
        raise ValueError(f"Missing required modules: {required - set(checked)}")
    entry = compile((source_root / "gui.py").read_bytes(), "gui.py", "exec", dont_inherit=True)
    if normalized(marshal.loads(archive.extract("gui"))) != normalized(entry):
        raise ValueError("GUI entry point differs from source")
    assets = []
    for source in sorted((source_root / "assets").rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(source_root)
        packaged = exe.parent / "_internal" / relative
        if source.read_bytes() != packaged.read_bytes():
            raise ValueError(f"Packaged asset differs from source: {relative}")
        assets.append(relative.as_posix())
    return {"verified": True, "python": sys.version.split()[0], "modules": checked,
            "entry": "gui.py", "assets": assets,
            "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = verify(args.exe.resolve())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Verified {len(report['modules'])} modules, GUI entry and {len(report['assets'])} assets.")
    print("EXE SHA256: " + report["exe_sha256"])


if __name__ == "__main__":
    main()
