# -*- coding: utf-8 -*-
from __future__ import print_function

import json
import os
import re
import shutil
import tempfile
import zipfile
from threading import Thread

try:
    from urllib.request import Request, urlopen
except ImportError:  # Python 2 images
    from urllib2 import Request, urlopen

from twisted.internet import reactor

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.Sources.List import List
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Screens.Standby import TryQuitMainloop


try:
    _
except NameError:
    def _(text):
        return text


APP_REPOSITORY = "QraczQQ/b4Default-FHD-app"
SKIN_REPOSITORY = "QraczQQ/b4Default-FHD-skin"
ADDONS_REPOSITORY = "QraczQQ/b4Addons"

APP_INSTALL_DIR = "/usr/lib/enigma2/python/Plugins/Extensions/b4DefaultFHD"
SKIN_INSTALL_DIR = "/usr/share/enigma2/b4Default-FHD"
COMPONENTS_DIR = "/usr/lib/enigma2/python/Components"
MAX_ARCHIVE_SIZE = 80 * 1024 * 1024
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _request(url):
    return Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "RaczQQUpdater-b4Default",
        "X-GitHub-Api-Version": "2022-11-28",
    })


def _latest_release(repository):
    url = "https://api.github.com/repos/%s/tags?per_page=100" % repository
    response = urlopen(_request(url), timeout=20)
    try:
        payload = response.read()
    finally:
        response.close()
    if not isinstance(payload, str):
        payload = payload.decode("utf-8")

    releases = []
    for item in json.loads(payload):
        tag = str(item.get("name", "")).strip()
        match = VERSION_RE.match(tag)
        archive_url = item.get("zipball_url")
        if match and archive_url:
            releases.append((tuple(int(value) for value in match.groups()), tag, str(archive_url)))
    if not releases:
        raise ValueError(_("Repozytorium %s nie zawiera wersji oznaczonej tagiem.") % repository)
    releases.sort(reverse=True)
    _version, tag, archive_url = releases[0]
    return tag.lstrip("v"), archive_url


def _download(url, destination):
    response = urlopen(_request(url), timeout=90)
    size = 0
    try:
        with open(destination, "wb") as output:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ARCHIVE_SIZE:
                    raise ValueError(_("Pobrane archiwum jest zbyt duże."))
                output.write(chunk)
    finally:
        response.close()
    if not size:
        raise ValueError(_("Pobrano puste archiwum."))


def _safe_extract(archive_path, destination):
    destination = os.path.abspath(destination)
    with zipfile.ZipFile(archive_path, "r") as archive:
        for info in archive.infolist():
            member = info.filename.replace("\\", "/")
            target = os.path.abspath(os.path.join(destination, member))
            if target != destination and not target.startswith(destination + os.sep):
                raise ValueError(_("Archiwum zawiera niebezpieczną ścieżkę."))
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError(_("Archiwum zawiera niedozwolony link symboliczny."))
        archive.extractall(destination)


def _download_source(work_dir, name, url, marker):
    archive_path = os.path.join(work_dir, "%s.zip" % name)
    extract_dir = os.path.join(work_dir, name)
    os.makedirs(extract_dir)
    _download(url, archive_path)
    _safe_extract(archive_path, extract_dir)
    for entry in os.listdir(extract_dir):
        candidate = os.path.join(extract_dir, entry)
        if os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, marker)):
            return candidate
    raise ValueError(_("Archiwum %s nie zawiera oczekiwanych plików.") % name)


def _read_version(source):
    try:
        with open(os.path.join(source, "VERSION"), "r") as version_file:
            return version_file.read().strip().lstrip("v")
    except Exception:
        return ""


def _runtime_files(source):
    excluded_dirs = set((".git", ".github", ".githooks", "tests", "tools", "__pycache__"))
    for root, directories, files in os.walk(source):
        directories[:] = [
            name for name in directories
            if name not in excluded_dirs and not name.startswith(".")
        ]
        relative_root = os.path.relpath(root, source)
        for name in files:
            if name.startswith(".") or name.endswith((".pyc", ".pyo")):
                continue
            relative = name if relative_root == "." else os.path.join(relative_root, name)
            yield relative


