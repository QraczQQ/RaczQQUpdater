# -*- coding: utf-8 -*-

import datetime
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import traceback
import zipfile
from threading import Thread
from xml.etree import ElementTree

from twisted.internet import reactor

from enigma import (
    eDVBDB,
    eTimer,
    getDesktop,
    gFont,
    RT_HALIGN_LEFT,
    RT_VALIGN_CENTER,
    RT_VALIGN_TOP,
    BT_SCALE,
    BT_KEEP_ASPECT_RATIO,
)

from Screens.Console import Console
from Screens.InfoBar import InfoBar
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Screens.Standby import TryQuitMainloop

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.Sources.List import List
from Components.Sources.StaticText import StaticText
from Components.ProgressBar import ProgressBar

from Tools.Directories import SCOPE_PLUGINS, resolveFilename
from Tools.LoadPixmap import LoadPixmap

from Plugins.Plugin import PluginDescriptor
from skin import parseColor

try:
    _
except NameError:
    def _(txt):
        return txt

PLUGIN_VERSION = "1.2.5"

# ---------------------------------------------------------------------------
# Paleta interfejsu (dark modern)
# ---------------------------------------------------------------------------
UI_TEXT_MUTED = "#9aa4b2"   # zwykły status
UI_TEXT_ALERT = "#ff5252"   # dostępna aktualizacja
UI_TEXT_WARN  = "#ffb020"   # błąd / ostrzeżenie

PLUGIN_PATH = resolveFilename(SCOPE_PLUGINS) + "Extensions/RaczQQUpdater/"
PLUGIN_TMP_PATH = "/tmp/RaczQQUpdater/"

if PLUGIN_PATH not in sys.path:
    sys.path.append(PLUGIN_PATH)

from picony import PiconyScreen
from conf_backup import ConfBackupScreen
from addons import AddonsScreen


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ensure_str(val):
    """Return *val* as a str. Never raises."""
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", "ignore")
    try:
        return str(val)
    except Exception:
        return ""

ensure_unicode = ensure_str  # legacy alias


def read_text_file(path, default="", encoding="utf-8"):
    try:
        with io.open(path, "r", encoding=encoding, errors="ignore") as f:
            return f.read()
    except Exception:
        return default


def read_first_line(path, default="", encoding="utf-8"):
    try:
        with io.open(path, "r", encoding=encoding, errors="ignore") as f:
            value = f.readline().strip()
        return value if value else default
    except Exception:
        return default


def prepare_tmp_dir():
    if not os.path.exists(PLUGIN_TMP_PATH):
        try:
            os.makedirs(PLUGIN_TMP_PATH)
        except OSError as e:
            print("[RaczQQ Updater] Error creating tmp dir:", e)


