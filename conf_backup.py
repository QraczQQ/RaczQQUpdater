# -*- coding: utf-8 -*-

import io
import os
import re
import time

RUNTIME_CONFIG_FILE = '/etc/enigma2/RaczQQUpdater.conf'
STORAGE_LOCATIONS = ('/media/hdd', '/media/usb', '/data', '/media/mmc')
DEFAULT_STORAGE_LOCATION = '/data'
PICON_PATHS = ('/picon', '/usr/share/enigma2/picon')

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


PLUGIN_DIR = "/usr/lib/enigma2/python/Plugins/Extensions/RaczQQUpdater"
FAV_LIST_FILE = os.path.join(PLUGIN_DIR, "backup_fav.list")


def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)


def get_device_model():
    '''Return the receiver model reported by /proc/stb/info/model.'''
    try:
        with io.open('/proc/stb/info/model', 'r', encoding='utf-8', errors='ignore') as f:
            model = f.readline().strip()
        return model or 'unknown'
    except Exception:
        return 'unknown'


def get_model_dir_name():
    model = get_device_model()
    safe_model = re.sub(r'[^A-Za-z0-9._-]+', '_', model).strip('._-')
    return safe_model or 'unknown'


def _read_config_lines():
    try:
        with io.open(RUNTIME_CONFIG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            return f.readlines()
    except Exception:
        return []


def is_storage_location_configured():
    for raw_line in _read_config_lines():
        line = raw_line.strip()
        if line.startswith('backup_location='):
            return line.split('=', 1)[1].strip() in STORAGE_LOCATIONS
    return False


def get_storage_location():
    for raw_line in _read_config_lines():
        line = raw_line.strip()
        if line.startswith('backup_location='):
            location = line.split('=', 1)[1].strip().rstrip('/') or '/'
            if location in STORAGE_LOCATIONS:
                return location
    return DEFAULT_STORAGE_LOCATION


def save_storage_location(location):
    location = (location or '').rstrip('/') or '/'
    if location not in STORAGE_LOCATIONS:
        raise ValueError('Nieobslugiwana lokalizacja backupu: %s' % location)

    lines = _read_config_lines()
    output = []
    replaced = False
    for raw_line in lines:
        if raw_line.strip().startswith('backup_location='):
            if not replaced:
                output.append('backup_location=%s\n' % location)
                replaced = True
        else:
            output.append(raw_line if raw_line.endswith('\n') else raw_line + '\n')
    if not replaced:
        if output and output[-1].strip():
            output.append('\n')
        output.append('backup_location=%s\n' % location)

    config_dir = os.path.dirname(RUNTIME_CONFIG_FILE)
    ensure_dir(config_dir)
    with io.open(RUNTIME_CONFIG_FILE, 'w', encoding='utf-8') as f:
        f.writelines(output)


def is_storage_location_available(location):
    if location not in STORAGE_LOCATIONS or not os.path.isdir(location):
        return False
    if location.startswith('/media/'):
        real_location = os.path.realpath(location)
        if not os.path.ismount(location) and not os.path.ismount(real_location):
            return False
    return os.access(location, os.W_OK)


def get_plugin_backup_dir(location=None):
    return os.path.join(location or get_storage_location(), 'RaczQQUpdater', 'backup')


def get_system_backup_dir(location=None):
    return os.path.join(
        location or get_storage_location(),
        'RaczQQUpdater',
        'system_backup',
        get_model_dir_name(),
    )


def get_picon_backup_dir(location=None):
    return os.path.join(location or get_storage_location(), 'RaczQQUpdater', 'picon')


def get_backup_search_dirs(kind):
    '''Return selected storage first, followed by the other known locations.'''
    selected = get_storage_location()
    locations = [selected] + [p for p in STORAGE_LOCATIONS if p != selected]
    builders = {
        'plugin': get_plugin_backup_dir,
        'system': get_system_backup_dir,
        'picon': get_picon_backup_dir,
    }
    builder = builders[kind]
    result = []
    for location in locations:
        if not is_storage_location_available(location):
            continue
        path = builder(location)
        if path not in result:
            result.append(path)
    return result


class ConfRestoreListScreen(Screen):
    skin = '''
    <screen name="ConfRestoreListScreen" position="center,center" size="900,560" title="Przywróć ustawienia" backgroundColor="#0e1116">
        <eLabel position="0,0"   size="900,560" backgroundColor="#0e1116" zPosition="-10" />
        <eLabel position="0,0"   size="900,56"  backgroundColor="#151a21" zPosition="-5" />
        <eLabel position="0,56"  size="900,2"   backgroundColor="#4a9eff" />
        <eLabel position="24,16" size="4,24"    backgroundColor="#4a9eff" />
        <widget name="title" position="40,14" size="836,28" font="Regular;21" halign="left" valign="center" foregroundColor="#e8eaed" backgroundColor="#151a21" />

        <eLabel position="24,76" size="852,352" backgroundColor="#12161c" zPosition="-3" />
        <widget source="list" render="Listbox" position="36,84" size="828,336"
                scrollbarMode="showOnDemand" font="Regular;20" itemHeight="42"
                backgroundColor="#12161c" backgroundColorSelected="#1d2735"
                foregroundColor="#e8eaed" foregroundColorSelected="#ffffff">
            <convert type="StringList" />
        </widget>

        <eLabel position="24,442" size="852,1"  backgroundColor="#232a34" />
        <widget name="status" position="24,454" size="852,28" font="Regular;18" halign="center" valign="center" foregroundColor="#9aa4b2" backgroundColor="#0e1116" />
        <widget name="hint"   position="24,490" size="852,26" font="Regular;17" halign="center" valign="center" foregroundColor="#4a9eff" backgroundColor="#0e1116" />
        <eLabel position="0,552" size="900,8" backgroundColor="#151a21" zPosition="-5" />
    </screen>'''

    def __init__(self, session, backup_dirs):
        Screen.__init__(self, session)
        self.session = session
        if isinstance(backup_dirs, (list, tuple)):
            self.backup_dirs = list(backup_dirs)
        else:
            self.backup_dirs = [backup_dirs]

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
        seen = set()
        for backup_dir in self.backup_dirs:
            if not os.path.isdir(backup_dir):
                continue
            files = [f for f in os.listdir(backup_dir) if f.endswith(".tar.gz")]
            files.sort(reverse=True)
            for filename in files:
                fullpath = os.path.join(backup_dir, filename)
                if fullpath in seen:
                    continue
                seen.add(fullpath)
                size_kb = 0
                try:
                    size_kb = int(os.path.getsize(fullpath) / 1024)
                except Exception:
                    pass
                location = backup_dir.split("/RaczQQUpdater/", 1)[0]
                label = "%s (%d KB) [%s]" % (filename, size_kb, location)
                items.append((label, fullpath))

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
    <screen name="ConfBackupScreen" position="center,center" size="950,620" title="Backup plików systemowych" backgroundColor="#0e1116">
        <eLabel position="0,0"   size="950,620" backgroundColor="#0e1116" zPosition="-10" />
        <eLabel position="0,0"   size="950,58"  backgroundColor="#151a21" zPosition="-5" />
        <eLabel position="0,58"  size="950,2"   backgroundColor="#4a9eff" />
        <eLabel position="24,17" size="4,24"    backgroundColor="#4a9eff" />
        <widget name="title" position="40,15" size="886,28" font="Regular;22" halign="left" valign="center" foregroundColor="#e8eaed" backgroundColor="#151a21" />

        <eLabel position="24,78" size="902,330" backgroundColor="#12161c" zPosition="-3" />
        <widget source="list" render="Listbox" position="36,86" size="878,314"
                scrollbarMode="showOnDemand" font="Regular;20" itemHeight="38"
                backgroundColor="#12161c" backgroundColorSelected="#1d2735"
                foregroundColor="#e8eaed" foregroundColorSelected="#ffffff">
            <convert type="StringList" />
        </widget>

        <eLabel position="24,424" size="902,1"  backgroundColor="#232a34" />
        <widget name="status"  position="24,436" size="902,28" font="Regular;19" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#0e1116" />
        <widget name="target"  position="24,470" size="902,24" font="Regular;17" halign="center" valign="center" foregroundColor="#4a9eff" backgroundColor="#0e1116" />
        <widget name="favinfo" position="24,498" size="902,22" font="Regular;16" halign="center" valign="center" foregroundColor="#7c8898" backgroundColor="#0e1116" />

        <eLabel position="24,556"  size="4,34" backgroundColor="#ffb020" />
        <widget name="key_yellow" position="28,556"  size="260,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
        <eLabel position="300,556" size="4,34" backgroundColor="#4a9eff" />
        <widget name="key_blue"   position="304,556" size="300,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
        <widget name="hint"       position="620,556" size="306,34" font="Regular;16" halign="center" valign="center" foregroundColor="#6b7684" backgroundColor="#0e1116" />

        <eLabel position="0,604" size="950,16" backgroundColor="#151a21" zPosition="-5" />
    </screen>'''

    BACKUP_ITEMS = [
        ("Backup /etc/enigma2", "/etc/enigma2"),
        ("Backup /etc/tuxbox", "/etc/tuxbox"),
        ("Backup /etc/network", "/etc/network"),
        ("Backup /etc/resolv.conf", "/etc/resolv.conf"),
        ("Backup /etc/fstab", "/etc/fstab"),
        ("Backup /usr/keys", "/usr/keys"),
        ("Backup /etc/hostname", "/etc/hostname"),
        ("Backup piconów /picon", "/picon"),
        ("Backup piconów /usr/share/enigma2/picon", "/usr/share/enigma2/picon"),
        ("Backup ulubionych ścieżek", "backup_fav"),
    ]

    def __init__(self, session):
        Screen.__init__(self, session)
        self.session = session
        self.backup_dir = get_system_backup_dir()
        self.picon_backup_dir = get_picon_backup_dir()

        self["title"] = Label(_("Backup plików systemowych"))
        self["list"] = List([])
        self["status"] = Label(_("OK - utwórz backup wybranej pozycji"))
        self["target"] = Label(_("Katalog backupu: %s") % self.backup_dir)
        self["favinfo"] = Label(_("Lista ulubionych ścieżek: %s") % FAV_LIST_FILE)
        self["key_yellow"] = Label(_("Odśwież"))
        self["key_blue"] = Label(_("Przywróć ustawienia"))
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
        location = get_storage_location()
        if not is_storage_location_available(location):
            self.session.open(
                MessageBox,
                _("Wybrana lokalizacja backupu nie jest dostępna:\n%s") % location,
                MessageBox.TYPE_ERROR,
                timeout=6
            )
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

        ts = time.strftime("%Y%m%d_%H%M%S")
        if source_path in PICON_PATHS:
            ensure_dir(self.picon_backup_dir)
            if source_path == "/picon":
                archive_name = "picon_root_%s.tar.gz" % ts
            else:
                archive_name = "picon_usr_share_enigma2_%s.tar.gz" % ts
            archive_path = os.path.join(self.picon_backup_dir, archive_name)
        else:
            ensure_dir(self.backup_dir)
            safe_name = os.path.basename(source_path.rstrip("/")) or "root"
            archive_name = "backup_%s_%s.tar.gz" % (safe_name, ts)
            archive_path = os.path.join(self.backup_dir, archive_name)

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

        ensure_dir(self.backup_dir)

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
        archive_path = os.path.join(self.backup_dir, archive_name)

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
        search_dirs = get_backup_search_dirs("system") + get_backup_search_dirs("picon")
        self.session.openWithCallback(
            self._onBackupSelected,
            ConfRestoreListScreen,
            search_dirs
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
            ("picon_root_", "/picon"),
            ("picon_usr_share_enigma2_", "/usr/share/enigma2/picon"),
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
            cmd_parts.append('rm -rf "{0}"'.format(target_path))
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