def _install_trees(source_targets, backup_dir):
    """Overlay files and roll back every touched file if any copy fails."""
    restored = []
    created = []
    try:
        for tree_index, (source, target) in enumerate(source_targets):
            if not os.path.isdir(target):
                os.makedirs(target)
            for relative in _runtime_files(source):
                source_file = os.path.join(source, relative)
                target_file = os.path.join(target, relative)
                target_parent = os.path.dirname(target_file)
                if not os.path.isdir(target_parent):
                    os.makedirs(target_parent)

                if os.path.isfile(target_file):
                    backup_file = os.path.join(backup_dir, str(tree_index), relative)
                    backup_parent = os.path.dirname(backup_file)
                    if not os.path.isdir(backup_parent):
                        os.makedirs(backup_parent)
                    shutil.copy2(target_file, backup_file)
                    restored.append((backup_file, target_file))
                else:
                    created.append(target_file)

                temporary = target_file + ".raczqq-new"
                shutil.copy2(source_file, temporary)
                if os.path.exists(target_file):
                    os.unlink(target_file)
                os.rename(temporary, target_file)
    except Exception:
        for path in reversed(created):
            try:
                if os.path.isfile(path):
                    os.unlink(path)
            except Exception:
                pass
        for backup_file, target_file in reversed(restored):
            try:
                shutil.copy2(backup_file, target_file)
            except Exception:
                pass
        raise


def install_products(products):
    work_dir = tempfile.mkdtemp(prefix="raczqq-b4default-")
    installed = []
    source_targets = []
    try:
        if "app" in products:
            version, url = _latest_release(APP_REPOSITORY)
            source = _download_source(work_dir, "app", url, "plugin.py")
            if _read_version(source) != version:
                raise ValueError(_("Wersja aplikacji w archiwum nie zgadza się z najnowszym tagiem."))
            source_targets.append((source, APP_INSTALL_DIR))
            installed.append(_("aplikacja %s") % version)

        if "skin" in products:
            version, url = _latest_release(SKIN_REPOSITORY)
            skin_source = _download_source(work_dir, "skin", url, "skin.xml")
            if _read_version(skin_source) != version:
                raise ValueError(_("Wersja skina w archiwum nie zgadza się z najnowszym tagiem."))

            addons_url = "https://github.com/%s/archive/refs/heads/main.zip" % ADDONS_REPOSITORY
            addons_source = _download_source(work_dir, "addons", addons_url, "Converter")
            converter_source = os.path.join(addons_source, "Converter")
            renderer_source = os.path.join(addons_source, "Renderer")
            if not os.path.isdir(converter_source) or not os.path.isdir(renderer_source):
                raise ValueError(_("Repozytorium b4Addons nie zawiera katalogów Converter i Renderer."))

            source_targets.extend((
                (skin_source, SKIN_INSTALL_DIR),
                (converter_source, os.path.join(COMPONENTS_DIR, "Converter")),
                (renderer_source, os.path.join(COMPONENTS_DIR, "Renderer")),
            ))
            installed.append(_("skin %s oraz b4Addons") % version)

        _install_trees(source_targets, os.path.join(work_dir, "backup"))
        return ", ".join(installed)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


