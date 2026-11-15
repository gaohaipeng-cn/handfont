"""Trim a PyInstaller onedir build of HandFont.

The app only uses QtCore / QtGui / QtWidgets (verified by dumping the loaded
modules of the running exe). PyInstaller still bundles Qml, Quick, Pdf,
OpenGL, Network, every Qt translation and a software OpenGL fallback, which
together are about half of the output size.

This script removes only items that are NOT loaded at runtime, and works for
both bindings: PySide6 (Qt6, 64-bit build) and PySide2 (Qt5, 32-bit build).

Usage:  python slim.py <app_dir> [--check]
        <app_dir> is the folder that contains the exe and _internal.
"""

import os
import shutil
import sys

# Qt5/Qt6 module stems that our app never imports or links against.
DROP_STEMS = {
    "Quick", "Qml", "QmlModels", "QmlWorkerScript", "QmlCore",
    "Pdf", "OpenGL", "OpenGLWidgets",
    "Network", "NetworkAuth", "Bearer",
    "VirtualKeyboard", "WebEngineCore", "WebEngineWidgets", "WebEngineQuick",
    "WebChannel", "WebSockets",
    "Multimedia", "MultimediaWidgets", "MultimediaQuick",
    "Positioning", "Location", "Sensors", "SerialPort", "SerialBus",
    "Bluetooth", "Nfc", "Charts", "DataVisualization",
    "3DAnimation", "3DCore", "3DExtras", "3DInput", "3DLogic", "3DRender",
    "Designer", "Help", "Test", "Sql", "Xml", "XmlPatterns",
    "PrintSupport", "SvgWidgets", "UiTools", "QuickWidgets", "QuickControls2",
    "RemoteObjects", "Scxml", "TextToSpeech", "Concurrent", "WebView",
    "DBus", "SpatialAudio", "Graphs", "GraphsWidgets", "HttpServer", "PdfWidgets",
    "StateMachine", "Quick3D",
}

# Plugin directories that are useless for a plain widget application.
DROP_PLUGIN_DIRS = {
    "tls", "networkinformation", "platforminputcontexts", "generic",
    "sqldrivers", "printsupport", "bearer", "mediaservice", "audio",
    "canbus", "designer", "qmltooling", "renderers", "geometryloaders",
    "sceneparsers", "texttospeech", "webview", "assetimporters",
}

# Keep only these translations (the app loads qtbase_zh_CN explicitly).
KEEP_TRANSLATIONS = {"qtbase_zh_CN.qm", "qt_zh_CN.qm"}

# Keep only the ICO reader; the app never loads jpeg/tiff/webp/pdf images.
KEEP_IMAGEFORMATS = {"qico.dll"}

# Keep only the Windows platform plugin and the native style.
KEEP_PLATFORMS = {"qwindows.dll"}
KEEP_STYLES = {"qmodernwindowsstyle.dll", "qwindowsvistastyle.dll"}

# Standalone payloads that are not needed (software GL fallback, Qt's own
# OpenSSL copy that duplicates the Python one).
DROP_FILES = {
    "opengl32sw.dll",
    "libcrypto-3-x64.dll", "libssl-3-x64.dll",
    "libcrypto-1_1-x64.dll", "libssl-1_1-x64.dll",
    "QtWebEngineProcess.exe",
}

# Top-level packages pulled in but never imported by the app.
DROP_PACKAGES = {"pathops"}


def _size(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


class Slimmer(object):
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.freed = 0
        self.count = 0
        self.failed = []

    def drop(self, path, why):
        if not os.path.exists(path):
            return
        size = _size(path)
        if self.dry_run:
            print("  would drop %7.1f KB  %-52s %s" % (size / 1024.0, os.path.basename(path), why))
        else:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
            except OSError as exc:
                self.failed.append((path, str(exc)))
                return
        self.freed += size
        self.count += 1
        if not self.dry_run:
            print("  -%8.1f KB  %-52s %s" % (size / 1024.0, os.path.basename(path), why))


def find_qt_dir(base):
    for name in ("PySide6", "PySide2"):
        path = os.path.join(base, name)
        if os.path.isdir(path):
            return path
    return None


def trim_qt_modules(qt_dir, s):
    for name in sorted(os.listdir(qt_dir)):
        path = os.path.join(qt_dir, name)
        if not os.path.isfile(path):
            continue
        stem, ext = os.path.splitext(name)
        if ext.lower() not in (".dll", ".pyd"):
            continue
        for prefix in ("Qt6", "Qt5"):
            if stem.startswith(prefix):
                module = stem[len(prefix):]
                if module in DROP_STEMS:
                    s.drop(path, "unused Qt module")
                break
        else:
            if name in DROP_FILES:
                s.drop(path, "unused runtime")


def trim_qt_payload(qt_dir, s):
    # translations
    tr = os.path.join(qt_dir, "translations")
    if os.path.isdir(tr):
        for name in sorted(os.listdir(tr)):
            if name not in KEEP_TRANSLATIONS:
                s.drop(os.path.join(tr, name), "translation")

    plugins = os.path.join(qt_dir, "plugins")
    if not os.path.isdir(plugins):
        return
    for name in sorted(os.listdir(plugins)):
        path = os.path.join(plugins, name)
        if os.path.isdir(path) and name in DROP_PLUGIN_DIRS:
            s.drop(path, "unused plugin group")
    for group, keep in (("platforms", KEEP_PLATFORMS),
                        ("styles", KEEP_STYLES),
                        ("imageformats", KEEP_IMAGEFORMATS)):
        folder = os.path.join(plugins, group)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name not in keep:
                s.drop(os.path.join(folder, name), "unused plugin")


def main(argv):
    if len(argv) < 2:
        print("usage: python slim.py <app_dir> [--check]")
        return 2
    app_dir = os.path.abspath(argv[1])
    dry_run = "--check" in argv[2:]
    if not os.path.isdir(app_dir):
        print("[slim] error: %s not found" % app_dir)
        return 2

    # PyInstaller 6 keeps everything in _internal, older versions next to the exe.
    internal = os.path.join(app_dir, "_internal")
    base = internal if os.path.isdir(internal) else app_dir

    before = _size(app_dir)
    s = Slimmer(dry_run)
    print("[slim] %s%s" % (app_dir, "  (dry run)" if dry_run else ""))

    qt_dir = find_qt_dir(base)
    if qt_dir is None:
        print("[slim] no PySide package found, nothing to trim")
    else:
        print("[slim] Qt payload: %s" % os.path.basename(qt_dir))
        trim_qt_modules(qt_dir, s)
        trim_qt_payload(qt_dir, s)

    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        if os.path.isdir(path) and name in DROP_PACKAGES:
            s.drop(path, "unused package")
        elif os.path.isfile(path) and name in DROP_FILES:
            s.drop(path, "unused runtime")

    after = _size(app_dir)
    print("[slim] removed %d items, %.1f MB (%.1f MB -> %.1f MB)"
          % (s.count, s.freed / 1048576.0, before / 1048576.0, after / 1048576.0))
    if s.failed:
        # A locked file must not fail the whole build: the exe is already
        # complete, only the optional trimming is incomplete.
        print("[slim] warning: %d item(s) could not be removed (file in use?)" % len(s.failed))
        for path, err in s.failed[:5]:
            print("       %s: %s" % (os.path.basename(path), err))
        print("[slim] the build is usable, but it is not fully trimmed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
