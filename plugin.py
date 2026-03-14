# -*- coding: utf-8 -*-

# === STANDARD LIBRARY ===
import ConfigParser
import datetime
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import traceback
import zipfile
from threading import Thread

# === TWISTED ===
from twisted.internet import reactor
from twisted.web.client import downloadPage, getPage

# === ENIGMA2 CORE ===
from enigma import eDVBDB, eTimer, getDesktop, gFont, RT_HALIGN_LEFT, RT_VALIGN_CENTER

# === SCREENS ===
from Screens.Console import Console
from Screens.InfoBar import InfoBar
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Screens.Standby import TryQuitMainloop

# === COMPONENTS ===
from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.PluginList import resolveFilename
from Components.Sources.List import List
from Components.Sources.Progress import Progress
from Components.Sources.StaticText import StaticText

# === TOOLS ===
from Tools.Directories import SCOPE_PLUGINS, fileExists
from Tools.Downloader import downloadWithProgress
from Tools.LoadPixmap import LoadPixmap

# === PLUGINS ===
from Plugins.Plugin import PluginDescriptor

# === SKIN ===
from skin import parseColor

# Wykrywanie wersji Pythona
IS_PY2 = sys.version_info[0] < 3
IS_PY3 = sys.version_info[0] >= 3

try:
    _unicode_type = unicode
except Exception:
    _unicode_type = str

def ensure_unicode(val):
    """Return a text (unicode on Py2) representation for safe internal processing."""
    if val is None:
        return u"" if IS_PY2 else ""
    if IS_PY2:
        try:
            if isinstance(val, _unicode_type):
                return val
        except Exception:
            pass
        # bytes -> unicode
        try:
            return val.decode("utf-8", "ignore")
        except Exception:
            try:
                return _unicode_type(str(val), "utf-8", "ignore")
            except Exception:
                return u""
    # Py3
    try:
        return str(val)
    except Exception:
        return ""

PLUGIN_PATH = resolveFilename(SCOPE_PLUGINS) + "Extensions/RaczQQUpdater/"
PLUGIN_TMP_PATH = "/tmp/RaczQQUpdater/"


def reload(self, answer):
    if answer is True:
        TryQuitMainloop(self.session, 3)  # 0=Toggle Standby ; 1=Deep Standby ; 2=Reboot System ; 3=Restart Enigma ; 4=Wake Up ; 5=Enter Standby
    else:
        return

def prepare_tmp_dir():
    if not os.path.exists(PLUGIN_TMP_PATH):
        try:
            os.makedirs(PLUGIN_TMP_PATH)
        except OSError as e:
            print("[RaczQQ Updater] Error creating tmp dir:", e)

