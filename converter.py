# -*- coding: utf-8 -*-
"""
Konwerter konfiguracji OSCam <-> NCam.

Kopiuje pliki z wybranego katalogu, zamieniajac przedrostek nazwy
(oscam*.* <-> ncam*.*) i opcjonalnie takze wystapienia nazwy softcamu
wewnatrz plikow tekstowych. Pliki zrodlowe pozostaja nietkniete.
"""

import io
import os
import re
import shutil
import traceback
from threading import Thread

from twisted.internet import reactor

from enigma import (
    gFont,
    RT_HALIGN_LEFT,
    RT_HALIGN_RIGHT,
    RT_VALIGN_CENTER,
)

from Screens.Screen import Screen
from Screens.MessageBox import MessageBox

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.ProgressBar import ProgressBar
from Components.Sources.List import List

try:
    _
except NameError:
    def _(txt):
        return txt


# ---------------------------------------------------------------------------
# Stale
# ---------------------------------------------------------------------------

CONFIG_ROOT = "/etc/tuxbox/config"

OSCAM = "oscam"
NCAM = "ncam"

DIR_OSCAM_TO_NCAM = "oscam2ncam"
DIR_NCAM_TO_OSCAM = "ncam2oscam"

DIRECTIONS = {
    DIR_OSCAM_TO_NCAM: (OSCAM, NCAM),
    DIR_NCAM_TO_OSCAM: (NCAM, OSCAM),
}

# Powyzej tego rozmiaru plik jest kopiowany bez podmiany tresci.
MAX_TEXT_SIZE = 4 * 1024 * 1024

# Kanoniczna pisownia uzywana przy mieszanej wielkosci liter.
CANONICAL = {OSCAM: "OSCam", NCAM: "NCam"}

STATE_READY = "ready"
STATE_COLLISION = "collision"
STATE_SKIP = "skip"
STATE_DONE = "done"
STATE_ERROR = "error"


def direction_label(direction):
    src, dst = DIRECTIONS.get(direction, (OSCAM, NCAM))
    return "%s -> %s" % (CANONICAL[src], CANONICAL[dst])


# ---------------------------------------------------------------------------
# Silnik konwersji (czysta logika - bez zaleznosci od GUI)
# ---------------------------------------------------------------------------

def match_case(sample, replacement):
    """Dopasuj wielkosc liter *replacement* do wzorca *sample*."""
    if sample.isupper():
        return replacement.upper()
    if sample.islower():
        return replacement.lower()
    return CANONICAL.get(replacement.lower(), replacement)


def convert_filename(name, src, dst):
    """
    'ncam.server' -> 'oscam.server'.
    Zwraca None jesli nazwa nie zaczyna sie od przedrostka *src*.
    """
    if len(name) < len(src):
        return None
    if name[:len(src)].lower() != src.lower():
        return None
    return match_case(name[:len(src)], dst) + name[len(src):]


def convert_text(text, src, dst):
    """Podmien wszystkie wystapienia *src* na *dst* zachowujac wielkosc liter."""
    pattern = re.compile(re.escape(src), re.IGNORECASE)
    return pattern.sub(lambda m: match_case(m.group(0), dst), text)


def is_binary_file(path, probe=8192):
    """Heurystyka: plik zawierajacy bajt zerowy traktujemy jako binarny."""
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(probe)
    except Exception:
        return True


def count_matching_files(directory, src):
    """Ile plikow w *directory* pasuje do przedrostka *src*."""
    total = 0
    try:
        for name in os.listdir(directory):
            if convert_filename(name, src, src) is None:
                continue
            if os.path.isfile(os.path.join(directory, name)):
                total += 1
    except Exception:
        return -1
    return total


