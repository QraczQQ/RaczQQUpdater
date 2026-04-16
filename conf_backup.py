# -*- coding: utf-8 -*-

import os
import time

from Screens.Screen import Screen
from Screens.MessageBox import MessageBox
from Screens.Console import Console
from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.Sources.List import List

try:
    _
except NameError:
    def _(txt):
        return txt


BACKUP_DIR = "/data/RaczQQUpdater/system_backup"
PLUGIN_DIR = "/usr/lib/enigma2/python/Plugins/Extensions/RaczQQUpdater"
FAV_LIST_FILE = os.path.join(PLUGIN_DIR, "backup_fav.list")


def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)


class ConfRestoreListScreen(Screen):
    skin = '''
    <screen name="ConfRestoreListScreen" position="center,center" size="900,560" title="Przywróć ustawienia">
        <widget name="title" position="20,15" size="860,35" font="Regular;28" halign="center" />
        <widget source="list" render="Listbox" position="20,70" size="860,400" scrollbarMode="showOnDemand">
            <convert type="StringList" />
        </widget>
        <widget name="status" position="20,485" size="860,30" font="Regular;22" halign="left" />
        <widget name="hint" position="20,520" size="860,30" font="Regular;20" halign="center" />
    </screen>'''

    def __init__(self, session, backup_dir):
        Screen.__init__(self, session)
        self.session = session
        self.backup_dir = backup_dir

        self["title"] = Label(_("Wybierz backup do przywrócenia"))
        self["list"] = List([])
        self["status"] = Label(_("OK - wybierz | Niebieski - usuń backup | EXIT - powrót"))
        self["hint"] = Label("")

        self["actions"] = ActionMap(
            ["OkCancelActions", "ColorActions", "DirectionActions"],
            {
                "ok": self.keyOK,
                "cancel": self.close,
                "back": self.close,
                "blue": self.deleteSelectedBackup,
                "up": self.keyUp,
                "down": self.keyDown,
            },
            -1
        )

        self.refreshList()

    def refreshList(self):
        items = []
        if os.path.isdir(self.backup_dir):
            files = [f for f in os.listdir(self.backup_dir) if f.endswith(".tar.gz")]
            files.sort(reverse=True)
            for filename in files:
                fullpath = os.path.join(self.backup_dir, filename)
                size_kb = 0
                try:
                    size_kb = int(os.path.getsize(fullpath) / 1024)
                except Exception:
                    pass
                items.append(("%s (%d KB)" % (filename, size_kb), fullpath))

        if not items:
            items.append((_("Brak backupów"), ""))

        self["list"].setList(items)

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

    def keyOK(self):
        sel = self["list"].getCurrent()
        if not sel or not sel[1]:
            return
        self.close(sel[1])

    def deleteSelectedBackup(self):
        sel = self["list"].getCurrent()
        if not sel or not sel[1]:
            return

        backup_path = sel[1]
        self.session.openWithCallback(
            lambda answer: self._doDelete(answer, backup_path),
            MessageBox,
            _("Usunąć backup?\n\n%s") % os.path.basename(backup_path),
            MessageBox.TYPE_YESNO
        )

    def _doDelete(self, answer, backup_path):
        if not answer:
            return
        try:
            if os.path.exists(backup_path):
                os.remove(backup_path)
            self.refreshList()
            self.session.open(
                MessageBox,
                _("Backup został usunięty."),
                MessageBox.TYPE_INFO,
                timeout=4
            )
        except Exception as e:
            self.session.open(
                MessageBox,
                _("Błąd usuwania backupu:\n%s") % str(e),
                MessageBox.TYPE_ERROR,
                timeout=6
            )


