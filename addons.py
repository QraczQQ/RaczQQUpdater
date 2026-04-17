# -*- coding: utf-8 -*-

import os

from enigma import (
    gFont,
    RT_HALIGN_LEFT,
    RT_VALIGN_CENTER,
)

from Screens.Console import Console
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.Sources.List import List
from Components.Sources.StaticText import StaticText

try:
    _
except NameError:
    def _(txt):
        return txt

ADDONS_DIR = "/data/RaczQQUpdater/files"


# ---------------------------------------------------------------------------
# AddonsScreen
# ---------------------------------------------------------------------------

class AddonsScreen(Screen):
    skin = """
    <screen name="AddonsScreen" position="center,center" size="900,560"
            title="Instalacja dodatków">

        <widget source="key_red" render="Label"
                position="20,15" size="190,35"
                font="Regular;24" halign="center" valign="center"
                backgroundColor="red" transparent="1" />
        <widget source="key_green" render="Label"
                position="230,15" size="190,35"
                font="Regular;24" halign="center" valign="center"
                backgroundColor="green" transparent="1" />
        <widget source="key_yellow" render="Label"
                position="440,15" size="190,35"
                font="Regular;24" halign="center" valign="center"
                backgroundColor="yellow" transparent="1" />
        <widget source="key_blue" render="Label"
                position="650,15" size="190,35"
                font="Regular;24" halign="center" valign="center"
                backgroundColor="blue" transparent="1" />

        <widget source="info" render="Label"
                position="20,60" size="860,28"
                font="Regular;21" halign="left" valign="center" />

        <widget source="list" render="Listbox"
                position="20,100" size="860,400"
                scrollbarMode="showOnDemand">
            <convert type="TemplatedMultiContent">
                {
                    "template": [
                        MultiContentEntryText(
                            pos=(10, 10), size=(840, 34),
                            font=0, flags=RT_HALIGN_LEFT | RT_VALIGN_CENTER,
                            text=0)
                    ],
                    "fonts": [gFont("Regular", 26)],
                    "itemHeight": 52
                }
            </convert>
        </widget>

        <widget source="status" render="Label"
                position="20,512" size="860,30"
                font="Regular;20" halign="center" valign="center" />
    </screen>
    """

    def __init__(self, session):
        Screen.__init__(self, session)
        self.session = session

        self["key_red"]    = StaticText(_("Instaluj wszystkie"))
        self["key_green"]  = StaticText(_("Instaluj"))
        self["key_yellow"] = StaticText(_("Odśwież"))
        self["key_blue"]   = StaticText(_("Zamknij"))
        self["info"]       = StaticText(_("Katalog: %s") % ADDONS_DIR)
        self["status"]     = StaticText(_("OK / Zielony = instaluj  |  Czerwony = instaluj wszystkie  |  EXIT = zamknij"))
        self["list"]       = List([])

        self["actions"] = ActionMap(
            ["ColorActions", "OkCancelActions"],
            {
                "red":    self.install_all,
                "green":  self.install_selected,
                "yellow": self.refresh_list,
                "blue":   self.close,
                "ok":     self.install_selected,
                "cancel": self.close,
            },
            -1,
        )

        self._ensure_dir()
        self.refresh_list()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_dir(self):
        if not os.path.isdir(ADDONS_DIR):
            try:
                os.makedirs(ADDONS_DIR)
            except OSError as e:
                print("[AddonsScreen] Nie można utworzyć katalogu: %s" % e)

    def _file_size_str(self, path):
        try:
            size = os.path.getsize(path)
            if size >= 1024 * 1024:
                return "%.1f MB" % (size / (1024.0 * 1024.0))
            elif size >= 1024:
                return "%.1f KB" % (size / 1024.0)
            return "%d B" % size
        except Exception:
            return ""

    def _get_ipk_items(self):
        """Return list of (fname, fullpath) for all *.ipk files in ADDONS_DIR."""
        result = []
        try:
            all_files = sorted(os.listdir(ADDONS_DIR))
        except Exception:
            return result
        for fname in all_files:
            if not fname.lower().endswith(".ipk"):
                continue
            fullpath = os.path.join(ADDONS_DIR, fname)
            if os.path.isfile(fullpath):
                result.append((fname, fullpath))
        return result

    # ------------------------------------------------------------------
    # List management
    # ------------------------------------------------------------------

    def refresh_list(self):
        """Scan ADDONS_DIR for *.ipk files and populate the list widget."""
        ipk_items = self._get_ipk_items()
        items = []

        for fname, fullpath in ipk_items:
            size_str = self._file_size_str(fullpath)
            # Display only filename + size — no directory path
            display = "%s  [%s]" % (fname, size_str) if size_str else fname
            # tuple layout: (display_name, full_path)
            items.append((display, fullpath))

        if items:
            self["info"].setText(
                _("Znaleziono %d plik(ów) .ipk w: %s") % (len(items), ADDONS_DIR)
            )
        else:
            self["info"].setText(_("Brak plików .ipk w: %s") % ADDONS_DIR)
            items.append((_("Brak plików *.ipk  —  skopiuj pliki do: %s") % ADDONS_DIR, ""))

        self["list"].setList(items)

    # ------------------------------------------------------------------
    # Installation – single
    # ------------------------------------------------------------------

    def install_selected(self):
        sel = self["list"].getCurrent()
        if not sel or not sel[1]:
            self.session.open(
                MessageBox,
                _("Brak wybranego pliku do instalacji."),
                MessageBox.TYPE_INFO,
                timeout=4,
            )
            return

        ipk_path = sel[1]
        fname    = os.path.basename(ipk_path)

        self.session.openWithCallback(
            lambda answer: self._confirm_install(answer, ipk_path, fname),
            MessageBox,
            _("Zainstalować pakiet?\n\n%s") % fname,
            MessageBox.TYPE_YESNO,
        )

    def _confirm_install(self, answer, ipk_path, fname):
        if not answer:
            return
        cmd = 'opkg install --force-reinstall "{path}" && echo "--- OK ---" || echo "--- BLAD ---"'.format(
            path=ipk_path
        )

        def on_finish(*args):
            self.session.open(
                MessageBox,
                _("Instalacja zakończona:\n%s\n\nSprawdź log powyżej.") % fname,
                MessageBox.TYPE_INFO,
                timeout=6,
            )

        self.session.openWithCallback(
            on_finish,
            Console,
            title=_("Instalacja: %s") % fname,
            cmdlist=[cmd],
            closeOnSuccess=False,
        )

    # ------------------------------------------------------------------
    # Installation – all
    # ------------------------------------------------------------------

    def install_all(self):
        ipk_items = self._get_ipk_items()
        if not ipk_items:
            self.session.open(
                MessageBox,
                _("Brak plików .ipk do instalacji w:\n%s") % ADDONS_DIR,
                MessageBox.TYPE_INFO,
                timeout=4,
            )
            return

        names = "\n".join(fname for fname, _ in ipk_items)
        self.session.openWithCallback(
            lambda answer: self._confirm_install_all(answer, ipk_items),
            MessageBox,
            _("Zainstalować wszystkie pakiety (%d)?\n\n%s") % (len(ipk_items), names),
            MessageBox.TYPE_YESNO,
        )

    def _confirm_install_all(self, answer, ipk_items):
        if not answer:
            return

        # Build a single shell command that installs packages one by one
        # and prints a separator between each so the log is readable.
        parts = []
        for fname, fullpath in ipk_items:
            parts.append(
                'echo "=== Instalacja: {fname} ===" && '
                'opkg install --force-reinstall "{path}" && '
                'echo "--- OK: {fname} ---" || echo "--- BLAD: {fname} ---"'.format(
                    fname=fname, path=fullpath
                )
            )
        cmd = " ; ".join(parts) + ' ; echo "=== Wszystkie pakiety przetworzone ==="'

        def on_finish(*args):
            self.session.open(
                MessageBox,
                _("Instalacja wszystkich pakietów zakończona.\n\nSprawdź log powyżej."),
                MessageBox.TYPE_INFO,
                timeout=6,
            )

        self.session.openWithCallback(
            on_finish,
            Console,
            title=_("Instalacja wszystkich dodatków"),
            cmdlist=[cmd],
            closeOnSuccess=False,
        )