def build_plan(directory, direction):
    """
    Zbuduj liste operacji do wykonania.
    Zwraca (plan, error) gdzie plan to lista slownikow.
    """
    src, dst = DIRECTIONS.get(direction, (OSCAM, NCAM))
    plan = []
    try:
        names = sorted(os.listdir(directory))
    except Exception as e:
        return plan, str(e)

    for name in names:
        new_name = convert_filename(name, src, dst)
        if new_name is None:
            continue

        full = os.path.join(directory, name)
        target = os.path.join(directory, new_name)
        item = {
            "name": name,
            "new_name": new_name,
            "path": full,
            "target": target,
            "state": STATE_READY,
            "reason": "",
        }

        if os.path.isdir(full):
            item["state"] = STATE_SKIP
            item["reason"] = _("katalog - pomijany")
        elif not os.path.isfile(full):
            item["state"] = STATE_SKIP
            item["reason"] = _("to nie jest zwykły plik")
        elif name == new_name:
            item["state"] = STATE_SKIP
            item["reason"] = _("nazwa bez zmian")
        elif os.path.exists(target):
            item["state"] = STATE_COLLISION
            item["reason"] = _("plik docelowy już istnieje")

        plan.append(item)

    return plan, None


def convert_one(item, direction, replace_content):
    """
    Wykonaj pojedyncza operacje.
    Zwraca (ok, opis). Nie rzuca wyjatkow.
    """
    src, dst = DIRECTIONS.get(direction, (OSCAM, NCAM))
    path = item["path"]
    target = item["target"]

    try:
        size = os.path.getsize(path)
    except Exception:
        size = 0

    try:
        if replace_content and size <= MAX_TEXT_SIZE and not is_binary_file(path):
            with io.open(path, "r", encoding="utf-8",
                         errors="surrogateescape", newline="") as f:
                data = f.read()
            new_data = convert_text(data, src, dst)
            with io.open(target, "w", encoding="utf-8",
                         errors="surrogateescape", newline="") as f:
                f.write(new_data)
            try:
                shutil.copystat(path, target)
            except Exception:
                pass
            if new_data != data:
                return True, _("skopiowany, treść podmieniona")
            return True, _("skopiowany, brak zmian w treści")

        shutil.copy2(path, target)
        if replace_content:
            return True, _("skopiowany (plik binarny)")
        return True, _("skopiowany")
    except Exception as e:
        return False, str(e)


def plan_stats(plan):
    stats = {"total": len(plan), "ready": 0, "collision": 0,
             "skip": 0, "done": 0, "error": 0}
    for item in plan:
        key = item.get("state")
        if key in stats:
            stats[key] += 1
    return stats


# ---------------------------------------------------------------------------
# Ekran 1: wybor kierunku konwersji
# ---------------------------------------------------------------------------