class ConfBackupScreen(Screen):
    skin = '''
    <screen name="ConfBackupScreen" position="center,center" size="950,620" title="Backup plików systemowych">
        <widget name="title" position="20,15" size="910,35" font="Regular;28" halign="center" />

        <widget source="list" render="Listbox" position="20,70" size="910,390" scrollbarMode="showOnDemand">
            <convert type="StringList" />
        </widget>

        <widget name="status" position="20,470" size="910,30" font="Regular;22" halign="left" />
        <widget name="target" position="20,505" size="910,30" font="Regular;22" halign="left" />

        <widget name="key_yellow" position="20,575" size="220,30" font="Regular;22" halign="left" foregroundColor="#ffff00" />
        <widget name="key_blue" position="260,575" size="300,30" font="Regular;22" halign="left" foregroundColor="#00aaff" />
        <widget name="hint" position="580,575" size="350,30" font="Regular;20" halign="right" />
    </screen>'''

    BACKUP_ITEMS = [
        ("Backup /etc/enigma2", "/etc/enigma2"),
        ("Backup /etc/tuxbox", "/etc/tuxbox"),
        ("Backup /etc/network", "/etc/network"),
        ("Backup /etc/resolv.conf", "/etc/resolv.conf"),
        ("Backup /etc/fstab", "/etc/fstab"),
        ("Backup /usr/keys", "/usr/keys"),
        ("Backup /etc/hostname", "/etc/hostname"),
        ("Backup ulubionych ścieżek", "backup_fav"),
    ]

    def __init__(self, session):
        Screen.__init__(self, session)
        self.session = session

        self["title"] = Label(_("Twórz backup plików systemowych"))
        self["list"] = List([])
        self["status"] = Label(_("OK - utwórz backup"))
        self["target"] = Label(_("Katalog backupu: %s") % BACKUP_DIR)
        self["favinfo"] = Label(_("Lista ulubionych ścieżek: %s") % FAV_LIST_FILE)
        self["key_yellow"] = Label(_("Żółty - odśwież"))
        self["key_blue"] = Label(_("Niebieski - przywróć ustawienia"))
        self["hint"] = Label(_("EXIT - powrót"))

        self["actions"] = ActionMap(
            ["OkCancelActions", "ColorActions", "DirectionActions"],
            {
                "ok": self.keyOK,
                "cancel": self.close,
                "back": self.close,
                "yellow": self.refreshList,
                "blue": self.showBackupList,
                "up": self.keyUp,
                "down": self.keyDown,
            },
            -1
        )

        self.refreshList()

    def _read_fav_paths(self):
        paths = []
        if not os.path.exists(FAV_LIST_FILE):
            return paths

        try:
            with open(FAV_LIST_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    paths.append(line)
        except Exception:
            pass
        return paths

    def refreshList(self):
        entries = []
        for title, path in self.BACKUP_ITEMS:
            if path == "backup_fav":
                fav_paths = self._read_fav_paths()
                if fav_paths:
                    entries.append((title + " (%d wpisów)" % len(fav_paths), path))
                else:
                    entries.append((title + " [pusta lista]", path))
            else:
                if os.path.exists(path):
                    entries.append((title, path))
                else:
                    entries.append((title + " [brak]", path))
        self["list"].setList(entries)

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

    def keyOK(self):
        sel = self["list"].getCurrent()
        if not sel:
            return

        title = sel[0]
        source_path = sel[1]

        if source_path == "backup_fav":
            fav_paths = self._read_fav_paths()
            if not fav_paths:
                self.session.open(
                    MessageBox,
                    _("Plik backup_fav.list jest pusty albo nie istnieje."),
                    MessageBox.TYPE_ERROR,
                    timeout=5
                )
                return

            preview = "\n".join(fav_paths[:12])
            if len(fav_paths) > 12:
                preview += "\n..."

            self.session.openWithCallback(
                lambda answer: self._do_backup_fav(answer, fav_paths),
                MessageBox,
                _("Utworzyć backup ulubionych ścieżek?\n\n%s") % preview,
                MessageBox.TYPE_YESNO
            )
            return

        if not os.path.exists(source_path):
            self.session.open(
                MessageBox,
                _("Wybrana ścieżka nie istnieje:\n%s") % source_path,
                MessageBox.TYPE_ERROR,
                timeout=5
            )
            return

        self.session.openWithCallback(
            lambda answer: self._do_backup(answer, title, source_path),
            MessageBox,
            _("Utworzyć backup?\n\n%s") % source_path,
            MessageBox.TYPE_YESNO
        )

    def _do_backup(self, answer, title, source_path):
        if not answer:
            return

        ensure_dir(BACKUP_DIR)

        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_name = os.path.basename(source_path.rstrip("/")) or "root"
        archive_name = "backup_%s_%s.tar.gz" % (safe_name, ts)
        archive_path = os.path.join(BACKUP_DIR, archive_name)

        cmd = 'tar -czf "{archive}" "{source}"'.format(
            archive=archive_path,
            source=source_path
        )

        self.session.openWithCallback(
            lambda *args: self._after_backup(archive_path),
            Console,
            title=_("Tworzenie backupu"),
            cmdlist=[cmd],
            closeOnSuccess=True
        )

    def _do_backup_fav(self, answer, fav_paths):
        if not answer:
            return

        ensure_dir(BACKUP_DIR)

        valid_paths = [p for p in fav_paths if os.path.exists(p)]
        if not valid_paths:
            self.session.open(
                MessageBox,
                _("Żadna ścieżka z backup_fav.list nie istnieje."),
                MessageBox.TYPE_ERROR,
                timeout=5
            )
            return

        ts = time.strftime("%Y%m%d_%H%M%S")
        archive_name = "backup_fav_%s.tar.gz" % ts
        archive_path = os.path.join(BACKUP_DIR, archive_name)

        quoted = " ".join(['"%s"' % p for p in valid_paths])
        cmd = 'tar -czf "{archive}" {paths}'.format(
            archive=archive_path,
            paths=quoted
        )

        self.session.openWithCallback(
            lambda *args: self._after_backup(archive_path),
            Console,
            title=_("Tworzenie backupu ulubionych ścieżek"),
            cmdlist=[cmd],
            closeOnSuccess=True
        )

    def _after_backup(self, archive_path):
        if os.path.exists(archive_path) and os.path.getsize(archive_path) > 0:
            self.session.open(
                MessageBox,
                _("Backup utworzony:\n%s") % archive_path,
                MessageBox.TYPE_INFO,
                timeout=6
            )
        else:
            self.session.open(
                MessageBox,
                _("Nie udało się utworzyć backupu."),
                MessageBox.TYPE_ERROR,
                timeout=6
            )

    def showBackupList(self):
        ensure_dir(BACKUP_DIR)
        self.session.openWithCallback(
            self._onBackupSelected,
            ConfRestoreListScreen,
            BACKUP_DIR
        )

    def _onBackupSelected(self, backup_path=None):
        if not backup_path:
            return

        self.session.openWithCallback(
            lambda answer: self._do_restore(answer, backup_path),
            MessageBox,
            _("Przywrócić backup?\n\n%s") % os.path.basename(backup_path),
            MessageBox.TYPE_YESNO
        )

    def _detect_restore_target(self, backup_filename):
        name = os.path.basename(backup_filename).lower()

        mapping = [
            ("backup_fav_", "/"),
            ("backup_enigma2_", "/etc/enigma2"),
            ("backup_tuxbox_", "/etc/tuxbox"),
            ("backup_network_", "/etc/network"),
            ("backup_resolv.conf_", "/etc/resolv.conf"),
            ("backup_fstab_", "/etc/fstab"),
            ("backup_keys_", "/usr/keys"),
            ("backup_hostname_", "/etc/hostname"),
        ]

        for prefix, target in mapping:
            if name.startswith(prefix):
                return target
        return None

    def _do_restore(self, answer, backup_path):
        if not answer:
            return

        target_path = self._detect_restore_target(backup_path)
        if not target_path:
            self.session.open(
                MessageBox,
                _("Nie można określić docelowej ścieżki dla backupu:\n%s") % os.path.basename(backup_path),
                MessageBox.TYPE_ERROR,
                timeout=6
            )
            return

        cmd_parts = []

        if target_path != "/":
            parent_dir = os.path.dirname(target_path.rstrip("/")) or "/"

            if os.path.isdir(target_path):
                cmd_parts.append('[ -d "{0}" ] && rm -rf "{0}"'.format(target_path))
            else:
                cmd_parts.append('[ -f "{0}" ] && rm -f "{0}"'.format(target_path))

            cmd_parts.append('mkdir -p "{0}"'.format(parent_dir))

        cmd_parts.append('tar -xzf "{0}" -C "/"'.format(backup_path))
        cmd_parts.append('sync')

        cmd = " && ".join(cmd_parts)

        self.session.openWithCallback(
            lambda *args: self._after_restore(target_path),
            Console,
            title=_("Przywracanie ustawień"),
            cmdlist=[cmd],
            closeOnSuccess=True
        )

    def _after_restore(self, target_path):
        self.session.open(
            MessageBox,
            _("Przywrócono backup do:\n%s\n\nZalecany restart GUI.") % target_path,
            MessageBox.TYPE_INFO,
            timeout=8
        )
