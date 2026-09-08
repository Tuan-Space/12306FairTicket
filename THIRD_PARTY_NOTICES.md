# Third-Party Notices

This notice covers the expected dependencies of the Windows onedir build. Direct GUI and build dependencies are pinned in `requirements-gui.txt` and `requirements-dev.txt`; transitive versions selected from `requirements.txt` may still vary. Wheel metadata and license files shipped by each installed package remain authoritative.

## Runtime components

| Component | Expected license | Project / license information |
| --- | --- | --- |
| Python 3.12 | Python Software Foundation License | <https://docs.python.org/3/license.html> |
| PySide6, PySide6-Essentials, PySide6-Addons, Shiboken6 and Qt 6 libraries/plugins | LGPL-3.0-only, GPL-3.0-only, or applicable Qt commercial license | <https://doc.qt.io/qtforpython-6/licenses.html> |
| Requests | Apache-2.0 | <https://github.com/psf/requests/blob/main/LICENSE> |
| json5 0.15.0 | Apache-2.0 | <https://github.com/dpranke/pyjson5/blob/v0.15.0/LICENSE> |
| certifi | MPL-2.0 | <https://github.com/certifi/python-certifi/blob/master/LICENSE> |
| charset-normalizer | MIT | <https://github.com/jawah/charset_normalizer/blob/master/LICENSE> |
| idna | BSD-3-Clause | <https://github.com/kjd/idna/blob/master/LICENSE.md> |
| urllib3 | MIT | <https://github.com/urllib3/urllib3/blob/main/LICENSE.txt> |

PySide6 wheels include Qt shared libraries and plugins. The onedir build intentionally keeps these as separate dynamically loaded files under `_internal`; do not merge, remove, or rename them when redistributing the application. A distributor relying on the LGPL option must also provide the applicable LGPL text and notices, preserve recipients' lawful ability to replace/debug the covered libraries, and meet any corresponding source-code obligations. A valid commercial Qt license may impose different terms. See Qt's licensing guidance before public distribution: <https://www.qt.io/licensing/open-source-lgpl-obligations>.

## Build tooling

| Component | Expected license | Project / license information |
| --- | --- | --- |
| PyInstaller and its bootloader | GPL-2.0-or-later with the PyInstaller bootloader exception | <https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt> |
| pytest | MIT | <https://github.com/pytest-dev/pytest/blob/main/LICENSE> |
| pytest-qt | MIT | <https://github.com/pytest-dev/pytest-qt/blob/master/LICENSE> |
| GitHub Actions maintained by GitHub (`checkout`, `setup-python`, `upload-artifact`) | See each action repository | <https://github.com/actions> |

The PyInstaller bootloader exception permits distribution of applications produced by PyInstaller under the application's chosen license, subject to the exception's terms. PyInstaller itself is a build dependency and is not an application feature.

## Release checklist

Before distributing a build outside the project team:

1. Build in a fresh Python 3.12 virtual environment and record `python -m pip freeze` with the artifact provenance.
2. Inspect every installed wheel's `*.dist-info/METADATA`, `LICENSE*`, and `NOTICE*` files; update this inventory for added or removed dependencies.
3. Include all license and notice texts required by the exact dependency versions in the distribution.
4. Keep the complete onedir output together so dynamically loaded Qt libraries and plugins remain separate.
5. Confirm the repository owner's chosen license for the application's own source code. This file documents third-party components only and is not legal advice.