class ConverterScreen(Screen):
    skin = """
    <screen name="ConverterScreen" position="center,center" size="820,440" title="Konwerter OSCam / NCam" backgroundColor="#0e1116">
        <eLabel position="0,0"   size="820,440" backgroundColor="#0e1116" zPosition="-10" />
        <eLabel position="0,0"   size="820,56"  backgroundColor="#151a21" zPosition="-5" />
        <eLabel position="0,56"  size="820,2"   backgroundColor="#4a9eff" />
        <eLabel position="24,16" size="4,24"    backgroundColor="#4a9eff" />
        <eLabel position="40,14" size="500,28"  font="Regular;22" halign="left" valign="center" text="Konwerter OSCam &lt;-&gt; NCam" foregroundColor="#e8eaed" backgroundColor="#151a21" />
        <widget name="mode" position="550,16" size="246,26" font="Regular;16" halign="right" valign="center" foregroundColor="#9aa4b2" backgroundColor="#151a21" />

        <eLabel position="24,76" size="772,164" backgroundColor="#12161c" zPosition="-3" />
        <widget source="list" render="Listbox" position="34,84" size="752,148" scrollbarMode="showOnDemand"
                backgroundColor="#12161c" backgroundColorSelected="#1d2735"
                foregroundColor="#e8eaed" foregroundColorSelected="#ffffff">
            <convert type="TemplatedMultiContent">
            {"template": [
                MultiContentEntryText(pos=(14,8),  size=(716,28), font=0, color=0xe8eaed, color_sel=0xffffff, flags=RT_HALIGN_LEFT|RT_VALIGN_CENTER, text=0),
                MultiContentEntryText(pos=(14,38), size=(716,24), font=1, color=0x7c8898, color_sel=0x9fb4cc, flags=RT_HALIGN_LEFT|RT_VALIGN_CENTER, text=1)
            ],
            "fonts": [gFont("Regular",23), gFont("Regular",17)],
            "itemHeight": 74
            }
            </convert>
        </widget>

        <eLabel position="24,256" size="772,1"  backgroundColor="#232a34" />
        <widget name="status" position="24,266" size="772,26" font="Regular;18" halign="center" valign="center" foregroundColor="#9aa4b2" backgroundColor="#0e1116" />
        <widget name="source" position="24,294" size="772,24" font="Regular;16" halign="center" valign="center" foregroundColor="#4a9eff" backgroundColor="#0e1116" />

        <eLabel position="24,344"  size="4,34" backgroundColor="#ff5252" />
        <widget name="key_red"    position="28,344"  size="180,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
        <eLabel position="220,344" size="4,34" backgroundColor="#3ddc84" />
        <widget name="key_green"  position="224,344" size="180,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
        <eLabel position="416,344" size="4,34" backgroundColor="#ffb020" />
        <widget name="key_yellow" position="420,344" size="180,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
        <eLabel position="612,344" size="4,34" backgroundColor="#4a9eff" />
        <widget name="key_blue"   position="616,344" size="180,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />

        <eLabel position="24,392" size="772,22" font="Regular;15" halign="center" valign="center" text="OK / Zielony - wybierz katalog i przejdź dalej   |   EXIT - powrót" foregroundColor="#6b7684" backgroundColor="#0e1116" />
        <eLabel position="0,432" size="820,8" backgroundColor="#151a21" zPosition="-5" />
    </screen>
    """

    def __init__(self, session):
        Screen.__init__(self, session)
        self.session = session
        self.replace_content = True
        self.last_dir = CONFIG_ROOT

        self["list"] = List([])
        self["mode"] = Label("")
        self["status"] = Label(_("Wybierz kierunek konwersji"))
        self["source"] = Label(_("Katalog startowy: %s") % CONFIG_ROOT)
        self["key_red"] = Label(_("Katalog: %s") % os.path.basename(CONFIG_ROOT))
        self["key_green"] = Label(_("Dalej"))
        self["key_yellow"] = Label(_("Treść plików"))
        self["key_blue"] = Label(_("Zamknij"))

        self["actions"] = ActionMap(
            ["OkCancelActions", "ColorActions", "DirectionActions"],
            {
                "ok": self.keyOk,
                "green": self.keyOk,
                "cancel": self.close,
                "back": self.close,
                "red": self.resetStartDir,
                "yellow": self.toggleContent,
                "blue": self.close,
                "up": self.keyUp,
                "down": self.keyDown,
            },
            -1,
        )

        self._buildList()
        self._refreshMode()

    # ------------------------------------------------------------------

    def _buildList(self):
        self["list"].setList([
            (
                _("OSCam  ->  NCam"),
                _("Pliki oscam*.* zostaną skopiowane jako ncam*.*"),
                DIR_OSCAM_TO_NCAM,
            ),
            (
                _("NCam  ->  OSCam"),
                _("Pliki ncam*.* zostaną skopiowane jako oscam*.*"),
                DIR_NCAM_TO_OSCAM,
            ),
        ])

    def _refreshMode(self):
        self["mode"].setText(
            _("Podmiana treści: TAK") if self.replace_content
            else _("Podmiana treści: NIE")
        )
        self["source"].setText(_("Katalog startowy: %s") % self.last_dir)

    def toggleContent(self):
        self.replace_content = not self.replace_content
        self._refreshMode()

    def resetStartDir(self):
        self.last_dir = CONFIG_ROOT
        self._refreshMode()
        self["status"].setText(_("Katalog startowy przywrócony"))

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

    # ------------------------------------------------------------------

    def keyOk(self):
        sel = self["list"].getCurrent()
        if not sel:
            return
        direction = sel[2]
        src, _dst = DIRECTIONS[direction]
        self["status"].setText(_("Wskaż katalog z plikami %s*.*") % src)
        self.session.openWithCallback(
            lambda path: self._afterDirChosen(path, direction),
            ConverterBrowserScreen,
            self.last_dir,
            src,
        )

    def _afterDirChosen(self, path, direction):
        if not path:
            self["status"].setText(_("Anulowano wybór katalogu"))
            return
        self.last_dir = path
        self._refreshMode()
        self.session.openWithCallback(
            self._afterConversion,
            ConverterPlanScreen,
            path,
            direction,
            self.replace_content,
        )

    def _afterConversion(self, *args):
        self["status"].setText(_("Wybierz kierunek konwersji"))


