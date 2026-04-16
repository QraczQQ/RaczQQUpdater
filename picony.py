# -*- coding: utf-8 -*-

import os
import shutil
import subprocess
import tarfile
import zipfile

from Screens.Screen import Screen
from Screens.MessageBox import MessageBox
from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.Sources.List import List
from Screens.Console import Console

try:
    _
except NameError:
    def _(txt):
        return txt


PLUGIN_TMP_PATH = "/tmp/RaczQQUpdater/"
PICON_TMP_DIR = os.path.join(PLUGIN_TMP_PATH, "picony")


def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)


class PiconyScreen(Screen):
    skin = '''
    <screen name="PiconyScreen" position="center,center" size="900,560" title="Picony">
        <widget name="title" position="20,15" size="860,35" font="Regular;28" halign="center" />
        <widget source="list" render="Listbox" position="20,70" size="860,360" scrollbarMode="showOnDemand">
            <convert type="StringList" />
        </widget>
        <widget name="status" position="20,445" size="860,30" font="Regular;22" halign="left" />
        <widget name="target" position="20,480" size="860,30" font="Regular;22" halign="left" />
        <widget name="hint" position="20,515" size="860,30" font="Regular;20" halign="center" />
    </screen>'''

    TARGET_DIRS = [
        "/usr/share/enigma2/picon/",
        "/picon/",
        "/media/hdd/picon/",
        "/media/usb/picon/",
    ]

    PICON_PACKS = [
        ("Przykładowa paczka ZIP", "https://example.com/picons.zip"),
        ("Przykładowa paczka TAR.GZ", "https://example.com/picons.tar.gz"),
    ]

    def __init__(self, session):
        Screen.__init__(self, session)
        self.session = session

        self.target_dir_index = 0

        self["title"] = Label(_("Menedżer piconów"))
        self["list"] = List([])
        self["status"] = Label(_("OK - pobierz | Żółty - zmień katalog | Niebieski - wyczyść katalog"))
        self["target"] = Label("")
        self["hint"] = Label(_("EXIT - powrót"))

        self["actions"] = ActionMap(
            ["OkCancelActions", "ColorActions", "DirectionActions"],
            {
                "ok": self.keyOK,
                "cancel": self.close,
                "back": self.close,
                "yellow": self.changeTargetDir,
                "blue": self.clearTargetDir,
                "up": self.keyUp,
                "down": self.keyDown,
            },
            -1
        )

        self.refreshList()
        self.refreshTargetLabel()

    def refreshList(self):
        self["list"].setList([(name, url) for name, url in self.PICON_PACKS])

    def refreshTargetLabel(self):
        self["target"].setText(_("Katalog docelowy: %s") % self.getTargetDir())

    def getTargetDir(self):
        return self.TARGET_DIRS[self.target_dir_index]

    def keyUp(self):
        try:
            self["list"].selectPrevious()
        except Exception:
            pass

    def keyDown(self):
        try:
            self["list"].selectNext()
        except Exception:
            pass

    def changeTargetDir(self):
        self.target_dir_index += 1
        if self.target_dir_index >= len(self.TARGET_DIRS):
            self.target_dir_index = 0
        self.refreshTargetLabel()

    def clearTargetDir(self):
        target = self.getTargetDir()
        self.session.openWithCallback(
            lambda answer: self._doClear(answer, target),
            MessageBox,
            _("Usunąć wszystkie pliki PNG z katalogu?\n\n%s") % target,
            MessageBox.TYPE_YESNO
        )

    def _doClear(self, answer, target):
        if not answer:
            return
        try:
            ensure_dir(target)
            for name in os.listdir(target):
                if name.lower().endswith(".png"):
                    try:
                        os.remove(os.path.join(target, name))
                    except Exception:
                        pass
            self.session.open(MessageBox, _("Wyczyszczono katalog piconów."), MessageBox.TYPE_INFO, timeout=4)
        except Exception as e:
            self.session.open(MessageBox, _("Błąd czyszczenia:\n%s") % str(e), MessageBox.TYPE_ERROR, timeout=6)

    def keyOK(self):
        sel = self["list"].getCurrent()
        if not sel:
            return
        title = sel[0]
        url = sel[1]
        self.install_archive_from_url(title, url)

    def install_archive_from_url(self, title, url):
        target_dir = self.getTargetDir()

        if not url:
            self.session.open(MessageBox, _("Brak adresu URL do pobrania."), MessageBox.TYPE_ERROR, timeout=5)
            return

        ensure_dir(PLUGIN_TMP_PATH)
        ensure_dir(PICON_TMP_DIR)
        ensure_dir(target_dir)

        archive_name = os.path.basename(url) or "picons_download"
        archive_path = os.path.join(PICON_TMP_DIR, archive_name)
        extract_dir = os.path.join(PICON_TMP_DIR, "extract")

        cmd = (
            '[ -d "{extract}" ] && rm -rf "{extract}"; '
            'mkdir -p "{extract}"; '
            '[ -f "{archive}" ] && rm -f "{archive}"; '
            'wget -T 30 --no-check-certificate -O "{archive}" "{url}"'
        ).format(
            extract=extract_dir,
            archive=archive_path,
            url=url
        )

        self.session.openWithCallback(
            lambda *args: self._afterDownload(title, archive_path, extract_dir, target_dir),
            Console,
            title=title,
            cmdlist=[cmd],
            closeOnSuccess=True
        )

    def _afterDownload(self, title, archive_path, extract_dir, target_dir):
        try:
            if not os.path.exists(archive_path) or os.path.getsize(archive_path) == 0:
                raise Exception("Nie udało się pobrać archiwum.")

            if archive_path.lower().endswith(".zip"):
                zf = zipfile.ZipFile(archive_path, "r")
                zf.extractall(extract_dir)
                zf.close()
            elif archive_path.lower().endswith(".tar.gz") or archive_path.lower().endswith(".tgz"):
                tf = tarfile.open(archive_path, "r:gz")
                tf.extractall(extract_dir)
                tf.close()
            else:
                raise Exception("Nieobsługiwany format archiwum.")

            copied = 0
            for root, dirs, files in os.walk(extract_dir):
                for name in files:
                    if name.lower().endswith(".png"):
                        src = os.path.join(root, name)
                        dst = os.path.join(target_dir, name)
                        shutil.copy2(src, dst)
                        copied += 1

            if copied == 0:
                raise Exception("Nie znaleziono żadnych plików PNG w archiwum.")

            self.session.open(
                MessageBox,
                _("Zainstalowano %d piconów do:\n%s") % (copied, target_dir),
                MessageBox.TYPE_INFO,
                timeout=6
            )
        except Exception as e:
            self.session.open(
                MessageBox,
                _("Błąd instalacji piconów:\n%s") % str(e),
                MessageBox.TYPE_ERROR,
                timeout=8
            )
