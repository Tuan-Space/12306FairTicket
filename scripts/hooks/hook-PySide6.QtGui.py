"""Collect the desktop Qt GUI runtime without unused PDF/virtual keyboard plugins.

Filter before dependency analysis so their private QtPdf/Qml/Quick DLLs are
not pulled into the portable bundle. Native Windows IME and TLS remain intact.
"""

from pathlib import Path

from PyInstaller.utils.hooks.qt import add_qt6_dependencies

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
unused_plugins = {"qpdf.dll", "qtvirtualkeyboardplugin.dll"}
binaries = [item for item in binaries if Path(item[0]).name.lower() not in unused_plugins]