# ---------------------------------------------------------------------------
# Ekran 2: przegladarka katalogow
# ---------------------------------------------------------------------------

class ConverterBrowserScreen(Screen):
    skin = """
    <screen name="ConverterBrowserScreen" position="center,center" size="900,560" title="Wybierz katalog" backgroundColor="#0e1116">
        <eLabel position="0,0"   size="900,560" backgroundColor="#0e1116" zPosition="-10" />
        <eLabel position="0,0"   size="900,56"  backgroundColor="#151a21" zPosition="-5" />
        <eLabel position="0,56"  size="900,2"   backgroundColor="#4a9eff" />
        <eLabel position="24,16" size="4,24"    backgroundColor="#4a9eff" />
        <widget name="path" position="40,14" size="836,28" font="Regular;20" halign="left" valign="center" foregroundColor="#e8eaed" backgroundColor="#151a21" />

        <eLabel position="24,76" size="852,336" backgroundColor="#12161c" zPosition="-3" />
        <widget source="list" render="Listbox" position="34,84" size="832,320" scrollbarMode="showOnDemand"
                backgroundColor="#12161c" backgroundColorSelected="#1d2735"
                foregroundColor="#e8eaed" foregroundColorSelected="#ffffff">
            <convert type="TemplatedMultiContent">
            {"template": [
                MultiContentEntryText(pos=(14,0),  size=(600,44), font=0, color=0xe8eaed, color_sel=0xffffff, flags=RT_HALIGN_LEFT|RT_VALIGN_CENTER, text=0),
                MultiContentEntryText(pos=(620,0), size=(180,44), font=1, color=0x3ddc84, color_sel=0x3ddc84, flags=RT_HALIGN_RIGHT|RT_VALIGN_CENTER, text=1)
            ],
            "fonts": [gFont("Regular",21), gFont("Regular",16)],
            "itemHeight": 44
            }
            </convert>
        </widget>

        <eLabel position="24,424" size="852,1"  backgroundColor="#232a34" />
        <widget name="status" position="24,434" size="852,26" font="Regular;18" halign="center" valign="center" foregroundColor="#9aa4b2" backgroundColor="#0e1116" />

        <eLabel position="24,476"  size="4,34" backgroundColor="#ff5252" />
        <widget name="key_red"    position="28,476"  size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
        <eLabel position="240,476" size="4,34" backgroundColor="#3ddc84" />
        <widget name="key_green"  position="244,476" size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
        <eLabel position="456,476" size="4,34" backgroundColor="#ffb020" />
        <widget name="key_yellow" position="460,476" size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
        <eLabel position="672,476" size="4,34" backgroundColor="#4a9eff" />
        <widget name="key_blue"   position="676,476" size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />

        <eLabel position="24,518" size="852,22" font="Regular;15" halign="center" valign="center" text="OK - wejdź do katalogu   |   Zielony - wybierz bieżący katalog   |   EXIT - anuluj" foregroundColor="#6b7684" backgroundColor="#0e1116" />
        <eLabel position="0,552" size="900,8" backgroundColor="#151a21" zPosition="-5" />
    </screen>
    """

    # Powyzej tylu podkatalogow nie liczymy pasujacych plikow (wydajnosc).
    COUNT_LIMIT = 80

    def __init__(self, session, start_dir=CONFIG_ROOT, src_prefix=NCAM):
        Screen.__init__(self, session)
        self.session = session
        self.src_prefix = src_prefix
        self.current = start_dir if os.path.isdir(start_dir) else "/"

        self["list"] = List([])
        self["path"] = Label(self.current)
        self["status"] = Label("")
        self["key_red"] = Label(_("Katalog nadrzędny"))
        self["key_green"] = Label(_("Wybierz katalog"))
        self["key_yellow"] = Label(_("Odśwież"))
        self["key_blue"] = Label(_("Anuluj"))

        self["actions"] = ActionMap(
            ["OkCancelActions", "ColorActions", "DirectionActions"],
            {
                "ok": self.keyOk,
                "cancel": self.keyCancel,
                "back": self.keyCancel,
                "red": self.goParent,
                "green": self.selectCurrent,
                "yellow": self.refresh,
                "blue": self.keyCancel,
                "up": self.keyUp,
                "down": self.keyDown,
                "left": self.goParent,
                "right": self.keyOk,
            },
            -1,
        )

        self.refresh()

    # ------------------------------------------------------------------

    def refresh(self):
        entries = []
        parent = os.path.dirname(self.current.rstrip("/")) or "/"
        if self.current.rstrip("/") != "":
            entries.append((_(".. (katalog nadrzędny)"), "", parent))

        try:
            names = sorted(
                n for n in os.listdir(self.current)
                if os.path.isdir(os.path.join(self.current, n))
            )
            error = None
        except Exception as e:
            names, error = [], str(e)

        show_counts = len(names) <= self.COUNT_LIMIT
        for name in names:
            full = os.path.join(self.current, name)
            info = ""
            if show_counts:
                found = count_matching_files(full, self.src_prefix)
                if found > 0:
                    info = _("%d x %s*") % (found, self.src_prefix)
                elif found < 0:
                    info = _("brak dostępu")
            entries.append((name, info, full))

        self["list"].setList(entries)
        self["path"].setText(self.current)

        here = count_matching_files(self.current, self.src_prefix)
        if error:
            self["status"].setText(_("Błąd odczytu katalogu: %s") % error)
        elif here > 0:
            self["status"].setText(
                _("W tym katalogu: %d plików %s*.* - Zielony rozpoczyna konwersję")
                % (here, self.src_prefix)
            )
        elif here == 0:
            self["status"].setText(
                _("W tym katalogu nie ma plików %s*.*") % self.src_prefix
            )
        else:
            self["status"].setText(_("Brak dostępu do katalogu"))

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

    def keyOk(self):
        sel = self["list"].getCurrent()
        if not sel:
            return
        target = sel[2]
        if os.path.isdir(target):
            self.current = target
            self.refresh()

    def goParent(self):
        parent = os.path.dirname(self.current.rstrip("/")) or "/"
        if parent != self.current:
            self.current = parent
            self.refresh()

    def selectCurrent(self):
        self.close(self.current)

    def keyCancel(self):
        self.close(None)


