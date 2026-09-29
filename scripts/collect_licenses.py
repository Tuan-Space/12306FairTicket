"""Collect versioned runtime license texts without network access.

Run with the same interpreter that builds the executable. Installed wheel
licenses are authoritative; the repository supplies official texts omitted
from the Qt wheels and a version-checked CPython fallback.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import ssl
import sys


RUNTIME_DISTRIBUTIONS = (
    "PySide6", "PySide6-Essentials", "PySide6-Addons", "shiboken6",
    "requests", "json5", "certifi", "charset-normalizer", "idna", "urllib3",
    "setuptools", "packaging", "typing_extensions",
    # The bootloader is part of the executable even though its builder is not.
    "PyInstaller",
)
FALLBACK_ROOT = Path(__file__).resolve().parents[1] / "docs" / "licenses"


def collect(output: Path) -> dict:
    provenance = json.loads((FALLBACK_ROOT / "sources.json").read_text(encoding="utf-8"))
    official = {item["file"]: item for item in provenance["files"]}
    files: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    components = []

    def add(filename: str, body: bytes) -> str:
        if not body.strip():
            raise RuntimeError(f"Empty license text: {filename}")
        digest = hashlib.sha256(body).hexdigest()
        if digest in hashes:
            return hashes[digest]
        if filename in files:
            raise RuntimeError(f"Conflicting license output: {filename}")
        files[filename] = body
        hashes[digest] = filename
        return filename

    def vendored(filename: str) -> tuple[str, list[str]]:
        entry = official[filename]
        body = (FALLBACK_ROOT / filename).read_bytes()
        if hashlib.sha256(body).hexdigest() != entry["sha256"]:
            raise RuntimeError(f"Official fallback text changed: {filename}")
        return add(filename, body), entry["sources"]

    for name in RUNTIME_DISTRIBUTIONS:
        distribution = metadata.distribution(name)
        if name in RUNTIME_DISTRIBUTIONS[:4] and distribution.version != provenance["qt_version"]:
            raise RuntimeError("Qt version changed; refresh docs/licenses before packaging")
        found = []
        for entry in distribution.files or ():
            if ".dist-info/" not in str(entry):
                continue
            if not any(word in entry.name.lower() for word in ("license", "copying", "notice")):
                continue
            # Vendor notices may share a basename but cover different code.
            suffix = str(entry).replace("/", "_").replace("\\", "_")
            filename = add(f"{name}-{suffix}", Path(distribution.locate_file(entry)).read_bytes())
            found.append(filename)
        if not found:
            raise RuntimeError(f"No installed license found for {name}")
        components.append({"component": name, "version": distribution.version,
                           "files": sorted(set(found)), "source": "installed wheel .dist-info"})

    python_version = ".".join(map(str, sys.version_info[:3]))
    candidates = [Path(sys.base_prefix) / name for name in ("LICENSE.txt", "LICENSE_PYTHON.txt", "LICENSE")]
    python_license = next((path for path in candidates if path.is_file()), None)
    if python_license:
        python_file = add("PYTHON-LICENSE.txt", python_license.read_bytes())
        python_sources = [f"Python {python_version} interpreter distribution / {python_license.name}"]
    elif python_version == provenance["python_fallback_version"]:
        python_file, python_sources = vendored(f"PYTHON-{python_version}.txt")
    else:
        raise RuntimeError(f"Python {python_version} license missing; supply the matching official text")
    components.append({"component": "Python", "version": python_version,
                       "files": [python_file], "sources": python_sources})

    qt_files, qt_sources = [], []
    for name in ("QT-LGPL-3.0.txt", "QT-GPL-2.0.txt", "QT-GPL-3.0.txt"):
        filename, sources = vendored(name)
        qt_files.append(filename)
        qt_sources.extend(sources)
    components.append({"component": "Qt open-source license alternatives",
                       "version": provenance["qt_version"], "files": qt_files, "sources": qt_sources})

    if not ssl.OPENSSL_VERSION.startswith("OpenSSL 3."):
        raise RuntimeError("OpenSSL license family changed; refresh docs/licenses before packaging")
    openssl_file, openssl_sources = vendored("OPENSSL-3-APACHE-2.0.txt")
    components.append({"component": "OpenSSL", "version": ssl.OPENSSL_VERSION,
                       "files": [openssl_file], "sources": openssl_sources})

    inventory = {"components": components, "files": [
        {"file": name, "sha256": hashlib.sha256(body).hexdigest()}
        for name, body in sorted(files.items())
    ]}
    # Validate everything before creating the output. Do not erase unrelated files.
    output.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (output / name).write_bytes(body)
    (output / "inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "README.txt").write_text(
        "Third-party runtime license texts\n\n"
        "inventory.json records installed component versions, sources and SHA256 values.\n"
        "Identical license files are stored once and referenced by every relevant component.\n"
        "Qt shared libraries remain replaceable under _internal; preserve that directory.\n"
        "Qt / PySide6 source: https://download.qt.io/archive/qt/6.8/6.8.3/\n"
        "PySide6 / Shiboken source: https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.8.3-src/\n"
        "These notices do not assign a license to the application's own code.\n",
        encoding="utf-8",
    )
    return inventory


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = collect(args.output)
    print(f"Collected {len(result['files'])} unique license texts for {len(result['components'])} components")