def run_wget(url, dest, timeout=20):
    """
    Download *url* to *dest* using wget via subprocess (not os.system).
    Returns True on success, False on failure.
    """
    cmd = [
        "wget",
        "--prefer-family=IPv4",
        "--no-check-certificate",
        "-U", "Enigma2",
        "-q",
        "-T", str(timeout),
        "-O", dest,
        url,
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            print("[RaczQQ Updater] wget error ({}): {}".format(
                result.returncode, result.stderr.decode("utf-8", "ignore").strip()))
            return False
        return os.path.exists(dest) and os.path.getsize(dest) > 0
    except Exception as e:
        print("[RaczQQ Updater] wget exception:", e)
        return False


def run_command_in_background(session, title, cmd_list, callback_on_finish=None):
    def _finished(*args):
        if callback_on_finish:
            try:
                callback_on_finish()
            except Exception:
                traceback.print_exc()

    session.openWithCallback(
        _finished,
        Console,
        title=title,
        cmdlist=cmd_list,
        closeOnSuccess=True,
    )


def reload_enigma_settings():
    """Reload Enigma2 service list / bouquets from the main reactor thread."""
    try:
        db = eDVBDB.getInstance()
        db.removeServices()
        db.reloadServicelist()
        db.reloadBouquets()
        if InfoBar.instance is not None:
            servicelist = InfoBar.instance.servicelist
            root = servicelist.getRoot()
            currentref = servicelist.getCurrentSelection()
            servicelist.setRoot(root)
            servicelist.setCurrentSelection(currentref)
    except Exception:
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Manifest / channel-list fetching
# ---------------------------------------------------------------------------

def _build_action(item_type, url, bouquet_id, name):
    """
    Encode action metadata as a JSON string to avoid fragile colon-splitting
    when URLs themselves contain colons (e.g. https://).
    """
    return json.dumps({
        "type": item_type,
        "url": url,
        "bouquet_id": bouquet_id,
        "name": name,
    }, ensure_ascii=False)


def _parse_action(action_str):
    """
    Decode an action string produced by _build_action.
    Falls back to the legacy colon-encoded format for backwards compatibility.
    Returns a dict with keys: type, url, bouquet_id, name.
    """
    # New JSON format
    if action_str.startswith("{"):
        try:
            return json.loads(action_str)
        except Exception:
            pass

    # Legacy format: "type:..." – kept for any cached data
    if action_str.startswith("archive:"):
        return {"type": "archive", "url": action_str[8:], "bouquet_id": "", "name": ""}

    for prefix in ("m3u:", "bouquet:"):
        if action_str.startswith(prefix):
            item_type = prefix.rstrip(":")
            rest = action_str[len(prefix):]
            # URL ends before the second-to-last colon segment
            parts = rest.split(":")
            # parts[0] is protocol (https / http), parts[1] is //host/...
            # Reconstruct: everything up to the last two colon-delimited tokens
            # is the URL; last two tokens are bouquet_id and name.
            if len(parts) >= 4:
                url = parts[0] + ":" + parts[1]
                bouquet_id = parts[2]
                name = ":".join(parts[3:])
            else:
                url, bouquet_id, name = rest, "", ""
            return {"type": item_type, "url": url, "bouquet_id": bouquet_id, "name": name}

    return {"type": "unknown", "url": "", "bouquet_id": "", "name": ""}


def _get_lists_from_repo_sync():
    manifest_url = "https://raw.githubusercontent.com/OliOli2013/PanelAIO-Lists/main/manifest.json"
    tmp_json_path = os.path.join(PLUGIN_TMP_PATH, "manifest.json")
    prepare_tmp_dir()

    if not run_wget(manifest_url, tmp_json_path, timeout=20):
        print("[RaczQQ Updater] Błąd pobierania manifest.json")
        return []

    lists_menu = []
    try:
        with io.open(tmp_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        for item in data:
            item_type = item.get("type", "LIST").upper()
            name = item.get("name", "Brak nazwy")
            author = item.get("author", "")
            url = item.get("url", "")

            if not url:
                continue

            if item_type == "M3U":
                bouquet_id = item.get("bouquet_id", "userbouquet.imported_m3u.tv")
                menu_title = "{} - {} (Dodaj Bukiet M3U)".format(name, author)
                action = _build_action("m3u", url, bouquet_id, name)
                lists_menu.append((menu_title, action))
            elif item_type == "BOUQUET":
                bouquet_id = item.get("bouquet_id", "userbouquet.imported_ref.tv")
                menu_title = "{} - {} (Dodaj Bukiet REF)".format(name, author)
                action = _build_action("bouquet", url, bouquet_id, name)
                lists_menu.append((menu_title, action))
            else:
                version = item.get("version", "")
                menu_title = "{} - {} ({})".format(name, author, version)
                action = _build_action("archive", url, "", name)
                lists_menu.append((menu_title, action))

    except Exception as e:
        print("[RaczQQ Updater] Błąd przetwarzania pliku manifest.json:", e)
        return []

    return lists_menu


# ---------------------------------------------------------------------------
# SatellitesUpdateProgress
# ---------------------------------------------------------------------------

class SatellitesUpdateProgress(Screen):
    skin = '''
    <screen name="SatellitesUpdateProgress" position="center,center" size="720,210" title="Aktualizacja satellites.xml" backgroundColor="#0e1116">
        <eLabel position="0,0"  size="720,210" backgroundColor="#0e1116" zPosition="-10" />
        <eLabel position="0,0"  size="720,54"  backgroundColor="#151a21" zPosition="-5" />
        <eLabel position="0,54" size="720,2"   backgroundColor="#4a9eff" />
        <eLabel position="24,15" size="4,24"   backgroundColor="#4a9eff" />

        <widget name="title"  position="40,12" size="656,30" font="Regular;23" halign="left"   valign="center" foregroundColor="#e8eaed" backgroundColor="#151a21" />
        <widget name="status" position="24,70" size="672,26" font="Regular;19" halign="center" valign="center" foregroundColor="#9aa4b2" backgroundColor="#0e1116" />

        <eLabel position="24,110" size="672,16" backgroundColor="#1a2028" zPosition="-3" />
        <widget name="progress" position="24,110" size="672,16" borderWidth="0" backgroundColor="#1a2028" foregroundColor="#4a9eff" />

        <widget name="percent" position="24,138" size="672,24" font="Regular;19" halign="center" valign="center" foregroundColor="#4a9eff" backgroundColor="#0e1116" />

        <eLabel position="24,176" size="672,1"  backgroundColor="#232a34" />
        <eLabel position="24,182" size="672,22" font="Regular;15" halign="center" valign="center" text="Trwa aktualizacja - proszę czekać" foregroundColor="#6b7684" backgroundColor="#0e1116" />
    </screen>'''

    def __init__(self, session, plugin_screen):
        Screen.__init__(self, session)
        self.session = session
        self.plugin_screen = plugin_screen

        self["title"] = Label(_("Aktualizacja satellites.xml"))
        self["status"] = Label(_("Przygotowanie..."))
        self["progress"] = ProgressBar()
        self["percent"] = Label("0%")
        self["progress"].setValue(0)

        self["actions"] = ActionMap(
            ["OkCancelActions"],
            {"cancel": self.blockClose, "back": self.blockClose},
            -1,
        )

        self._restart_timer = eTimer()
        try:
            self._restart_timer.timeout.connect(self._doRestart)
        except Exception:
            self._restart_timer.callback.append(self._doRestart)

        self.onShown.append(self.startUpdate)

    def blockClose(self):
        pass

    def setProgress(self, value, status_text=None):
        try:
            value = max(0, min(100, int(value)))
        except Exception:
            value = 0
        self["progress"].setValue(value)
        self["percent"].setText("%d%%" % value)
        if status_text is not None:
            self["status"].setText(status_text)

    def startUpdate(self):
        self.plugin_screen._run_sat_update_with_progress(self)

    def finishError(self, msg):
        self.close()
        self.session.open(
            MessageBox,
            _("Błąd aktualizacji satellites.xml:\n%s") % msg,
            MessageBox.TYPE_ERROR,
            timeout=8,
        )

    def finishSuccess(self, ver):
        self.setProgress(100, _("Zakończono"))
        try:
            self.plugin_screen["update"].setText(_("satellites.xml zaktualizowany: %s") % ver)
        except Exception:
            pass
        # Use a short timer before restart to let the progress UI repaint.
        try:
            self._restart_timer.start(1500, True)
        except Exception:
            self._doRestart()

    def _doRestart(self):
        try:
            self._restart_timer.stop()
        except Exception:
            pass
        try:
            self.session.open(TryQuitMainloop, 3)
        except Exception as e:
            print("[RaczQQ Updater] restart gui error:", e)


# ---------------------------------------------------------------------------
# ChannelListUpdateMenu  (main screen)
# ---------------------------------------------------------------------------

class ChannelListUpdateMenu(Screen):
    skin = '''<screen name="ChannelListUpdateMenu" position="center,center" size="900,620" title="RaczQQ Updater" backgroundColor="#0e1116">

    <!-- tło -->
    <eLabel position="0,0" size="900,620" backgroundColor="#0e1116" zPosition="-10" />

    <!-- nagłówek -->
    <eLabel position="0,0"   size="900,64" backgroundColor="#151a21" zPosition="-5" />
    <eLabel position="0,64"  size="900,2"  backgroundColor="#4a9eff" />
    <eLabel position="24,20" size="4,26"   backgroundColor="#4a9eff" />

    <widget name="ai_title"    position="40,16"  size="430,32" font="Regular;26" halign="left"  valign="center" foregroundColor="#e8eaed" backgroundColor="#151a21" />
    <widget name="ai_subtitle" position="480,20" size="396,24" font="Regular;17" halign="right" valign="center" foregroundColor="#6b7684" backgroundColor="#151a21" />

    <!-- pasek stanu systemu -->
    <eLabel position="24,80"  size="3,30" backgroundColor="#ff5252" />
    <widget name="cpu"     position="27,80"  size="159,30" font="Regular;17" halign="center" valign="center" foregroundColor="#c9d1d9" backgroundColor="#171c24" />
    <eLabel position="196,80" size="3,30" backgroundColor="#3ddc84" />
    <widget name="ram"     position="199,80" size="159,30" font="Regular;17" halign="center" valign="center" foregroundColor="#c9d1d9" backgroundColor="#171c24" />
    <eLabel position="368,80" size="3,30" backgroundColor="#4a9eff" />
    <widget name="iplocal" position="371,80" size="159,30" font="Regular;17" halign="center" valign="center" foregroundColor="#c9d1d9" backgroundColor="#171c24" />
    <eLabel position="540,80" size="3,30" backgroundColor="#ffb020" />
    <widget name="iptun"   position="543,80" size="159,30" font="Regular;17" halign="center" valign="center" foregroundColor="#c9d1d9" backgroundColor="#171c24" />
    <eLabel position="712,80" size="3,30" backgroundColor="#a78bfa" />
    <widget name="ipext"   position="715,80" size="161,30" font="Regular;17" halign="center" valign="center" foregroundColor="#c9d1d9" backgroundColor="#171c24" />

    <!-- menu główne -->
    <eLabel position="24,124" size="852,272" backgroundColor="#12161c" zPosition="-3" />
    <widget source="list" render="Listbox"
            position="24,124" size="852,272"
            scrollbarMode="showOnDemand"
            backgroundColor="#12161c" backgroundColorSelected="#1d2735"
            foregroundColor="#e8eaed" foregroundColorSelected="#ffffff">
        <convert type="TemplatedMultiContent">
        {"template": [
            MultiContentEntryPixmapAlphaBlend(pos=(18,8), size=(52,52), png=1, flags=BT_SCALE|BT_KEEP_ASPECT_RATIO),
            MultiContentEntryText(pos=(86,8),  size=(736,30), font=0, color=0xe8eaed, color_sel=0xffffff, flags=RT_HALIGN_LEFT|RT_VALIGN_CENTER, text=0),
            MultiContentEntryText(pos=(86,38), size=(736,24), font=1, color=0x7c8898, color_sel=0x9fb4cc, flags=RT_HALIGN_LEFT|RT_VALIGN_CENTER, text=3)
        ],
        "fonts": [gFont("Regular",24), gFont("Regular",19)],
        "itemHeight": 68
        }
        </convert>
    </widget>

    <!-- status aktualizacji -->
    <eLabel position="24,410" size="852,1" backgroundColor="#232a34" />
    <widget name="update" position="24,420" size="852,26" font="Regular;19" halign="center" valign="center" foregroundColor="#9aa4b2" backgroundColor="#0e1116" />

    <!-- przyciski kolorowe -->
    <widget name="key_red_bar" position="24,458"  size="4,34"   backgroundColor="#ff5252" foregroundColor="#ff5252" />
    <widget name="key_red"     position="28,458"  size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
    <eLabel                    position="240,458" size="4,34"   backgroundColor="#3ddc84" />
    <widget name="key_green"   position="244,458" size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
    <eLabel                    position="456,458" size="4,34"   backgroundColor="#ffb020" />
    <widget name="key_yellow"  position="460,458" size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
    <eLabel                    position="672,458" size="4,34"   backgroundColor="#4a9eff" />
    <widget name="key_blue"    position="676,458" size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />

    <!-- README -->
    <eLabel position="24,506" size="852,80" backgroundColor="#12161c" zPosition="-3" />
    <eLabel position="24,506" size="3,80"   backgroundColor="#4a9eff" />
    <widget name="readme_title" position="40,512" size="820,22" font="Regular;18" halign="left" valign="center" foregroundColor="#4a9eff" backgroundColor="#12161c" />
    <widget name="readme"       position="40,536" size="820,46" font="Regular;16" halign="left" valign="top"    foregroundColor="#7c8898" backgroundColor="#12161c" />

    <!-- stopka -->
    <eLabel position="0,592" size="900,28" backgroundColor="#151a21" zPosition="-5" />
    <widget name="info" position="24,592" size="852,28" font="Regular;16" halign="center" valign="center" foregroundColor="#6b7684" backgroundColor="#151a21" />

</screen>'''

    def __init__(self, session):
        Screen.__init__(self, session)
        self.session = session
        self.list = []
        self._prev_cpu = None
        self._health_timer = eTimer()
        self.update_available = False
        self._sat_check_running = False
        self._sat_open_progress_timer = eTimer()
        self._external_ip = "N/A"
        self._external_ip_last_check = 0
        self._external_ip_running = False

        # MENU_ITEMS defined here so _() is called at runtime, not import time.
        self.MENU_ITEMS = [
            ("live.png",    _("Listy kanałów"),                  "channels",     _("13.0E & 19.2E & 23.5E & 28.2E")),
            ("sat.png",     _("Pobierz listę satelit"),           "sat",          _("Aktualizacja listy satelit")),
            ("picon.png",   _("Picony"),                          "picony",       _("Pobieranie i instalacja piconów")),
            ("archive.png", _("Twórz archiwum Pluginu"),          "archive",      _("RaczQQ Updater")),
            ("archive.png", _("Twórz backup plików systemowych"), "conf_backup",  _("Archiwizacja plików systemowych")),
            ("puzzle.png",  _("Instalacja dodatków"),             "addons",       _("Przeglądaj i instaluj pliki *.ipk")),
        ]

        try:
            self._sat_open_progress_timer.timeout.connect(self._open_sat_progress_screen)
        except Exception:
            self._sat_open_progress_timer.callback.append(self._open_sat_progress_screen)

        self["cpu"]          = Label("")
        self["ram"]          = Label("")
        self["iplocal"]      = Label("")
        self["iptun"]        = Label("")
        self["ipext"]        = Label("")
        self["ai_title"]     = Label("RaczQQ Updater")
        self["ai_subtitle"]  = Label(_("Panel zarządzania dekoderem"))
        self["update"]       = Label(_("Sprawdzanie wersji online..."))
        self["list"]         = List(self.list)
        self["key_red"]      = Label(_("Aktualizacja"))
        self["key_red_bar"]  = Label("")
        self._set_red_key_visible(False)
        self["key_green"]    = Label("-")
        self["key_yellow"]   = Label(_("Wyczyść TMP"))
        self["key_blue"]     = Label(_("Wyczyść RAM"))
        self["info"]         = Label(
            "Updater by RaczQQ | Wersja: {} | Data: {} | Python: Py3".format(
                PLUGIN_VERSION,
                str(datetime.date.today()),
            )
        )
        self["readme_title"] = Label(_("README / Informacje"))
        self["readme"]       = Label("")

        try:
            self._health_timer.timeout.connect(self._update_health)
        except Exception:
            self._health_timer.callback.append(self._update_health)

        self["actions"] = ActionMap(
            ["WizardActions", "ColorActions"],
            {
                "red":    self.keyRed,
                "yellow": self.clear_tmp_cache,
                "blue":   self.clear_ram_memory,
                "ok":     self.KeyOk,
                "back":   self.close,
            },
        )

        self.onShown.append(self._start_health_timer)
        self.onClose.append(self._stop_health_timer)
        self.onClose.append(self._cleanup_tmp_plugin_dir)

        self.updateList()
        self._refresh_readme()
        self.check_updates()
        self._update_health()

    # ------------------------------------------------------------------
    # Navigation helpers
    # ------------------------------------------------------------------

    def _set_red_key_visible(self, visible):
        """Pokaż/ukryj czerwony przycisk razem z jego kolorowym paskiem."""
        for key in ("key_red", "key_red_bar"):
            try:
                widget = self[key]
            except Exception:
                continue
            try:
                widget.show() if visible else widget.hide()
            except Exception:
                pass

    def _set_update_color(self, color):
        try:
            self["update"].instance.setForegroundColor(parseColor(color))
        except Exception:
            pass

    def _open_sat_progress_screen(self):
        try:
            self._sat_open_progress_timer.stop()
        except Exception:
            pass
        self.session.open(SatellitesUpdateProgress, self)

    # ------------------------------------------------------------------
    # README
    # ------------------------------------------------------------------

    def _read_readme_text(self):
        readme_names = ["README.MD", "README.md", "readme.md"]
        content = next(
            (
                read_text_file(os.path.join(PLUGIN_PATH, name))
                for name in readme_names
                if os.path.exists(os.path.join(PLUGIN_PATH, name))
            ),
            "",
        )
        content = ensure_str(content).replace("\r\n", "\n").replace("\r", "\n").strip()
        if not content:
            return _("Brak pliku README.MD albo plik jest pusty.")

        lines = []
        for line in content.split("\n"):
            line = re.sub(r'^\s*#+\s*', '', ensure_str(line))
            line = line.replace('**', '').replace('__', '').replace('`', '')
            line = line.replace('* ', '• ')
            line = re.sub(r'\[(.*?)\]\((.*?)\)', r'\1', line)
            line = re.sub(r'\s+', ' ', line).strip()
            if line:
                lines.append(line)

        max_lines = 4
        text_out = "\n".join(lines[:max_lines])
        if len(lines) > max_lines:
            text_out += "\n..."
        return text_out

    def _refresh_readme(self):
        try:
            self["readme"].setText(self._read_readme_text())
        except Exception as e:
            self["readme"].setText(_("Błąd odczytu README.MD: {}").format(e))

    # ------------------------------------------------------------------
    # Temp / memory cleanup
    # ------------------------------------------------------------------

    def _remove_path_quietly(self, path):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
        except Exception as e:
            print("[RaczQQ Updater] cleanup path error (%s): %s" % (path, e))

    def _cleanup_tmp_plugin_dir(self):
        self._remove_path_quietly(PLUGIN_TMP_PATH)

    def clear_ram_memory(self):
        try:
            subprocess.run(["sync"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with io.open("/proc/sys/vm/drop_caches", "w", encoding="ascii") as f:
                f.write("3\n")
        except Exception as e:
            print("[RaczQQ Updater] clear RAM error:", e)
        self.session.open(MessageBox, _("Pamięć RAM została wyczyszczona."), MessageBox.TYPE_INFO, timeout=3)

    def clear_tmp_cache(self):
        try:
            patterns = [
                "/tmp/*.ipk",
                "/tmp/*.zip",
                "/tmp/*.tar.gz",
                "/tmp/*.tgz",
                os.path.join(PLUGIN_TMP_PATH, "*"),
            ]
            for pattern in patterns:
                for path in glob.glob(pattern):
                    self._remove_path_quietly(path)
            self.session.open(MessageBox, _("Wyczyszczono pamięć podręczną /tmp."), MessageBox.TYPE_INFO, timeout=3)
        except Exception as e:
            self.session.open(MessageBox, _("Błąd: {}").format(e), MessageBox.TYPE_INFO, timeout=3)

    # ------------------------------------------------------------------
    # Health timer (CPU / RAM / IP)
    # ------------------------------------------------------------------

    def _start_health_timer(self):
        try:
            self._health_timer.start(2000, True)
        except Exception:
            pass

    def _stop_health_timer(self):
        try:
            self._health_timer.stop()
        except Exception:
            pass

    def _read_cpu_percent(self):
        try:
            with open("/proc/stat", "r") as f:
                line = f.readline()
            parts = line.split()
            if not parts or parts[0] != "cpu":
                return None
            nums = list(map(int, parts[1:8]))
            total = sum(nums)
            idle = nums[3] + nums[4]
            if self._prev_cpu is None:
                self._prev_cpu = (total, idle)
                return 0.0
            prev_total, prev_idle = self._prev_cpu
            dt = total - prev_total
            di = idle - prev_idle
            self._prev_cpu = (total, idle)
            if dt <= 0:
                return 0.0
            return max(0.0, min(100.0, (dt - di) * 100.0 / float(dt)))
        except Exception:
            return None

    def _read_mem_pct(self):
        try:
            mem = {}
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    mem[k.strip()] = int(v.strip().split()[0])
            total = mem.get("MemTotal", 0)
            avail = mem.get("MemAvailable", mem.get("MemFree", 0))
            used = max(0, total - avail)
            return (used * 100.0 / float(total)) if total else 0.0
        except Exception:
            return None

    def _get_ips_from_system(self):
        local_ip = None
        tunneled_ip = None
        try:
            result = subprocess.run(
                ["ip", "-4", "-o", "addr", "show"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            out = result.stdout.decode("utf-8", "ignore")
            for line in out.splitlines():
                m = re.search(r'^\d+:\s+([^\s]+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/', line)
                if not m:
                    continue
                iface, ip = m.group(1), m.group(2)
                if iface.startswith(("tun", "tap", "ppp", "wg")):
                    if tunneled_ip is None:
                        tunneled_ip = ip
                elif iface.startswith(("eth", "wlan", "ra", "en")) and not ip.startswith("127."):
                    if local_ip is None:
                        local_ip = ip
        except Exception:
            pass
        return local_ip, tunneled_ip

    def _is_valid_ipv4(self, value):
        return bool(re.match(r'^\d{1,3}(\.\d{1,3}){3}$', value or ""))

    def _read_external_ip_sync(self):
        commands = [
            ["curl", "-4", "-s", "--max-time", "4", "https://icanhazip.com"],
            ["wget", "-qO-", "-T", "4", "https://icanhazip.com"],
        ]
        for cmd in commands:
            try:
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if result.returncode != 0:
                    continue
                parts = result.stdout.decode("utf-8", "ignore").strip().split()
                ip = parts[0] if parts else ""
                if self._is_valid_ipv4(ip):
                    return ip
            except Exception:
                pass
        return "N/A"

    def _apply_external_ip(self, ip):
        self._external_ip = ip or "N/A"
        self._external_ip_running = False
        try:
            self["ipext"].setText("WAN: %s" % self._external_ip)
        except Exception:
            pass

    def _refresh_external_ip_async(self):
        now = time.time()
        if self._external_ip_running or (now - self._external_ip_last_check) < 60:
            return
        self._external_ip_last_check = now
        self._external_ip_running = True

        def worker():
            ip = self._read_external_ip_sync()
            reactor.callFromThread(self._apply_external_ip, ip)

        Thread(target=worker).start()

    def _update_health(self):
        try:
            cpu = self._read_cpu_percent()
            mem = self._read_mem_pct()
            local_ip, tunneled_ip = self._get_ips_from_system()
            self["cpu"].setText("CPU: %s" % ("N/A" if cpu is None else "%d%%" % int(cpu)))
            self["ram"].setText("RAM: %s" % ("N/A" if mem is None else "%d%%" % int(mem)))
            self["iplocal"].setText("LAN: %s" % (local_ip or "N/A"))
            self["iptun"].setText("VPN: %s" % (tunneled_ip or "N/A"))
            self["ipext"].setText("WAN: %s" % (self._external_ip or "N/A"))
            self._refresh_external_ip_async()
        except Exception:
            pass
        self._start_health_timer()
    # ------------------------------------------------------------------
    # Version helpers
    # ------------------------------------------------------------------

    def _read_version_file(self, path, default="unknown"):
        try:
            v = read_text_file(path, default="", encoding="utf-8").strip()
            return v if v else default
        except Exception:
            return default

    def _normalize_version(self, version_string):
        v = (version_string or "").strip()
        if not v:
            return [0]
        parts = re.findall(r'\d+', v)
        if parts:
            try:
                return [int(x) for x in parts]
            except Exception:
                pass
        return [0]

    def _is_online_version_newer(self, local_ver, online_ver):
        return self._normalize_version(online_ver) > self._normalize_version(local_ver)

    # ------------------------------------------------------------------
    # Plugin update
    # ------------------------------------------------------------------

    def check_updates(self):
        prepare_tmp_dir()
        self._set_red_key_visible(False)
        self._set_update_color(UI_TEXT_MUTED)
        self["update"].setText(_("Sprawdzanie wersji online..."))

        url = "https://raw.githubusercontent.com/QraczQQ/RaczQQUpdater/main/plugin.version"
        tmp_version_path = os.path.join(PLUGIN_TMP_PATH, "plugin.version")

        def after_download():
            try:
                online_version = self._read_version_file(tmp_version_path, "unknown")
                self.update_available = False
                self._set_red_key_visible(False)
                self._set_update_color(UI_TEXT_MUTED)
                status = _("Brak aktualizacji.")

                if online_version != "unknown" and self._is_online_version_newer(PLUGIN_VERSION, online_version):
                    status = _("Aktualizacja jest dostępna.")
                    self.update_available = True
                    self._set_red_key_visible(True)
                    self._set_update_color(UI_TEXT_ALERT)

                self["update"].setText(_("Wersja online: {} | {}").format(online_version, status))
            except Exception as e:
                print("[RaczQQ Updater] after_download error:", e)
                self._set_red_key_visible(False)
                self._set_update_color(UI_TEXT_WARN)
                self["update"].setText(_("Wersja online: błąd odczytu | Brak informacji o aktualizacji"))

        def run_check():
            try:
                if os.path.exists(tmp_version_path):
                    os.remove(tmp_version_path)
            except Exception:
                pass
            if run_wget(url, tmp_version_path, timeout=15):
                reactor.callFromThread(after_download)
            else:
                reactor.callFromThread(self.errorUpdate)

        Thread(target=run_check).start()

    def errorUpdate(self, failure=None):
        self._set_red_key_visible(False)
        self._set_update_color(UI_TEXT_WARN)
        self["update"].setText(_("Wersja online: błąd pobierania | Brak informacji o aktualizacji"))

    def keyRed(self):
        if not self.update_available:
            self.session.open(MessageBox, _("Aktualizacja nie jest konieczna."), MessageBox.TYPE_INFO, timeout=3)
            return
        self.session.openWithCallback(
            self._confirm_download_and_install_update,
            MessageBox,
            _("Pobrać i zainstalować aktualizację pluginu?"),
            MessageBox.TYPE_YESNO,
        )

    def _confirm_download_and_install_update(self, answer):
        if answer:
            self.download_and_install_update()

    def download_and_install_update(self):
        url = "https://github.com/QraczQQ/RaczQQUpdater/archive/refs/heads/main.zip"
        zip_path = os.path.join(PLUGIN_TMP_PATH, "RaczQQUpdater-main.zip")
        prepare_tmp_dir()
        self["update"].setText(_("Pobieranie aktualizacji..."))

        def show_error(msg):
            self["update"].setText(_("Błąd aktualizacji: {}").format(msg))
            self.session.open(MessageBox, _("Błąd aktualizacji:\n{}").format(msg), MessageBox.TYPE_ERROR, timeout=6)

        def finish_ok():
            self["update"].setText(_("Aktualizacja zakończona. Restart GUI..."))
            self.session.open(TryQuitMainloop, 3)

        def worker():
            try:
                try:
                    if os.path.exists(zip_path):
                        os.remove(zip_path)
                except Exception:
                    pass

                if not run_wget(url, zip_path, timeout=30):
                    reactor.callFromThread(show_error, "nie udało się pobrać archiwum ZIP")
                    return

                try:
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        zf.extractall(PLUGIN_TMP_PATH)
                except Exception as e:
                    reactor.callFromThread(show_error, "błąd rozpakowania ZIP: %s" % e)
                    return

                src_root = os.path.join(PLUGIN_TMP_PATH, "RaczQQUpdater-main")
                if not os.path.isdir(src_root):
                    reactor.callFromThread(show_error, "brak katalogu RaczQQUpdater-main po rozpakowaniu")
                    return

                try:
                    for name in os.listdir(src_root):
                        src = os.path.join(src_root, name)
                        dst = os.path.join(PLUGIN_PATH, name)
                        if os.path.isdir(src):
                            if os.path.exists(dst):
                                shutil.rmtree(dst, ignore_errors=True)
                            shutil.copytree(src, dst)
                        else:
                            shutil.copy2(src, dst)
                except Exception as e:
                    reactor.callFromThread(show_error, "błąd podmiany plików: %s" % e)
                    return

                reactor.callFromThread(finish_ok)
            except Exception as e:
                reactor.callFromThread(show_error, str(e))

        Thread(target=worker).start()

    # ------------------------------------------------------------------
    # Satellites.xml helpers
    # ------------------------------------------------------------------

    def _normalize_date_version(self, value):
        value = (value or "").strip()
        if re.match(r'^\d{4}-\d{2}-\d{2}$', value):
            return value
        return "unknown"

    def _extract_sat_version_from_xml(self, path):
        try:
            root = ElementTree.parse(path).getroot()
            for value in root.attrib.values():
                match = re.search(r'(\d{4}-\d{2}-\d{2})', value or "")
                if match:
                    return self._normalize_date_version(match.group(1))
        except Exception:
            pass
        try:
            content = read_text_file(path)
            match = re.search(r'File[^\n\r]*(\d{4}-\d{2}-\d{2})', content)
            if match:
                return self._normalize_date_version(match.group(1))
        except Exception:
            pass
        return "unknown"
    def _sat_date_tuple(self, value):
        norm = self._normalize_date_version(value)
        if norm == "unknown":
            return None
        try:
            return tuple(map(int, norm.split("-")))
        except Exception:
            return None

    def _is_sat_online_newer(self, xml_ver, online_ver):
        xml_date    = self._sat_date_tuple(xml_ver)
        online_date = self._sat_date_tuple(online_ver)
        if xml_date is None or online_date is None:
            return False
        return online_date > xml_date

    def _fetch_online_sat_version(self):
        # FIX: changed http:// → https://
        url = "https://raw.githubusercontent.com/OpenPLi/tuxbox-xml/master/xml/satellites.xml"
        tmp_sat_path = os.path.join(PLUGIN_TMP_PATH, "satellites_online.xml")
        prepare_tmp_dir()
        try:
            if os.path.exists(tmp_sat_path):
                os.remove(tmp_sat_path)
        except Exception:
            pass
        if not run_wget(url, tmp_sat_path, timeout=15):
            return None
        return self._extract_sat_version_from_xml(tmp_sat_path)

    def _run_sat_update_with_progress(self, progress_screen):
        # FIX: changed http:// → https://
        url           = "https://raw.githubusercontent.com/OpenPLi/tuxbox-xml/master/xml/satellites.xml"
        tmp_file      = os.path.join(PLUGIN_TMP_PATH, "satellites.xml")
        target        = "/etc/tuxbox/satellites.xml"
        version_file  = "/etc/tuxbox/satellites.version"
        enigma_override = "/etc/enigma2/satellites.xml"

        prepare_tmp_dir()

        def ui_progress(value, text):
            reactor.callFromThread(progress_screen.setProgress, value, text)

        def ui_error(msg):
            self["update"].setText(_("Błąd aktualizacji satellites.xml"))
            reactor.callFromThread(progress_screen.finishError, msg)

        def ui_success(ver):
            self["update"].setText(_("satellites.xml zaktualizowany: %s") % ver)
            reactor.callFromThread(progress_screen.finishSuccess, ver)

        def worker():
            try:
                ui_progress(5, _("Przygotowanie..."))
                try:
                    if os.path.exists(tmp_file):
                        os.remove(tmp_file)
                except Exception:
                    pass

                ui_progress(15, _("Pobieranie pliku..."))

                if not run_wget(url, tmp_file, timeout=20):
                    ui_error("nie udało się pobrać satellites.xml")
                    return

                ui_progress(55, _("Sprawdzanie pliku..."))

                if not os.path.exists(tmp_file) or os.path.getsize(tmp_file) == 0:
                    ui_error("pobrany plik jest pusty")
                    return

                xml_version = self._extract_sat_version_from_xml(tmp_file)
                if xml_version == "unknown":
                    ui_error("nie udało się odczytać daty z pobranego satellites.xml")
                    return

                ui_progress(70, _("Zapisywanie satellites.xml..."))
                try:
                    shutil.move(tmp_file, target)
                except Exception as e:
                    ui_error("nie udało się zapisać pliku docelowego: %s" % e)
                    return

                ui_progress(82, _("Zapisywanie wersji..."))
                try:
                    with io.open(version_file, "w", encoding="utf-8") as f:
                        f.write(ensure_str(xml_version) + "\n")
                except Exception as e:
                    ui_error("nie udało się zapisać satellites.version: %s" % e)
                    return

                ui_progress(90, _("Usuwanie lokalnego override..."))
                try:
                    if os.path.exists(enigma_override):
                        os.remove(enigma_override)
                except Exception as e:
                    ui_error("nie udało się usunąć /etc/enigma2/satellites.xml: %s" % e)
                    return

                ui_progress(96, _("Przeładowywanie list..."))
                try:
                    os.sync()
                except Exception:
                    pass
                try:
                    reactor.callFromThread(reload_enigma_settings)
                except Exception:
                    pass

                ui_progress(100, _("Gotowe"))
                ui_success(xml_version)

            except Exception as e:
                ui_error(str(e))

        Thread(target=worker).start()

    def _show_sat_update_summary(self, online_version):
        xml_version    = self._extract_sat_version_from_xml("/etc/tuxbox/satellites.xml")
        online_display = online_version if online_version and online_version != "unknown" else "nieznana"
        xml_display    = xml_version if xml_version != "unknown" else "nieznana"
        msg = (
            "Wersja dostępna online: {}\n"
            "Wersja z satellites.xml: {}"
        ).format(online_display, xml_display)

        if not online_version or online_version == "unknown":
            self["update"].setText(_("Nie udało się pobrać wersji online satellites.xml"))
            self.session.open(MessageBox, msg + "\n\nNie udało się pobrać wersji online.",
                              MessageBox.TYPE_ERROR, timeout=3)
            return

        if xml_version == "unknown":
            self["update"].setText(_("Brak daty w satellites.xml — dostępna aktualizacja"))
            self.session.openWithCallback(
                self._confirm_sat_update,
                MessageBox,
                msg + "\n\nNie wykryto daty w satellites.xml. Czy chcesz pobrać i zainstalować aktualizację?",
                MessageBox.TYPE_YESNO,
            )
            return

        if self._is_sat_online_newer(xml_version, online_version):
            self["update"].setText(_("Dostępna jest nowsza wersja satellites.xml"))
            self.session.openWithCallback(
                self._confirm_sat_update,
                MessageBox,
                msg + "\n\nDostępna jest nowsza wersja. Czy chcesz zaktualizować?",
                MessageBox.TYPE_YESNO,
            )
        else:
            self["update"].setText(_("satellites.xml jest aktualny"))
            self.session.open(MessageBox, msg + "\n\nAktualizacja nie jest wymagana",
                              MessageBox.TYPE_INFO, timeout=3)

    def _confirm_sat_update(self, answer):
        if answer:
            self["update"].setText(_("Rozpoczynam aktualizację satellites.xml..."))
            try:
                self._sat_open_progress_timer.start(200, True)
            except Exception:
                self._open_sat_progress_screen()
        else:
            self["update"].setText(_("Aktualizacja satellites.xml anulowana"))

    def update_sat(self):
        if self._sat_check_running:
            return
        self._sat_check_running = True
        self["update"].setText(_("Sprawdzanie wersji satellites.xml..."))

        def worker():
            try:
                online_version = self._fetch_online_sat_version()
                reactor.callFromThread(self._after_sat_online_check, online_version)
            except Exception as e:
                reactor.callFromThread(self._after_sat_online_check_error, str(e))

        Thread(target=worker).start()

    def _after_sat_online_check(self, online_version):
        self._sat_check_running = False
        self["update"].setText(_("Sprawdzono wersję satellites.xml"))
        self._show_sat_update_summary(online_version)

    def _after_sat_online_check_error(self, err):
        self._sat_check_running = False
        self["update"].setText(_("Błąd sprawdzania satellites.xml"))
        self.session.open(MessageBox, _("Błąd sprawdzania satellites.xml:\n%s") % err,
                          MessageBox.TYPE_ERROR, timeout=5)

    # ------------------------------------------------------------------
    # Menu list / navigation
    # ------------------------------------------------------------------

    def updateList(self):
        images_path = os.path.join(resolveFilename(SCOPE_PLUGINS), "Extensions/RaczQQUpdater/images")
        self["list"].setList([
            (name, LoadPixmap(os.path.join(images_path, icon)), idx, desc)
            for icon, name, idx, desc in self.MENU_ITEMS
        ])

    def open_channels(self):
        self.session.open(ChannelsScreen)

    def open_picony(self):
        self.session.open(PiconyScreen)

    def open_archive(self):
        self.session.open(ArchiveScreen)

    def open_conf_backup(self):
        self.session.open(ConfBackupScreen)

    def open_addons(self):
        self.session.open(AddonsScreen)

    def KeyOk(self):
        sel = self["list"].getCurrent()
        if not sel:
            return
        actions = {
            "channels":    self.open_channels,
            "sat":         self.update_sat,
            "picony":      self.open_picony,
            "archive":     self.open_archive,
            "conf_backup": self.open_conf_backup,
            "addons":      self.open_addons,
        }
        action = actions.get(sel[2])
        if action:
            action()


# ---------------------------------------------------------------------------
# ManifestChannelsScreen
# ---------------------------------------------------------------------------

class ManifestChannelsScreen(Screen):
    skin = '''
    <screen name="ManifestChannelsScreen" position="center,center" size="900,560" title="Dostępne listy kanałów" backgroundColor="#0e1116">
        <eLabel position="0,0"   size="900,560" backgroundColor="#0e1116" zPosition="-10" />
        <eLabel position="0,0"   size="900,56"  backgroundColor="#151a21" zPosition="-5" />
        <eLabel position="0,56"  size="900,2"   backgroundColor="#4a9eff" />
        <eLabel position="24,16" size="4,24"    backgroundColor="#4a9eff" />
        <eLabel position="40,14" size="700,28"  font="Regular;22" halign="left" valign="center" text="Dostępne listy kanałów" foregroundColor="#e8eaed" backgroundColor="#151a21" />

        <eLabel position="24,76" size="852,396" backgroundColor="#12161c" zPosition="-3" />
        <widget source="list" render="Listbox" position="36,84" size="828,380"
                scrollbarMode="showOnDemand" font="Regular;21" itemHeight="44"
                backgroundColor="#12161c" backgroundColorSelected="#1d2735"
                foregroundColor="#e8eaed" foregroundColorSelected="#ffffff">
            <convert type="StringList" />
        </widget>

        <eLabel position="24,486" size="852,1"  backgroundColor="#232a34" />
        <widget name="status" position="24,498" size="852,30" font="Regular;18" halign="center" valign="center" foregroundColor="#9aa4b2" backgroundColor="#0e1116" />
        <eLabel position="0,552" size="900,8" backgroundColor="#151a21" zPosition="-5" />
    </screen>'''

    def __init__(self, session, lists_menu):
        Screen.__init__(self, session)
        self.session = session
        self.lists_menu = lists_menu or []
        self["list"]    = List([])
        self["status"]  = Label("OK - wybierz | EXIT - powrót")
        self["actions"] = ActionMap(
            ["OkCancelActions", "WizardActions", "DirectionActions"],
            {
                "ok":     self.keyOk,
                "back":   self.close,
                "cancel": self.close,
                "up":     self.keyUp,
                "down":   self.keyDown,
            },
            -1,
        )
        self.buildList()

    def buildList(self):
        entries = []
        for title, action in self.lists_menu:
            entries.append((ensure_str(title), action))
        self["list"].setList(entries)
        self["status"].setText(
            "Załadowano %d pozycji" % len(entries) if entries else "Brak pozycji do wyświetlenia"
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

    def keyOk(self):
        sel = self["list"].getCurrent()
        if not sel:
            return
        _title, action_str = sel[0], sel[1]
        action = _parse_action(action_str)
        item_type = action.get("type", "unknown")
        url        = action.get("url", "")
        bouquet_id = action.get("bouquet_id", "")
        name       = action.get("name", "")

        if item_type == "archive":
            self.install_archive(_title, url)
        elif item_type == "m3u":
            self.install_m3u_as_bouquet(_title, url, bouquet_id, name)
        elif item_type == "bouquet":
            self.install_bouquet_reference(_title, url, bouquet_id, name)

    # ------------------------------------------------------------------
    # Installation methods
    # ------------------------------------------------------------------

    def install_archive(self, title, url):
        prepare_tmp_dir()
        if not url.endswith((".zip", ".tar.gz", ".tgz", ".ipk")):
            self.session.open(MessageBox, _("Nieobsługiwany format archiwum!"), MessageBox.TYPE_ERROR, timeout=5)
            return

        archive_type     = "zip" if url.endswith(".zip") else ("tar.gz" if url.endswith((".tar.gz", ".tgz")) else "ipk")
        tmp_archive_path = os.path.join(PLUGIN_TMP_PATH, os.path.basename(url))
        extract_dir      = os.path.join(PLUGIN_TMP_PATH, "extracted")

        if archive_type == "ipk":
            cmd = (
                '[ -f "{archive}" ] && rm -f "{archive}"; '
                'wget -T 30 --no-check-certificate -O "{archive}" "{url}" && '
                'opkg install --force-reinstall "{archive}" && '
                '[ -f "{archive}" ] && rm -f "{archive}"; sync'
            ).format(archive=tmp_archive_path, url=url)
            run_command_in_background(self.session, title, [cmd], callback_on_finish=reload_enigma_settings)
            return

        extract_cmd = (
            'unzip -o -q "{archive}" -d "{extract}"'
            if archive_type == "zip"
            else 'tar -xzf "{archive}" -C "{extract}"'
        ).format(archive=tmp_archive_path, extract=extract_dir)

        cmd = (
            '[ -f "{archive}" ] && rm -f "{archive}"; '
            '[ -d "{extract}" ] && rm -rf "{extract}"; '
            'mkdir -p "{extract}"; '
            'wget -T 30 --no-check-certificate -O "{archive}" "{url}" && '
            '{extract_cmd} && '
            'LAMEDB=$(find "{extract}" -type f -name "lamedb*" | head -n 1); '
            'if [ -n "$LAMEDB" ]; then '
            'LAMEDB_DIR=$(dirname "$LAMEDB"); '
            'rm -f /etc/enigma2/lamedb* /etc/enigma2/bouquets.* /etc/enigma2/whitelist*; '
            'find /etc/enigma2 -maxdepth 1 -type f -name "userbouquet.*.tv" ! -name "userbouquet.jedi*.tv" -exec rm -f {{}} \\; ; '
            'find /etc/enigma2 -maxdepth 1 -type f -name "userbouquet.*.radio" -exec rm -f {{}} \\; ; '
            'find "$LAMEDB_DIR" -maxdepth 1 -type f \\( -name "lamedb*" -o -name "bouquets.*" -o -name "userbouquet.*" -o -name "whitelist*" \\) -exec cp -f {{}} /etc/enigma2/ \\; ; '
            '[ -f /etc/enigma2/bouquets.tv ] || touch /etc/enigma2/bouquets.tv; '
            'for f in /etc/enigma2/userbouquet.jedi*.tv; do '
            '[ -f "$f" ] || continue; '
            'bn=$(basename "$f"); '
            'line=$(printf \'#SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "%s" ORDER BY bouquet\' "$bn"); '
            'grep -Fqx "$line" /etc/enigma2/bouquets.tv || echo "$line" >> /etc/enigma2/bouquets.tv; '
            'done; fi; '
            '[ -f "{archive}" ] && rm -f "{archive}"; '
            '[ -d "{extract}" ] && rm -rf "{extract}"; sync'
        ).format(archive=tmp_archive_path, extract=extract_dir, url=url, extract_cmd=extract_cmd)

        run_command_in_background(self.session, title, [cmd], callback_on_finish=reload_enigma_settings)

    def install_m3u_as_bouquet(self, title, url, bouquet_id, bouquet_name):
        tmp = os.path.join(PLUGIN_TMP_PATH, "temp.m3u")
        run_command_in_background(
            self.session,
            title,
            ['wget -T 30 --no-check-certificate -O "{}" "{}"'.format(tmp, url)],
            callback_on_finish=lambda: Thread(
                target=self._parse_m3u_thread, args=(tmp, bouquet_id, bouquet_name)
            ).start(),
        )

    def _parse_m3u_thread(self, tmp_path, bid, bname):
        try:
            if not os.path.exists(tmp_path):
                return
            e2 = ["#NAME {}\n".format(bname)]
            with io.open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
                name = "N/A"
                for line in f:
                    ln = line.strip()
                    if ln.startswith("#EXTINF:"):
                        name = ln.split(",")[-1].strip()
                    elif ln.startswith("http"):
                        e2.append("#SERVICE 4097:0:1:0:0:0:0:0:0:0:{}:{}\n".format(
                            ln.replace(":", "%3a"), name))
                        name = "N/A"
            if len(e2) > 1:
                t_bq = os.path.join(PLUGIN_TMP_PATH, bid)
                with io.open(t_bq, "w", encoding="utf-8") as f:
                    f.writelines([ensure_str(x) for x in e2])
                reactor.callFromThread(self._install_parsed_bouquet, t_bq, bid)
        except Exception as e:
            print("[RaczQQ Updater] _parse_m3u_thread error:", e)

    def _install_parsed_bouquet(self, t_bq, bid):
        try:
            shutil.move(t_bq, os.path.join("/etc/enigma2", bid))
            # FIX: read first, then append only if needed – safer than r+ mode.
            bouquets_path = "/etc/enigma2/bouquets.tv"
            try:
                with open(bouquets_path, "r") as f:
                    content = f.read()
            except Exception:
                content = ""
            if bid not in content:
                with open(bouquets_path, "a") as f:
                    f.write('#SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "{}" ORDER BY bouquet\n'.format(bid))
            reload_enigma_settings()
        except Exception:
            traceback.print_exc()

    def install_bouquet_reference(self, title, url, bid, bname):
        cmd = (
            'wget -qO "/etc/enigma2/{b}" "{u}" && '
            '(grep -q "{b}" /etc/enigma2/bouquets.tv || '
            'echo \'#SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "{b}" ORDER BY bouquet\' >> /etc/enigma2/bouquets.tv)'
        ).format(b=bid, u=url)
        run_command_in_background(self.session, title, [cmd], callback_on_finish=reload_enigma_settings)


# ---------------------------------------------------------------------------
# ChannelsScreen  (was: "channels" – renamed to PEP 8 CamelCase)
# ---------------------------------------------------------------------------

class ChannelsScreen(Screen):
    skin = '''
    <screen name="ChannelsScreen" position="center,center" size="720,190" title="Pobieranie list" backgroundColor="#0e1116">
        <eLabel position="0,0"   size="720,190" backgroundColor="#0e1116" zPosition="-10" />
        <eLabel position="0,0"   size="720,50"  backgroundColor="#151a21" zPosition="-5" />
        <eLabel position="0,50"  size="720,2"   backgroundColor="#4a9eff" />
        <eLabel position="24,14" size="4,22"    backgroundColor="#4a9eff" />
        <eLabel position="40,12" size="560,26"  font="Regular;21" halign="left" valign="center" text="Pobieranie list kanałów" foregroundColor="#e8eaed" backgroundColor="#151a21" />

        <widget name="status" position="24,72" size="672,68" font="Regular;21" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#0e1116" />

        <eLabel position="24,156" size="672,1"  backgroundColor="#232a34" />
        <eLabel position="24,162" size="672,22" font="Regular;15" halign="center" valign="center" text="EXIT - powrót" foregroundColor="#6b7684" backgroundColor="#0e1116" />
    </screen>'''

    def __init__(self, session, args=None):
        Screen.__init__(self, session)
        self.session = session
        self["status"] = Label("Pobieranie manifest.json...\nProszę czekać...")
        self["actions"] = ActionMap(
            ["OkCancelActions", "WizardActions"],
            {"back": self.close, "cancel": self.close},
            -1,
        )
        self.onShown.append(self.startLoad)

    def startLoad(self):
        Thread(target=self._load_manifest_thread).start()

    def _load_manifest_thread(self):
        try:
            lists_menu = _get_lists_from_repo_sync()
            reactor.callFromThread(self._open_manifest_screen, lists_menu)
        except Exception as e:
            print("[RaczQQ Updater] load manifest thread error:", e)
            reactor.callFromThread(self._show_error, str(e))

    def _open_manifest_screen(self, lists_menu):
        if not lists_menu:
            self.session.open(
                MessageBox,
                _("Nie udało się pobrać list z manifest.json"),
                MessageBox.TYPE_ERROR,
                timeout=5,
            )
            self.close()
            return
        self.session.open(ManifestChannelsScreen, lists_menu)
        self.close()

    def _show_error(self, err):
        self.session.open(
            MessageBox,
            _("Błąd pobierania manifest.json:\n{}").format(err),
            MessageBox.TYPE_ERROR,
            timeout=6,
        )
        self.close()


# ---------------------------------------------------------------------------
# ArchiveScreen
# ---------------------------------------------------------------------------

class ArchiveScreen(Screen):
    skin = """
    <screen name="ArchiveScreen" position="center,center" size="900,560" title="Archiwum RaczQQ Updater" backgroundColor="#0e1116">
        <eLabel position="0,0"   size="900,560" backgroundColor="#0e1116" zPosition="-10" />
        <eLabel position="0,0"   size="900,56"  backgroundColor="#151a21" zPosition="-5" />
        <eLabel position="0,56"  size="900,2"   backgroundColor="#4a9eff" />
        <eLabel position="24,16" size="4,24"    backgroundColor="#4a9eff" />
        <eLabel position="40,14" size="320,28"  font="Regular;22" halign="left" valign="center" text="Archiwum pluginu" foregroundColor="#e8eaed" backgroundColor="#151a21" />
        <widget source="info" render="Label" position="380,16" size="496,26" font="Regular;17" halign="right" valign="center" foregroundColor="#9aa4b2" backgroundColor="#151a21" />

        <eLabel position="24,76" size="852,356" backgroundColor="#12161c" zPosition="-3" />
        <widget source="list" render="Listbox" position="36,84" size="828,340" scrollbarMode="showOnDemand"
                backgroundColor="#12161c" backgroundColorSelected="#1d2735"
                foregroundColor="#e8eaed" foregroundColorSelected="#ffffff">
            <convert type="TemplatedMultiContent">
                {
                    "template": [
                        MultiContentEntryText(pos=(10, 8),  size=(800, 30), font=0, color=0xe8eaed, color_sel=0xffffff, flags=RT_HALIGN_LEFT | RT_VALIGN_CENTER, text=0),
                        MultiContentEntryText(pos=(10, 40), size=(800, 24), font=1, color=0x7c8898, color_sel=0x9fb4cc, flags=RT_HALIGN_LEFT | RT_VALIGN_CENTER, text=1)
                    ],
                    "fonts": [gFont("Regular", 23), gFont("Regular", 18)],
                    "itemHeight": 72
                }
            </convert>
        </widget>

        <eLabel position="24,446" size="852,1" backgroundColor="#232a34" />

        <eLabel position="24,458"  size="4,34" backgroundColor="#ff5252" />
        <widget source="key_red"    render="Label" position="28,458"  size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
        <eLabel position="240,458" size="4,34" backgroundColor="#3ddc84" />
        <widget source="key_green"  render="Label" position="244,458" size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
        <eLabel position="456,458" size="4,34" backgroundColor="#ffb020" />
        <widget source="key_yellow" render="Label" position="460,458" size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />
        <eLabel position="672,458" size="4,34" backgroundColor="#4a9eff" />
        <widget source="key_blue"   render="Label" position="676,458" size="200,34" font="Regular;18" halign="center" valign="center" foregroundColor="#e8eaed" backgroundColor="#1a2028" />

        <eLabel position="24,506" size="852,22" font="Regular;15" halign="center" valign="center" text="OK / Zielony - przywróć   |   Czerwony - utwórz   |   Żółty - odśwież   |   EXIT - powrót" foregroundColor="#6b7684" backgroundColor="#0e1116" />
        <eLabel position="0,552" size="900,8" backgroundColor="#151a21" zPosition="-5" />
    </screen>
    """

    BACKUP_DIR = "/data/RaczQQUpdater/backup"

    def __init__(self, session):
        Screen.__init__(self, session)
        self.session = session
        self["key_red"]    = StaticText(_("Utwórz archiwum"))
        self["key_green"]  = StaticText(_("Przywróć"))
        self["key_yellow"] = StaticText(_("Odśwież"))
        self["key_blue"]   = StaticText(_("Zamknij"))
        self["info"]       = StaticText(_("Wybierz backup z listy"))
        self["list"]       = List([])
        self["actions"]    = ActionMap(
            ["ColorActions", "OkCancelActions"],
            {
                "red":    self.create_archive,
                "yellow": self.updateList,
                "green":  self.restore_selected_backup,
                "blue":   self.close,
                "cancel": self.close,
                "ok":     self.restore_selected_backup,
            },
            -1,
        )
        self.updateList()

    def restart_gui(self):
        self.session.open(TryQuitMainloop, 3)

    def _get_backup_date(self, fullpath, filename):
        m = re.search(r'(\d{8})', filename)
        if m:
            raw = m.group(1)
            return "%s-%s-%s" % (raw[0:4], raw[4:6], raw[6:8])
        try:
            ts = os.path.getmtime(fullpath)
            return time.strftime("%Y-%m-%d", time.localtime(ts))
        except Exception:
            return "brak daty"

    def _get_backup_version(self, fullpath):
        """Read plugin.version from a .tar.gz backup using a context manager."""
        try:
            with tarfile.open(fullpath, "r:gz") as tar:
                version_member = next(
                    (m for m in tar.getmembers()
                     if os.path.basename(m.name) == "plugin.version"),
                    None,
                )
                if version_member is not None:
                    f = tar.extractfile(version_member)
                    if f is not None:
                        version = ensure_str(f.read()).strip()
                        if version:
                            return version
        except Exception as e:
            print("[RaczQQ Updater] Blad odczytu wersji z backupu %s: %s" % (fullpath, e))
        return "brak wersji"

    def updateList(self):
        """Build backup list – version reading is done in a background thread to avoid UI freezes."""
        if not os.path.isdir(self.BACKUP_DIR):
            try:
                os.makedirs(self.BACKUP_DIR)
            except Exception as e:
                print("[RaczQQ Updater] Nie mozna utworzyc katalogu backup: %s" % e)

        try:
            files = sorted(
                [f for f in os.listdir(self.BACKUP_DIR) if f.endswith(".tar.gz")],
                reverse=True,
            )
        except Exception as e:
            print("[RaczQQ Updater] Blad listowania backupow: %s" % e)
            files = []

        # Show placeholder entries immediately while versions load in background.
        placeholder_items = []
        for filename in files:
            fullpath = os.path.join(self.BACKUP_DIR, filename)
            date_str = self._get_backup_date(fullpath, filename)
            placeholder_items.append((filename + "  | …", "Data backupu: " + date_str, fullpath, "…", date_str))

        if not placeholder_items:
            placeholder_items.append((
                _("Brak backupów"),
                _("Naciśnij czerwony przycisk aby utworzyć archiwum"),
                "", "", "",
            ))

        self["list"].setList(placeholder_items)
        self["info"].setText(_("Wczytywanie backupów..."))

        def load_versions():
            items = []
            for filename in files:
                fullpath = os.path.join(self.BACKUP_DIR, filename)
                date_str    = self._get_backup_date(fullpath, filename)
                version_str = self._get_backup_version(fullpath)
                title = "%s  | %s" % (filename, version_str)
                desc  = "Data backupu: %s" % date_str
                items.append((title, desc, fullpath, version_str, date_str))
            reactor.callFromThread(self._apply_backup_list, items)

        Thread(target=load_versions).start()

    def _apply_backup_list(self, items):
        if not items:
            items = [(
                _("Brak backupów"),
                _("Naciśnij czerwony przycisk aby utworzyć archiwum"),
                "", "", "",
            )]
        self["list"].setList(items)
        latest = next((item for item in items if item[2]), None)
        if latest:
            self["info"].setText(_("Ostatni backup: %s | wersja: %s") % (latest[4], latest[3]))
        else:
            self["info"].setText(_("Brak archiwów w katalogu backup"))

    def create_archive(self):
        script_path = os.path.join(PLUGIN_PATH, "archive.sh")
        if not os.path.exists(script_path):
            self.session.open(MessageBox, _("Nie znaleziono pliku archive.sh"), MessageBox.TYPE_ERROR, timeout=5)
            return
        cmd = 'chmod +x "{0}" && "{0}"'.format(script_path)
        run_command_in_background(self.session, _("Tworzenie archiwum"), [cmd],
                                  callback_on_finish=self.updateList)

    def restore_selected_backup(self):
        sel = self["list"].getCurrent()
        if not sel or not sel[2]:
            self.session.open(MessageBox, _("Brak wybranego backupu"), MessageBox.TYPE_INFO, timeout=4)
            return
        backup_path = sel[2]
        self.session.openWithCallback(
            self._confirm_restore_callback,
            MessageBox,
            _("Przywrócić backup?\n\n%s") % os.path.basename(backup_path),
            MessageBox.TYPE_YESNO,
        )

    def _confirm_restore_callback(self, answer):
        if not answer:
            return
        sel = self["list"].getCurrent()
        if not sel or not sel[2]:
            return
        backup_path = sel[2]
        cmd = (
            'for item in "{plugin_path}"/*; do '
            '    [ ! -e "$item" ] && continue; '
            '    [ "$item" = "{plugin_path}/backup" ] && continue; '
            '    rm -rf "$item"; '
            'done && '
            'tar -xzf "{archive}" -C "{plugin_path}" && '
            'find "{plugin_path}" -type d ! -path "{plugin_path}/backup" ! -path "{plugin_path}/backup/*" -exec chmod 755 {{}} \\; && '
            'find "{plugin_path}" -type f ! -path "{plugin_path}/backup/*" -exec chmod 644 {{}} \\; && '
            'sync'
        ).format(plugin_path=PLUGIN_PATH, archive=backup_path)
        run_command_in_background(self.session, _("Przywracanie backupu"), [cmd],
                                  callback_on_finish=self.restart_gui)


# ---------------------------------------------------------------------------
# Plugin entry points
# ---------------------------------------------------------------------------

def main(session, **kwargs):
    session.open(ChannelListUpdateMenu)


def menu(menuid, **kwargs):
    if menuid == "scan":
        return [(_("RaczQQ Updater"), main, "Updater by RaczQQ", 0)]
    return []


def Plugins(**kwargs):
    screenwidth = getDesktop(0).size().width()
    icon = "pluginhd.png" if screenwidth == 1920 else "plugin.png"
    return [
        PluginDescriptor(
            name="RaczQQ Updater",
            description=_("Updater by RaczQQ"),
            where=PluginDescriptor.WHERE_MENU,
            fnc=menu,
        ),
        PluginDescriptor(
            name="RaczQQ Updater",
            description=_("Updater by RaczQQ"),
            icon=icon,
            where=PluginDescriptor.WHERE_PLUGINMENU,
            fnc=main,
        ),
    ]