def _get_lists_from_repo_sync():
    manifest_url = "https://raw.githubusercontent.com/OliOli2013/PanelAIO-Lists/main/manifest.json"
    tmp_json_path = os.path.join(PLUGIN_TMP_PATH, 'manifest.json')
    prepare_tmp_dir()
    try:
        # identyczna logika jak w org.py
        cmd = "wget --prefer-family=IPv4 --no-check-certificate -U \"Enigma2\" -q -T 20 -O {} {}".format(tmp_json_path, manifest_url)
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, stderr = process.communicate()
        ret_code = process.returncode
        if ret_code != 0:
            print("[RaczQQ Updater] Wget error downloading manifest (code {}): {}".format(ret_code, stderr))
            return []
        if not (os.path.exists(tmp_json_path) and os.path.getsize(tmp_json_path) > 0):
            print("[RaczQQ Updater] Błąd pobierania manifest.json: plik pusty lub nie istnieje")
            return []
    except Exception as e:
        print("[RaczQQ Updater] Błąd pobierania manifest.json (wyjątek):", e)
        return []

    lists_menu = []
    try:
        with io.open(tmp_json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        for item in data:
            item_type = item.get("type", "LIST").upper()
            name = item.get('name', 'Brak nazwy')
            author = item.get('author', '')
            url = item.get('url', '')

            if not url:
                continue

            if item_type == "M3U":
                bouquet_id = item.get('bouquet_id', 'userbouquet.imported_m3u.tv')
                menu_title = "{} - {} (Dodaj Bukiet M3U)".format(name, author)
                action = "m3u:{}:{}:{}".format(url, bouquet_id, name)
                lists_menu.append((menu_title, action))

            elif item_type == "BOUQUET":
                bouquet_id = item.get('bouquet_id', 'userbouquet.imported_ref.tv')
                menu_title = "{} - {} (Dodaj Bukiet REF)".format(name, author)
                action = "bouquet:{}:{}:{}".format(url, bouquet_id, name)
                lists_menu.append((menu_title, action))

            else:
                version = item.get('version', '')
                menu_title = "{} - {} ({})".format(name, author, version)
                action = "archive:{}".format(url)
                lists_menu.append((menu_title, action))

    except Exception as e:
        print("[RaczQQ Updater] Błąd przetwarzania pliku manifest.json:", e)
        return []

    if not lists_menu:
        print("[RaczQQ Updater] Brak list w repozytorium (manifest pusty?)")
        return []

    return lists_menu


class ChannelListUpdateMenu(Screen):
    skin = '''<screen name="ChannelListUpdateMenu" position="center,center" size="750,460" title="RaczQQ Updater">
            <widget source="list" render="Listbox" position="10,10" size="730,240" scrollbarMode="showOnDemand" transparent="1">
                <convert type="TemplatedMultiContent">
                {"template": [
                    MultiContentEntryText(pos = (115, 2), size = (620, 26), font=0, color=0xd282ff, flags = RT_HALIGN_LEFT, text = 0),
                    MultiContentEntryPixmapAlphaBlend(pos = (2, 5), size = (100, 40), png = 1),
                    MultiContentEntryText(pos = (115, 30), size = (620, 26), font=1, flags = RT_VALIGN_TOP | RT_HALIGN_LEFT, text = 3),
                    ],
                    "fonts": [gFont("Regular", 24),gFont("Regular", 22)],
                    "itemHeight": 60
                }
                </convert>
            </widget>
<widget name="key_red" position="59,275" size="150,40" font="Regular;22" halign="center" valign="center" backgroundColor="red" />
<widget name="key_green" position="225,275" size="150,40" font="Regular;22" halign="center" valign="center" backgroundColor="green" />
<widget name="key_yellow" position="391,275" size="150,40" font="Regular;22" halign="center" valign="center" backgroundColor="yellow" />
<widget name="key_blue" position="558,275" size="150,40" font="Regular;22" halign="center" valign="center" backgroundColor="blue" />
<widget name="update" position="4,333" size="740,35" font="Regular;22" halign="center" backgroundColor="black" />
<widget name="info" position="4,373" size="740,35" font="Regular;22" halign="center" backgroundColor="black" />
<widget name="cpu" position="5,417" size="125,30" font="Regular;22" halign="left" foregroundColor="#ff5555" backgroundColor="black" />
<widget name="ram" position="136,417" size="125,30" font="Regular;22" halign="left" foregroundColor="#55ff55" backgroundColor="black" />
<widget name="iplocal" position="267,417" size="205,30" font="Regular;22" halign="left" foregroundColor="#55aaff" backgroundColor="black" />
<widget name="iptun" position="477,417" size="205,30" font="Regular;22" halign="left" foregroundColor="#ffaa00" backgroundColor="black" />

</screen>'''

    def _read_local_version(self, default="unknown"):
        try:
            p = os.path.join(PLUGIN_PATH, "plugin.version")
            with open(p, "r") as f:
                v = f.read().strip()
            return v if v else default
        except Exception:
            return default

    VER = "unknown"
    DATE = str(datetime.date.today())

    def _cleanup_tmp_plugin_dir(self):
        try:
            os.system("rm -rf /tmp/RaczQQUpdater/*")
        except Exception as e:
            print("[RaczQQ Updater] cleanup tmp error:", e)

    def keyRed(self):
        self.session.openWithCallback(
            self._confirm_download_and_install_update,
            MessageBox,
            _("Pobrać i zainstalować aktualizację pluginu?"),
            MessageBox.TYPE_YESNO
        )

    def _confirm_download_and_install_update(self, answer):
        if answer:
            self.download_and_install_update()

    def download_and_install_update(self):
        url = "https://github.com/QraczQQ/RaczQQUpdater/archive/refs/heads/main.zip"
        zip_path = os.path.join(PLUGIN_TMP_PATH, "RaczQQUpdater-main.zip")
        extract_path = os.path.join(PLUGIN_TMP_PATH, "RaczQQUpdater-main")

        prepare_tmp_dir()

        self["update"].setText(_("Pobieranie aktualizacji..."))

        def error(msg):
            print("[RaczQQ Updater] update error:", msg)
            self["update"].setText(_("Błąd aktualizacji: {}").format(msg))
            self.session.open(
                MessageBox,
                _("Błąd aktualizacji:\n{}").format(msg),
                MessageBox.TYPE_ERROR,
                timeout=6
            )

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

                try:
                    if os.path.exists(extract_path):
                        shutil.rmtree(extract_path, ignore_errors=True)
                except Exception:
                    pass

                cmd = (
                    'wget --prefer-family=IPv4 --no-check-certificate '
                    '-U "Enigma2" -q -T 30 -O "{dst}" "{url}"'
                ).format(dst=zip_path, url=url)

                rc = os.system(cmd)
                if rc != 0 or not os.path.exists(zip_path) or os.path.getsize(zip_path) == 0:
                    reactor.callFromThread(error, "nie udało się pobrać archiwum ZIP")
                    return

                try:
                    zf = zipfile.ZipFile(zip_path, "r")
                    zf.extractall(PLUGIN_TMP_PATH)
                    zf.close()
                except Exception as e:
                    reactor.callFromThread(error, "błąd rozpakowania ZIP: %s" % e)
                    return

                src_root = os.path.join(PLUGIN_TMP_PATH, "RaczQQUpdater-main")
                if not os.path.isdir(src_root):
                    reactor.callFromThread(error, "brak katalogu RaczQQUpdater-main po rozpakowaniu")
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
                    reactor.callFromThread(error, "błąd podmiany plików: %s" % e)
                    return

                reactor.callFromThread(finish_ok)

            except Exception as e:
                reactor.callFromThread(error, str(e))

        Thread(target=worker).start()

####MENU####
    MENU_ITEMS = [
        ("live.png", _("Listy kanałów"), "channels", _("13.0E & 19.2E & 23.5E & 28.2E")),
        ("sat.png", _("Pobierz listę satelit"), "sat", _("Aktualizacja listy satelit")),
        ("archive.png", _("Twórz archiwum Pluginu"), "archive", _("RaczQQ Updater")),
    ]

    def __init__(self, session):
        Screen.__init__(self, session)
        self.list = []
        self._prev_cpu = None
        self._health_timer = eTimer()
        self["cpu"] = Label("")
        self["ram"] = Label("")
        self["iplocal"] = Label("")
        self["iptun"] = Label("")
        self["update"] = Label("Sprawdzanie wersji online...")

        self["list"] = List(self.list)
        self["key_red"] = Label("Aktualizacja")
        self["key_red"].hide()
        self["key_green"] = Label("-")
        self["key_yellow"] = Label("Wyczyść TMP")
        self["key_blue"] = Label("Wyczyść RAM")
        self.VER = self._read_local_version("unknown")
        self.DATE = str(datetime.date.today())
        self["info"] = Label(
            "Updater by RaczQQ | Wersja: {} | Data: {} | Python: {}".format(
                self.VER, self.DATE, "Py3" if IS_PY3 else "Py2"
            )
        )
        self["health"] = Label("")

        try:
            self._health_timer_conn = self._health_timer.timeout.connect(self._update_health)
        except Exception:
            self._health_timer.callback.append(self._update_health)

        self["actions"] = ActionMap(
            ["WizardActions", "ColorActions"],
            {"red": self.keyRed, "yellow": self.clear_tmp_cache, "blue": self.clear_ram_memory, "ok": self.KeyOk, "back": self.close}
        )

        self.onShown.append(self._start_health_timer)
        self.onClose.append(self._stop_health_timer)
        self.onClose.append(self._cleanup_tmp_plugin_dir)

        self.updateList()
        self.check_updates(0)
        self._update_health()

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

    def restart_gui(self): self.session.open(TryQuitMainloop, 3)

    def reload_settings_python(self):
        try:
            eDVBDB.getInstance().removeServices()
            eDVBDB.getInstance().reloadServicelist()
            eDVBDB.getInstance().reloadBouquets()
            if InfoBar.instance is not None:
                servicelist = InfoBar.instance.servicelist
                root = servicelist.getRoot()
                currentref = servicelist.getCurrentSelection()
                servicelist.setRoot(root)
                servicelist.setCurrentSelection(currentref)
        except:
            traceback.print_exc()

    def open_channels(self):
        self.session.open(channels)

    def update_sat(self):
        run_command_in_background(
            self.session,
            "Aktualizacja listy satelitów",
            ["bash " + os.path.join(PLUGIN_PATH, "update_satellites_xml.sh")],
            callback_on_finish=self.reload_settings_python
        )

    def open_archive(self):
        self.session.open(ArchiveScreen)

    def KeyOk(self):
        sel = self["list"].getCurrent()
        if not sel:
            return

        actions = {
            "channels": self.open_channels,
            "sat": self.update_sat,
            "archive": self.open_archive,
        }

        action = actions.get(sel[2])
        if action:
            action()

    def updateList(self):
        images_path = os.path.join(
            resolveFilename(SCOPE_PLUGINS),
            "Extensions/RaczQQUpdater/images"
        )

        self["list"].setList([
            (name, LoadPixmap(os.path.join(images_path, icon)), idx, desc)
            for icon, name, idx, desc in self.MENU_ITEMS
        ])

    def quit(self):
        self.close()

    def _read_version_file(self, path, default="unknown"):
        try:
            with open(path, "r") as f:
                v = f.read().strip()
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

    def errorUpdate(self, failure=None):
        self["key_red"].hide()
        self["update"].setText(_("Wersja online: błąd pobierania | Brak informacji o aktualizacji"))
def check_updates(self, tryb=0):
    prepare_tmp_dir()

    self["key_red"].hide()
    self["update"].setText(_("Sprawdzanie wersji online..."))

    url = "https://raw.githubusercontent.com/QraczQQ/RaczQQUpdater/main/plugin.version"
    tmp_version_path = os.path.join(PLUGIN_TMP_PATH, "plugin.version")

    def after_download():
        try:
            local_version = self._read_local_version("unknown")
            online_version = self._read_version_file(tmp_version_path, "unknown")

            print("[RaczQQ Updater] local_version =", local_version)
            print("[RaczQQ Updater] online_version =", online_version)
            print("[RaczQQ Updater] tmp_version_path =", tmp_version_path)

            status = _("Brak aktualizacji.")
            self["key_red"].hide()

            if online_version != "unknown" and self._is_online_version_newer(local_version, online_version):
                status = _("Aktualizacja jest dostępna.")
                self["key_red"].show()

            self["update"].setText(
                _("Wersja online: {} | {}").format(online_version, status)
            )

        except Exception as e:
            print("[RaczQQ Updater] after_download error:", e)
            self["key_red"].hide()
            self["update"].setText(_("Wersja online: błąd odczytu | Brak informacji o aktualizacji"))

    def run_check():
        try:
            if os.path.exists(tmp_version_path):
                os.remove(tmp_version_path)
        except Exception as e:
            print("[RaczQQ Updater] remove old tmp version error:", e)

        cmd = (
            'wget --prefer-family=IPv4 --no-check-certificate '
            '-U "Enigma2" -q -T 15 -O "{dst}" "{url}"'
        ).format(dst=tmp_version_path, url=url)

        rc = os.system(cmd)
        exists = os.path.exists(tmp_version_path)
        size_ok = exists and os.path.getsize(tmp_version_path) > 0

        print("[RaczQQ Updater] wget rc =", rc)
        print("[RaczQQ Updater] tmp exists =", exists)
        print("[RaczQQ Updater] tmp size_ok =", size_ok)

        if rc == 0 and size_ok:
            reactor.callFromThread(after_download)
        else:
            reactor.callFromThread(self.errorUpdate, None)

    Thread(target=run_check).start()

    def clear_ram_memory(self):
        os.system("sync; echo 3 > /proc/sys/vm/drop_caches")
        self.session.open(MessageBox, _("Pamięć RAM została wyczyszczona."), MessageBox.TYPE_INFO, timeout=3)

    def clear_tmp_cache(self):
        try:
            os.system("rm -rf /tmp/*.ipk /tmp/*.zip /tmp/*.tar.gz /tmp/*.tgz /tmp/RaczQQUpdater/*")
            self.session.open(MessageBox, _("Wyczyszczono pamięć podręczną /tmp."), MessageBox.TYPE_INFO, timeout=3)
        except Exception as e:
            self.session.open(MessageBox, _("Błąd: {}".format(e)), MessageBox.TYPE_INFO, timeout=3)

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
            used = (dt - di) * 100.0 / float(dt)
            return max(0.0, min(100.0, used))
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
            pct = (used * 100.0 / float(total)) if total else 0.0
            return pct
        except Exception:
            return None

    def _local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return None

    def _get_ips_from_system(self):
        local_ip = None
        tunneled_ip = None

        try:
            out = subprocess.check_output(["ip", "-4", "-o", "addr", "show"])
            if not isinstance(out, str):
                out = out.decode("utf-8", "ignore")

            for line in out.splitlines():
                # przykład:
                # 2: eth0    inet 192.168.1.10/24 brd ...
                # 5: tun0    inet 10.8.0.2/24 scope global ...
                m = re.search(r'^\d+:\s+([^\s]+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/', line)
                if not m:
                    continue

                iface = m.group(1)
                ip = m.group(2)

                # tunneled IP
                if iface.startswith(("tun", "tap", "ppp", "wg")):
                    if tunneled_ip is None:
                        tunneled_ip = ip
                    continue

                # local IP
                if iface.startswith(("eth", "wlan", "ra", "en")):
                    if not ip.startswith("127."):
                        if local_ip is None:
                            local_ip = ip

            return local_ip, tunneled_ip

        except Exception:
            return None, None

    def _local_ip(self):
        local_ip, tunneled_ip = self._get_ips_from_system()
        return local_ip

    def _tunneled_ip(self):
        local_ip, tunneled_ip = self._get_ips_from_system()
        return tunneled_ip

    def _update_health(self):
        try:
            cpu = self._read_cpu_percent()
            mem = self._read_mem_pct()
            local_ip, tunneled_ip = self._get_ips_from_system()

            cpu_s = "N/A" if cpu is None else "%d%%" % int(cpu)
            mem_s = "N/A" if mem is None else "%d%%" % int(mem)
            local_s = local_ip or "N/A"
            tun_s = tunneled_ip or "N/A"

            self["cpu"].setText("CPU: %s" % cpu_s)
            self["ram"].setText("RAM: %s" % mem_s)
            self["iplocal"].setText("IP: %s" % local_s)
            self["iptun"].setText("IP VPN: %s" % tun_s)
        except Exception:
            pass
        self._start_health_timer()

    def update(self, answer):
        if answer is True:
            self.session.open(ChannelListUpdateUpdater, self.updateurl)
        else:
            return

####Listy kanałów z pliku manifest####
class ManifestChannelsScreen(Screen):
    skin = '''
    <screen name="ManifestChannelsScreen" position="center,center" size="900,560" title="Dostępne listy kanałów">
        <widget source="list" render="Listbox" position="10,10" size="880,480" scrollbarMode="showOnDemand">
            <convert type="StringList" />
        </widget>
        <widget name="status" position="10,500" size="880,40" font="Regular;24" halign="left" valign="center" />
    </screen>'''

    def __init__(self, session, lists_menu):
        Screen.__init__(self, session)
        self.session = session
        self.lists_menu = lists_menu or []

        self["list"] = List([])
        self["status"] = Label("OK - wybierz | EXIT - powrót")

        self["actions"] = ActionMap(
            ["OkCancelActions", "WizardActions", "DirectionActions"],
            {
                "ok": self.keyOk,
                "back": self.close,
                "cancel": self.close,
                "up": self.keyUp,
                "down": self.keyDown
            },
            -1
        )

        self.buildList()

    def buildList(self):
        entries = []
        for title, action in self.lists_menu:
            try:
                title_ui = ensure_unicode(title)
                if IS_PY2:
                    title_ui = title_ui.encode("utf-8")
            except Exception:
                title_ui = str(title)
            entries.append((title_ui, action))

        self["list"].setList(entries)

        if entries:
            self["status"].setText("Załadowano %d pozycji" % len(entries))
        else:
            self["status"].setText("Brak pozycji do wyświetlenia")

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

    def reload_settings_python(self):
        try:
            eDVBDB.getInstance().removeServices()
            eDVBDB.getInstance().reloadServicelist()
            eDVBDB.getInstance().reloadBouquets()
            if InfoBar.instance is not None:
                servicelist = InfoBar.instance.servicelist
                root = servicelist.getRoot()
                currentref = servicelist.getCurrentSelection()
                servicelist.setRoot(root)
                servicelist.setCurrentSelection(currentref)
        except:
            traceback.print_exc()

    def keyOk(self):
        sel = self["list"].getCurrent()
        if not sel:
            return

        title = sel[0]
        action = sel[1]

        if action.startswith("archive:"):
            url = action.split(":", 1)[1]
            self.install_archive(title, url)

        elif action.startswith("m3u:"):
            parts = action.split(":", 3)
            if len(parts) >= 4:
                url = parts[1] + ":" + parts[2]
                rest = parts[3]
                rest_parts = rest.split(":", 1)
                bouquet_id = rest_parts[0]
                bouquet_name = rest_parts[1] if len(rest_parts) > 1 else bouquet_id
                self.install_m3u_as_bouquet(title, url, bouquet_id, bouquet_name)

        elif action.startswith("bouquet:"):
            parts = action.split(":", 3)
            if len(parts) >= 4:
                url = parts[1] + ":" + parts[2]
                rest = parts[3]
                rest_parts = rest.split(":", 1)
                bouquet_id = rest_parts[0]
                bouquet_name = rest_parts[1] if len(rest_parts) > 1 else bouquet_id
                self.install_bouquet_reference(title, url, bouquet_id, bouquet_name)

    def install_archive(self, title, url):
        prepare_tmp_dir()

        if not url.endswith((".zip", ".tar.gz", ".tgz", ".ipk")):
            self.session.open(
                MessageBox,
                _("Nieobsługiwany format archiwum!"),
                MessageBox.TYPE_ERROR,
                timeout=5
            )
            return

        archive_type = "zip" if url.endswith(".zip") else ("tar.gz" if url.endswith((".tar.gz", ".tgz")) else "ipk")
        tmp_archive_path = os.path.join(PLUGIN_TMP_PATH, os.path.basename(url))
        extract_dir = os.path.join(PLUGIN_TMP_PATH, "extracted")

        if archive_type == "ipk":
            cmd = (
                '[ -f "{archive}" ] && rm -f "{archive}"; '
                'wget -T 30 --no-check-certificate -O "{archive}" "{url}" && '
                'opkg install --force-reinstall "{archive}" && '
                '[ -f "{archive}" ] && rm -f "{archive}"; '
                'sync'
            ).format(
                archive=tmp_archive_path,
                url=url
            )
            run_command_in_background(
                self.session,
                title,
                [cmd],
                callback_on_finish=self.reload_settings_python
            )
            return

        extract_cmd = (
            'unzip -o -q "{archive}" -d "{extract}"'
            if archive_type == "zip"
            else 'tar -xzf "{archive}" -C "{extract}"'
        ).format(
            archive=tmp_archive_path,
            extract=extract_dir
        )

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
            'find "$LAMEDB_DIR" -maxdepth 1 -type f \\( '
            '-name "lamedb*" -o '
            '-name "bouquets.*" -o '
            '-name "userbouquet.*" -o '
            '-name "whitelist*" '
            '\\) -exec cp -f {{}} /etc/enigma2/ \\; ; '
            '[ -f /etc/enigma2/bouquets.tv ] || touch /etc/enigma2/bouquets.tv; '
            'for f in /etc/enigma2/userbouquet.jedi*.tv; do '
            '[ -f "$f" ] || continue; '
            'bn=$(basename "$f"); '
            'line=$(printf \'#SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "%s" ORDER BY bouquet\' "$bn"); '
            'grep -Fqx "$line" /etc/enigma2/bouquets.tv || echo "$line" >> /etc/enigma2/bouquets.tv; '
            'done; '
            'fi; '
            '[ -f "{archive}" ] && rm -f "{archive}"; '
            '[ -d "{extract}" ] && rm -rf "{extract}"; '
            'sync'
        ).format(
            archive=tmp_archive_path,
            extract=extract_dir,
            url=url,
            extract_cmd=extract_cmd
        )

        run_command_in_background(
            self.session,
            title,
            [cmd],
            callback_on_finish=self.reload_settings_python
        )

    def install_m3u_as_bouquet(self, title, url, bouquet_id, bouquet_name):
        tmp = os.path.join(PLUGIN_TMP_PATH, "temp.m3u")
        run_command_in_background(
            self.session,
            title,
            ["wget -T 30 --no-check-certificate -O \"{}\" \"{}\"".format(tmp, url)],
            callback_on_finish=lambda: Thread(target=self._parse_m3u_thread, args=(tmp, bouquet_id, bouquet_name)).start()
        )

    def _parse_m3u_thread(self, tmp_path, bid, bname):
        try:
            if not os.path.exists(tmp_path):
                return

            e2 = ["#NAME {}\n".format(bname)]
            with io.open(tmp_path, 'r', encoding='utf-8', errors='ignore') as f:
                name = "N/A"
                for line in f:
                    l = line.strip()
                    if l.startswith('#EXTINF:'):
                        name = l.split(',')[-1].strip()
                    elif l.startswith('http'):
                        e2.append("#SERVICE 4097:0:1:0:0:0:0:0:0:0:{}:{}\n".format(l.replace(':', '%3a'), name))
                        name = "N/A"

            if len(e2) > 1:
                t_bq = os.path.join(PLUGIN_TMP_PATH, bid)
                with open(t_bq, 'w') as f:
                    f.writelines(e2)
                reactor.callFromThread(self._install_parsed_bouquet, t_bq, bid)
        except Exception as e:
            print("[RaczQQ Updater] _parse_m3u_thread error:", e)

    def _install_parsed_bouquet(self, t_bq, bid):
        try:
            shutil.move(t_bq, os.path.join("/etc/enigma2", bid))
            with open("/etc/enigma2/bouquets.tv", 'r+') as f:
                content = f.read()
                if bid not in content:
                    f.write('#SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "{}" ORDER BY bouquet\n'.format(bid))
            self.reload_settings_python()
        except Exception:
            traceback.print_exc()

    def install_bouquet_reference(self, title, url, bid, bname):
        cmd = (
            "wget -qO \"/etc/enigma2/{b}\" \"{u}\" && "
            "(grep -q \"{b}\" /etc/enigma2/bouquets.tv || "
            "echo '#SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET \"{b}\" ORDER BY bouquet' >> /etc/enigma2/bouquets.tv)"
        ).format(b=bid, u=url)

        run_command_in_background(
            self.session,
            title,
            [cmd],
            callback_on_finish=self.reload_settings_python
        )