# ---------------------------------------------------------------------------
# Ekran 3: plan konwersji + wykonanie
# ---------------------------------------------------------------------------

class ConverterPlanScreen(Screen):
    skin = """
    <screen name="ConverterPlanScreen" position="center,center" size="900,600" title="Konwersja plikow" backgroundColor="#0e1116">
        <eLabel position="0,0"   size="900,600" backgroundColor="#0e1116" zPosition="-10" />
        <eLabel position="0,0"   size="900,58"  backgroundColor="#151a21" zPosition="-5" />
        <eLabel position="0,58"  size="900,2"   backgroundColor="#4a9eff" />
        <eLabel position="24,17" size="4,24"    backgroundColor="#4a9eff" />
        <widget name="title" position="40,15" size="520,28" font="Regular;22" halign="left"  valign="center" foregroundColor="#e8eaed" backgroundColor="#151a21" />
        <widget name="mode"  position="570,17" size="306,24" font="Regular;16" halign="right" valign="center" foregroundColor="#9aa4b2" backgroundColor="#151a21" />

        <widget name="source" position="24,72" size="852,24" font="Regular;16" halign="left" valign="center" foregroundColor="#4a9eff" backgroundColor="#0e1116" />

        <eLabel position="24,104" size="852,290" backgroundColor="#12161c" zPosition="-3" />
        <widget source="list" render="Listbox" position="34,112" size="832,274" scrollbarMode="showOnDemand"
                backgroundColor="#12161c" backgroundColorSelected="#1d2735"
                foregroundColor="#e8eaed" foregroundColorSelected="#ffffff">
            <convert type="TemplatedMultiContent">
            {"template": [
                MultiContentEntryText(pos=(14,6),  size=(578,26), font=0, color=0xe8eaed, color_sel=0xffffff, flags=RT_HALIGN_LEFT|RT_VALIGN_CENTER,  text=0),
                MultiContentEntryText(pos=(600,6), size=(198,26), font=1, color=0x3ddc84, color_sel=0x3ddc84, flags=RT_HALIGN_RIGHT|RT_VALIGN_CENTER, text=1),
                MultiContentEntryText(pos=(600,6), size=(198,26), font=1, color=0xffb020, color_sel=0xffb020, flags=RT_HALIGN_RIGHT|RT_VALIGN_CENTER, text=2),
                MultiContentEntryText(pos=(600,6), size=(198,26), font=1, color=0xff5252, color_sel=0xff5252, flags=RT_HALIGN_RIGHT|RT_VALIGN_CENTER, text=3),
                MultiContentEntryText(pos=(14,32), size=(784,20), font=2, color=0x7c8898, color_sel=0x9fb4cc, flags=RT_HALIGN_LEFT|RT_VALIGN_CENTER,  text=4)
            ],
            "fonts": [gFont("Regular",21), gFont("Regular",16), gFont("Regular",15)],
            "itemHeight": 58
            }
            </convert>
        </widget>

        <eLabel position="24,410" size="852,1"  backgroundColor="#232a34" />
        <eLabel position="24,422" size="852,12" backgroundColor="#1a2028" zPosition="-3" />
        <widget name="progress" position="24,422" size="852,12" borderWidth="0" backgroundColor="#1a2028" foregroundColor="#4a9eff" />

        <widget name="counters" position="24,444" size="852,26" font="Regular;17" halign="center" valign="center" foregroundColor="#9aa4b2" backgroundColor="#0e1116" />
        <widget name="status"   position="24,472" size="852,26" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#0e1116" />

        <eLabel position="24,516"  size="4,34" backgroundColor="#ff5252" />
        <widget name="key_red"    position="28,516"  size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
        <eLabel position="240,516" size="4,34" backgroundColor="#3ddc84" />
        <widget name="key_green"  position="244,516" size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
        <eLabel position="456,516" size="4,34" backgroundColor="#ffb020" />
        <widget name="key_yellow" position="460,516" size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
        <eLabel position="672,516" size="4,34" backgroundColor="#4a9eff" />
        <widget name="key_blue"   position="676,516" size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />

        <eLabel position="24,558" size="852,22" font="Regular;15" halign="center" valign="center" text="Zielony - rozpocznij konwersję   |   Żółty - traktowanie kolizji   |   EXIT - powrót" foregroundColor="#6b7684" backgroundColor="#0e1116" />
        <eLabel position="0,592" size="900,8" backgroundColor="#151a21" zPosition="-5" />
    </screen>
    """

    def __init__(self, session, directory, direction, replace_content=True):
        Screen.__init__(self, session)
        self.session = session
        self.directory = directory
        self.direction = direction
        self.replace_content = replace_content
        self.overwrite = False
        self.plan = []
        self.running = False
        self.finished = False

        self["list"] = List([])
        self["title"] = Label(_("Konwersja: %s") % direction_label(direction))
        self["mode"] = Label("")
        self["source"] = Label(_("Katalog: %s") % directory)
        self["progress"] = ProgressBar()
        self["counters"] = Label("")
        self["status"] = Label(_("Analiza katalogu..."))
        self["key_red"] = Label(_("Anuluj"))
        self["key_green"] = Label(_("Konwertuj"))
        self["key_yellow"] = Label(_("Kolizje: pomijaj"))
        self["key_blue"] = Label(_("Zamknij"))

        self["progress"].setValue(0)

        self["actions"] = ActionMap(
            ["OkCancelActions", "ColorActions", "DirectionActions"],
            {
                "ok": self.keyGreen,
                "green": self.keyGreen,
                "cancel": self.keyClose,
                "back": self.keyClose,
                "red": self.keyClose,
                "yellow": self.toggleOverwrite,
                "blue": self.keyClose,
                "up": self.keyUp,
                "down": self.keyDown,
            },
            -1,
        )

        self._refreshMode()
        self.onShown.append(self._startAnalysis)

    # ------------------------------------------------------------------
    # Analiza
    # ------------------------------------------------------------------

    def _startAnalysis(self):
        if self.plan or self.running:
            return
        Thread(target=self._analysisWorker).start()

    def _analysisWorker(self):
        try:
            plan, error = build_plan(self.directory, self.direction)
            reactor.callFromThread(self._applyPlan, plan, error)
        except Exception as e:
            traceback.print_exc()
            reactor.callFromThread(self._applyPlan, [], str(e))

    def _applyPlan(self, plan, error):
        self.plan = plan
        self._renderPlan()
        if error:
            self["status"].setText(_("Błąd odczytu katalogu: %s") % error)
            return
        stats = plan_stats(plan)
        if stats["total"] == 0:
            src, _dst = DIRECTIONS[self.direction]
            self["status"].setText(
                _("Nie znaleziono plików %s*.* w tym katalogu") % src
            )
        elif stats["ready"] == 0:
            self["status"].setText(_("Brak plików gotowych do konwersji"))
        else:
            self["status"].setText(
                _("Gotowe do konwersji: %d - naciśnij Zielony") % stats["ready"]
            )

    # ------------------------------------------------------------------
    # Prezentacja
    # ------------------------------------------------------------------

    def _rowFor(self, item):
        """(tytul, ok, ostrzezenie, blad, detale)"""
        title = "%s   ->   %s" % (item["name"], item["new_name"])
        state = item["state"]
        ok = warn = err = ""
        if state == STATE_READY:
            warn = _("do konwersji")
        elif state == STATE_COLLISION:
            warn = _("KOLIZJA") if not self.overwrite else _("nadpisze")
        elif state == STATE_SKIP:
            warn = _("pominięty")
        elif state == STATE_DONE:
            ok = _("gotowe")
        elif state == STATE_ERROR:
            err = _("błąd")
        return (title, ok, warn, err, item.get("reason", ""))

    def _renderPlan(self):
        if not self.plan:
            self["list"].setList([(
                _("Brak pasujących plików"), "", "",
                "", _("Wybierz inny katalog lub zmień kierunek konwersji"),
            )])
        else:
            self["list"].setList([self._rowFor(i) for i in self.plan])
        self._refreshCounters()

    def _refreshCounters(self):
        s = plan_stats(self.plan)
        self["counters"].setText(
            _("Znaleziono: %d   |   do konwersji: %d   |   kolizje: %d   |   pominięte: %d   |   gotowe: %d   |   błędy: %d")
            % (s["total"], s["ready"], s["collision"], s["skip"], s["done"], s["error"])
        )

    def _refreshMode(self):
        parts = [
            _("Treść: TAK") if self.replace_content else _("Treść: NIE"),
            _("Kolizje: nadpisuj") if self.overwrite else _("Kolizje: pomijaj"),
        ]
        self["mode"].setText("   |   ".join(parts))
        self["key_yellow"].setText(
            _("Kolizje: nadpisuj") if self.overwrite else _("Kolizje: pomijaj")
        )

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

    def toggleOverwrite(self):
        if self.running:
            return
        self.overwrite = not self.overwrite
        self._refreshMode()
        self._renderPlan()

    # ------------------------------------------------------------------
    # Wykonanie
    # ------------------------------------------------------------------

    def _pending(self):
        states = (STATE_READY, STATE_COLLISION) if self.overwrite else (STATE_READY,)
        return [i for i in self.plan if i["state"] in states]

    def keyGreen(self):
        if self.running:
            return
        if self.finished:
            self.close()
            return
        pending = self._pending()
        if not pending:
            self.session.open(
                MessageBox,
                _("Nie ma plików do skonwertowania."),
                MessageBox.TYPE_INFO,
                timeout=4,
            )
            return

        stats = plan_stats(self.plan)
        msg = _("Skonwertować %d plików?\n\nKatalog: %s\nKierunek: %s\nPodmiana treści: %s") % (
            len(pending),
            self.directory,
            direction_label(self.direction),
            _("tak") if self.replace_content else _("nie"),
        )
        if stats["collision"]:
            msg += "\n" + (
                _("Kolizje (%d) zostaną nadpisane.") if self.overwrite
                else _("Kolizje (%d) zostaną pominięte.")
            ) % stats["collision"]
        msg += "\n\n" + _("Pliki źródłowe pozostaną nietknięte.")

        self.session.openWithCallback(self._confirmRun, MessageBox, msg, MessageBox.TYPE_YESNO)

    def _confirmRun(self, answer):
        if answer:
            self._run()

    def _run(self):
        pending = self._pending()
        if not pending:
            return
        self.running = True
        self["key_green"].setText(_("Pracuję..."))
        self["status"].setText(_("Konwertowanie..."))
        self["progress"].setValue(0)
        Thread(target=self._runWorker, args=(pending,)).start()

    def _runWorker(self, pending):
        total = len(pending)
        for index, item in enumerate(pending, 1):
            try:
                ok, reason = convert_one(item, self.direction, self.replace_content)
            except Exception as e:
                ok, reason = False, str(e)
            item["state"] = STATE_DONE if ok else STATE_ERROR
            item["reason"] = reason
            reactor.callFromThread(
                self._progressUpdate, int(index * 100.0 / total), item["name"]
            )
        reactor.callFromThread(self._runFinished)

    def _progressUpdate(self, percent, name):
        try:
            self["progress"].setValue(max(0, min(100, percent)))
            self["status"].setText(_("Konwertowanie: %s") % name)
            self._renderPlan()
        except Exception:
            pass

    def _runFinished(self):
        self.running = False
        self.finished = True
        self["progress"].setValue(100)
        self._renderPlan()
        s = plan_stats(self.plan)
        self["key_green"].setText(_("Zamknij"))
        self["key_red"].setText(_("Zamknij"))
        if s["error"]:
            self["status"].setText(
                _("Zakończono z błędami: %d gotowych, %d błędów") % (s["done"], s["error"])
            )
        else:
            self["status"].setText(_("Zakończono - skonwertowano %d plików") % s["done"])

    def keyClose(self):
        if self.running:
            self.session.open(
                MessageBox,
                _("Konwersja w toku - poczekaj na zakończenie."),
                MessageBox.TYPE_INFO,
                timeout=4,
            )
            return
        self.close()