class B4DefaultInstallScreen(Screen):
    skin = '''<screen name="B4DefaultInstallScreen" position="center,center" size="900,500" title="b4Default Skin and App" backgroundColor="#0e1116">
      <eLabel position="0,0" size="900,64" backgroundColor="#151a21" zPosition="-5" />
      <eLabel position="0,64" size="900,2" backgroundColor="#4a9eff" />
      <eLabel position="24,20" size="4,26" backgroundColor="#4a9eff" />
      <widget name="title" position="40,13" size="820,40" font="Regular;28" foregroundColor="#e8eaed" backgroundColor="#151a21" />
      <widget source="list" render="Listbox" position="24,88" size="852,160" scrollbarMode="showOnDemand" backgroundColor="#12161c" backgroundColorSelected="#1d2735" foregroundColor="#e8eaed" foregroundColorSelected="#ffffff">
        <convert type="TemplatedMultiContent">
        {"template": [
          MultiContentEntryText(pos=(20,6), size=(812,32), font=0, color=0xe8eaed, color_sel=0xffffff, flags=RT_HALIGN_LEFT|RT_VALIGN_CENTER, text=0),
          MultiContentEntryText(pos=(20,39), size=(812,25), font=1, color=0x7c8898, color_sel=0x9fb4cc, flags=RT_HALIGN_LEFT|RT_VALIGN_CENTER, text=2)
        ], "fonts": [gFont("Regular",24), gFont("Regular",18)], "itemHeight": 76}
        </convert>
      </widget>
      <eLabel position="24,270" size="852,1" backgroundColor="#232a34" />
      <widget name="status" position="24,286" size="852,82" font="Regular;19" halign="center" valign="center" foregroundColor="#9aa4b2" backgroundColor="#0e1116" />
      <eLabel position="24,396" size="4,36" backgroundColor="#ff5252" />
      <widget name="key_red" position="28,396" size="200,36" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
      <eLabel position="240,396" size="4,36" backgroundColor="#3ddc84" />
      <widget name="key_green" position="244,396" size="260,36" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
      <eLabel position="516,396" size="4,36" backgroundColor="#ffb020" />
      <widget name="key_yellow" position="520,396" size="356,36" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
      <eLabel position="0,466" size="900,34" backgroundColor="#151a21" />
      <widget name="footer" position="24,466" size="852,34" font="Regular;16" halign="center" valign="center" foregroundColor="#6b7684" backgroundColor="#151a21" />
    </screen>'''

    ITEMS = (
        (_("Zainstaluj b4Default-FHD skin"), "skin", _("Najnowszy skin + Converter i Renderer z b4Addons")),
        (_("Zainstaluj b4Default-FHD app"), "app", _("Najnowsza aplikacja b4Default-FHD Skin App")),
    )

    def __init__(self, session):
        Screen.__init__(self, session)
        self.session = session
        self.busy = False
        self["title"] = Label("b4Default Skin and App")
        self["list"] = List(list(self.ITEMS))
        self["status"] = Label(_("Wybierz składnik do pobrania i instalacji z GitHub."))
        self["key_red"] = Label(_("Zamknij"))
        self["key_green"] = Label(_("Instaluj wybrane"))
        self["key_yellow"] = Label(_("Instaluj skin i app"))
        self["footer"] = Label("GitHub: QraczQQ | b4Default-FHD")
        self["actions"] = ActionMap(
            ["WizardActions", "ColorActions"],
            {
                "red": self.close_screen,
                "green": self.install_selected,
                "yellow": self.install_all,
                "ok": self.install_selected,
                "back": self.close_screen,
            },
        )

    def install_selected(self):
        current = self["list"].getCurrent()
        if current:
            self._confirm((current[1],), current[0])

    def close_screen(self):
        if self.busy:
            self.session.open(MessageBox, _("Poczekaj na zakończenie instalacji."), MessageBox.TYPE_INFO, timeout=3)
            return
        self.close()

    def install_all(self):
        self._confirm(("skin", "app"), _("skin i aplikację"))

    def _confirm(self, products, label):
        if self.busy:
            self.session.open(MessageBox, _("Instalacja już trwa."), MessageBox.TYPE_INFO, timeout=3)
            return
        self.session.openWithCallback(
            lambda answer: self._start(products) if answer else None,
            MessageBox,
            _("Pobrać najnowszą wersję i zainstalować: %s?") % label,
            MessageBox.TYPE_YESNO,
        )

    def _start(self, products):
        self.busy = True
        self["status"].setText(_("Pobieranie najnowszych wersji z GitHub..."))

        def worker():
            try:
                installed = install_products(products)
                reactor.callFromThread(self._finished, installed)
            except Exception as error:
                reactor.callFromThread(self._failed, str(error))

        Thread(target=worker).start()

    def _failed(self, error):
        self.busy = False
        self["status"].setText(_("Błąd instalacji: %s") % error)
        self.session.open(
            MessageBox,
            _("Nie udało się zainstalować b4Default:\n%s") % error,
            MessageBox.TYPE_ERROR,
            timeout=8,
        )

    def _finished(self, installed):
        self.busy = False
        self["status"].setText(_("Zainstalowano: %s") % installed)
        self.session.openWithCallback(
            self._restart_answer,
            MessageBox,
            _("Zainstalowano: %s.\nUruchomić ponownie GUI, aby wczytać zmiany?") % installed,
            MessageBox.TYPE_YESNO,
        )

    def _restart_answer(self, answer):
        if answer:
            self.session.open(TryQuitMainloop, 3)