class channels(Screen):
    skin = '''
    <screen name="channels" position="center,center" size="700,160" title="Pobieranie list">
        <widget name="status" position="20,20" size="660,100" font="Regular;24" halign="center" valign="center" />
    </screen>'''

    def __init__(self, session, args=None):
        Screen.__init__(self, session)
        self.session = session
        self["status"] = Label("Pobieranie manifest.json...\nProszę czekać...")

        self["actions"] = ActionMap(
            ["OkCancelActions", "WizardActions"],
            {
                "back": self.close,
                "cancel": self.close
            },
            -1
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
            self.session.open(MessageBox, _("Nie udało się pobrać list z manifest.json"), MessageBox.TYPE_ERROR, timeout=5)
            self.close()
            return

        self.session.open(ManifestChannelsScreen, lists_menu)
        self.close()

    def _show_error(self, err):
        self.session.open(MessageBox, _("Błąd pobierania manifest.json:\n{}").format(err), MessageBox.TYPE_ERROR, timeout=6)
        self.close()


def main(session, **kwargs):
    session.open(ChannelListUpdateMenu)


# --- FUNKCJA URUCHAMIANIA W TLE (Dla zadań wewnętrznych) ---
def run_command_in_background(session, title, cmd_list, callback_on_finish=None):
    def _finished(*args):
        if callback_on_finish:
            try:
                callback_on_finish()
            except Exception:
                pass

    session.openWithCallback(
        _finished,
        Console,
        title=title,
        cmdlist=cmd_list
    )


    def worker():
        rc = 0
        err = ""

        try:
            for cmd in cmd_list:
                p = subprocess.Popen(
                    cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                out, stderr = p.communicate()
                rc = p.returncode

                if rc != 0:
                    try:
                        err = stderr.decode("utf-8", "ignore")
                    except Exception:
                        err = str(stderr)
                    break
        except Exception as e:
            rc = 1
            err = str(e)

        def finish():
            try:
                wait_message.close()
            except Exception:
                pass

            if rc == 0:
                if callback_on_finish:
                    try:
                        callback_on_finish()
                    except Exception as e:
                        session.open(
                            MessageBox,
                            "Błąd po wykonaniu:\n%s" % str(e),
                            MessageBox.TYPE_ERROR,
                            timeout=8
                        )
            else:
                session.open(
                    MessageBox,
                    "Błąd wykonywania:\n%s" % (err or "Nieznany błąd"),
                    MessageBox.TYPE_ERROR,
                    timeout=8
                )

        reactor.callFromThread(finish)

    Thread(target=worker).start()

class ArchiveScreen(Screen):
    skin = """
    <screen name="ArchiveScreen" position="center,center" size="900,520" title="Archiwum RaczQQ Updater">
        <widget source="key_red" render="Label" position="20,15" size="200,35" font="Regular;24" halign="center" valign="center" backgroundColor="red" transparent="1" />
        <widget source="key_green" render="Label" position="240,15" size="200,35" font="Regular;24" halign="center" valign="center" backgroundColor="green" transparent="1" />
        <widget source="key_yellow" render="Label" position="460,15" size="200,35" font="Regular;24" halign="center" valign="center" backgroundColor="yellow" transparent="1" />
        <widget source="key_blue" render="Label" position="680,15" size="200,35" font="Regular;24" halign="center" valign="center" backgroundColor="blue" transparent="1" />

        <widget source="info" render="Label" position="20,60" size="860,30" font="Regular;22" />

        <widget source="list" render="Listbox" position="20,100" size="860,380" scrollbarMode="showOnDemand">
            <convert type="TemplatedMultiContent">
                {
                    "template": [
                        MultiContentEntryText(pos=(10, 5), size=(520, 30), font=0, flags=RT_HALIGN_LEFT | RT_VALIGN_CENTER, text=0),
                        MultiContentEntryText(pos=(10, 40), size=(830, 26), font=1, flags=RT_HALIGN_LEFT | RT_VALIGN_CENTER, text=1)
                    ],
                    "fonts": [gFont("Regular", 26), gFont("Regular", 22)],
                    "itemHeight": 74
                }
            </convert>
        </widget>
    </screen>
    """

    BACKUP_DIR = os.path.join(PLUGIN_PATH, "backup")

    def __init__(self, session):
        Screen.__init__(self, session)
        self.session = session

        self["key_red"] = StaticText(_("Create archive"))
        self["key_green"] = StaticText(_("Restore backup"))
        self["key_yellow"] = StaticText(_("Odśwież"))
        self["key_blue"] = StaticText(_("Zamknij"))
        self["info"] = StaticText(_("Wybierz backup z listy"))

        self["list"] = List([])

        self["actions"] = ActionMap(
            ["ColorActions", "OkCancelActions"],
            {
                "red": self.create_archive,
                "yellow": self.updateList,
                "green": self.restore_selected_backup,
                "blue": self.close,
                "cancel": self.close,
                "ok": self.restore_selected_backup,
            },
            -1
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
        try:
            tar = tarfile.open(fullpath, "r:gz")
            members = tar.getmembers()

            version_member = None
            for member in members:
                base = os.path.basename(member.name)
                if base == "plugin.version":
                    version_member = member
                    break

            if version_member is not None:
                f = tar.extractfile(version_member)
                if f is not None:
                    version = f.read().strip()
                    tar.close()
                    if version:
                        return version

            tar.close()
        except Exception as e:
            print("[RaczQQ Updater] Blad odczytu wersji z backupu %s: %s" % (fullpath, e))

        return "brak wersji"

    def updateList(self):
        items = []

        if not os.path.isdir(self.BACKUP_DIR):
            try:
                os.makedirs(self.BACKUP_DIR)
            except Exception as e:
                print("[RaczQQ Updater] Nie mozna utworzyc katalogu backup: %s" % e)

        try:
            files = [f for f in os.listdir(self.BACKUP_DIR) if f.endswith(".tar.gz")]
            files.sort(reverse=True)
        except Exception as e:
            print("[RaczQQ Updater] Blad listowania backupow: %s" % e)
            files = []

        for filename in files:
            fullpath = os.path.join(self.BACKUP_DIR, filename)
            date_str = self._get_backup_date(fullpath, filename)
            version_str = self._get_backup_version(fullpath)

            title = "%s  | %s" % (filename, version_str)
            desc = "Data backupu: %s" % date_str
            items.append((title, desc, fullpath, version_str, date_str))

        if not items:
            items.append((
                _("Brak backupów"),
                _("Naciśnij czerwony przycisk aby utworzyć archiwum"),
                "",
                "",
                ""
            ))

        self["list"].setList(items)

        latest = None
        for item in items:
            if item[2]:
                latest = item
                break

        if latest:
            self["info"].setText(
                _("Ostatni backup: %s | wersja: %s") % (latest[4], latest[3])
            )
        else:
            self["info"].setText(_("Brak archiwów w katalogu backup"))

    def create_archive(self):
        script_path = os.path.join(PLUGIN_PATH, "archive.sh")

        if not os.path.exists(script_path):
            self.session.open(
                MessageBox,
                _("Nie znaleziono pliku archive.sh"),
                MessageBox.TYPE_ERROR,
                timeout=5
            )
            return

        title = _("Tworzenie archiwum")
        cmd = 'chmod +x "{0}" && "{0}"'.format(script_path)

        run_command_in_background(
            self.session,
            title,
            [cmd],
            callback_on_finish=self._after_create_archive
        )

    def _after_create_archive(self):
        self.updateList()

    def restore_selected_backup(self):
        sel = self["list"].getCurrent()
        if not sel or not sel[2]:
            self.session.open(
                MessageBox,
                _("Brak wybranego backupu"),
                MessageBox.TYPE_INFO,
                timeout=4
            )
            return

        backup_path = sel[2]
        msg = _("Przywrócić backup?\n\n%s") % os.path.basename(backup_path)
        self.session.openWithCallback(
            self._confirm_restore_callback,
            MessageBox,
            msg,
            MessageBox.TYPE_YESNO
        )

    def _confirm_restore_callback(self, answer):
        if not answer:
            return

        sel = self["list"].getCurrent()
        if not sel or not sel[2]:
            return

        backup_path = sel[2]

        cmd = (
            'tar -xzf "{archive}" -C / && '
            'sync'
        ).format(archive=backup_path)

        run_command_in_background(
            self.session,
            _("Przywracanie backupu"),
            [cmd],
            callback_on_finish=self.restart_gui
        )

def menu(menuid, **kwargs):
    if menuid == "scan":
        return [(_("RaczQQ Updater"), main, "Updater by RaczQQ", 0)]
    return []


def Plugins(**kwargs):
    screenwidth = getDesktop(0).size().width()
    if screenwidth and screenwidth == 1920:
        return [
            PluginDescriptor(name="RaczQQ Updater", description=_('Updater by RaczQQ'),
                             where=PluginDescriptor.WHERE_MENU, fnc=menu),
            PluginDescriptor(name="RaczQQ Updater", description=_('Updater by RaczQQ'), icon='pluginhd.png',
                             where=PluginDescriptor.WHERE_PLUGINMENU, fnc=main)
        ]
    else:
        return [
            PluginDescriptor(name="RaczQQ Updater", description=_('Updater by RaczQQ'),
                             where=PluginDescriptor.WHERE_MENU, fnc=menu),
            PluginDescriptor(name="RaczQQ Updater", description=_('Updater by RaczQQ'), icon='plugin.png',
                             where=PluginDescriptor.WHERE_PLUGINMENU, fnc=main)
        ]
