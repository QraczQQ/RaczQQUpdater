# -*- coding: utf-8 -*-

import http.cookiejar
import io
import os
import shutil
import ssl
import subprocess
import tarfile
import time
import urllib.request
import urllib.error
import zipfile
from datetime import datetime
from threading import Thread

from twisted.internet import reactor

from enigma import gFont, RT_HALIGN_LEFT, RT_VALIGN_CENTER

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


PLUGIN_TMP_PATH     = "/tmp/RaczQQUpdater/"
PICON_TMP_DIR       = os.path.join(PLUGIN_TMP_PATH, "picony")
PLUGIN_LOG_FILE     = os.path.join(PLUGIN_TMP_PATH, "install.log")
PICON_DOWNLOAD_BASE = "https://picon.cz/download/"
PICON_LIST_URLS = [
    "https://github.com/s3n0/e2plugins/blob/master/ChocholousekPicons"
    "/src/id_for_permalinks%28240501%29.log?raw=true",
    "https://raw.githubusercontent.com/s3n0/e2plugins/master/ChocholousekPicons"
    "/src/id_for_permalinks%28240501%29.log",
    "https://cdn.jsdelivr.net/gh/s3n0/e2plugins@master/ChocholousekPicons"
    "/src/id_for_permalinks%28240501%29.log",
]

DOWNLOAD_CHUNK   = 65536
DOWNLOAD_TIMEOUT = 120
PLUGIN_VERSION   = "1.2.0"
RUNTIME_CONFIG_FILE = "/etc/enigma2/RaczQQUpdater.conf"

DEFAULT_RUNTIME_SETTINGS = {
    "vpn_pause_enabled": False,
    "vpn_stop_cmd": "",
    "vpn_start_cmd": "",
    "vpn_wait_down": 4,
    "vpn_wait_up": 10,
}

_RUNTIME_SETTINGS = None


# ---------------------------------------------------------------------------
# Pomocnicze funkcje modułu
# ---------------------------------------------------------------------------

def ensure_dir(path):
    if not os.path.exists(path):
        try:
            os.makedirs(path)
        except OSError:
            pass


def _parse_bool(value, default=False):
    if value is None:
        return default
    txt = str(value).strip().lower()
    if txt in ("1", "true", "yes", "on", "y", "tak"):
        return True
    if txt in ("0", "false", "no", "off", "n", "nie"):
        return False
    return default


def _parse_int(value, default=0):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _log(msg):
    try:
        ensure_dir(PLUGIN_TMP_PATH)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with io.open(PLUGIN_LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (stamp, msg))
    except Exception:
        pass


def _load_runtime_settings():
    settings = dict(DEFAULT_RUNTIME_SETTINGS)
    try:
        if os.path.exists(RUNTIME_CONFIG_FILE):
            with io.open(RUNTIME_CONFIG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if (not line) or line.startswith("#") or ("=" not in line):
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip().lower()
                    value = value.strip()

                    if key in ("vpn_pause_enabled", "pause_vpn_before_download", "openvpn_pause_enabled"):
                        settings["vpn_pause_enabled"] = _parse_bool(value, settings["vpn_pause_enabled"])
                    elif key in ("vpn_stop_cmd", "openvpn_stop_cmd"):
                        settings["vpn_stop_cmd"] = value
                    elif key in ("vpn_start_cmd", "openvpn_start_cmd"):
                        settings["vpn_start_cmd"] = value
                    elif key in ("vpn_wait_down", "openvpn_wait_down"):
                        settings["vpn_wait_down"] = _parse_int(value, settings["vpn_wait_down"])
                    elif key in ("vpn_wait_up", "openvpn_wait_up"):
                        settings["vpn_wait_up"] = _parse_int(value, settings["vpn_wait_up"])
    except Exception:
        pass
    return settings


def _get_runtime_settings():
    global _RUNTIME_SETTINGS
    if _RUNTIME_SETTINGS is None:
        _RUNTIME_SETTINGS = _load_runtime_settings()
    return _RUNTIME_SETTINGS


def _fmt_size(size_bytes):
    if size_bytes >= 1024 * 1024:
        return "%.1f MB" % (size_bytes / (1024.0 * 1024.0))
    elif size_bytes >= 1024:
        return "%.1f KB" % (size_bytes / 1024.0)
    return "%d B" % size_bytes


def _detect_archive_type(path):
    try:
        with open(path, "rb") as f:
            header = f.read(6)
        if header[:2] == b"PK":
            return "zip"
        if header[:2] == b"\x1f\x8b":
            return "tar.gz"
        if header[:6] == b"7z\xbc\xaf'\x1c":
            return "7z"
    except Exception:
        pass
    return None


def _find_7za():
    for binary in ("7za", "7z"):
        try:
            proc = subprocess.Popen(["which", binary], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, _ = proc.communicate()
            path = out.decode("utf-8", "ignore").strip()
            if path and os.path.exists(path):
                return path
        except Exception:
            pass
    return None


def _head_info(path, max_bytes=128):
    try:
        with open(path, "rb") as f:
            head = f.read(max_bytes)
        head_hex = " ".join("%02x" % b for b in head[:16])
        head_ascii = "".join(chr(b) if 32 <= b < 127 else "." for b in head[:64])
        return head_hex, head_ascii
    except Exception:
        return "?", "?"


def _which(binary):
    for path_dir in (os.environ.get("PATH") or "").split(":"):
        candidate = os.path.join(path_dir, binary)
        if candidate and os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _run_command(cmd):
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate()
        return (
            proc.returncode,
            out.decode("utf-8", "ignore").strip(),
            err.decode("utf-8", "ignore").strip(),
        )
    except Exception as e:
        return -1, "", str(e)


def _run_shell_command(cmd_text, timeout=30):
    if not cmd_text:
        return -1, "", "empty command"
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", cmd_text],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except TypeError:
            out, err = proc.communicate()
        return (
            proc.returncode,
            out.decode("utf-8", "ignore").strip(),
            err.decode("utf-8", "ignore").strip(),
        )
    except Exception as e:
        return -1, "", str(e)


def _detect_openvpn_commands():
    if os.path.exists("/etc/init.d/openvpn"):
        return "/etc/init.d/openvpn stop", "/etc/init.d/openvpn start"
    if _which("systemctl"):
        return "systemctl stop openvpn", "systemctl start openvpn"
    if _which("service"):
        return "service openvpn stop", "service openvpn start"
    return "", ""


def _get_vpn_command_pair():
    settings = _get_runtime_settings()
    auto_stop_cmd, auto_start_cmd = _detect_openvpn_commands()
    return (
        settings.get("vpn_stop_cmd") or auto_stop_cmd,
        settings.get("vpn_start_cmd") or auto_start_cmd,
    )


def _vpn_is_running():
    rc, out, _err = _run_command(["pidof", "openvpn"])
    return (rc == 0) and bool((out or "").strip())


def _wait_for_vpn_state(want_running, timeout_secs):
    end_time = time.time() + max(0, int(timeout_secs))
    while time.time() <= end_time:
        running = _vpn_is_running()
        if bool(running) == bool(want_running):
            return True
        time.sleep(0.5)
    return bool(_vpn_is_running()) == bool(want_running)


def _pause_openvpn_for_download(status_cb=None):
    settings = _get_runtime_settings()
    if not settings.get("vpn_pause_enabled"):
        return {"enabled": False, "should_resume": False}

    stop_cmd, start_cmd = _get_vpn_command_pair()
    if not stop_cmd or not start_cmd:
        raise Exception(
            "Brak komendy stop/start dla OpenVPN.\n"
            "Ustaw vpn_stop_cmd i vpn_start_cmd w %s" % RUNTIME_CONFIG_FILE
        )

    if not _vpn_is_running():
        return {
            "enabled": True,
            "was_running": False,
            "should_resume": False,
            "start_cmd": start_cmd,
        }

    if status_cb:
        try:
            status_cb(_("Zatrzymywanie OpenVPN..."))
        except Exception:
            pass

    rc, out, err = _run_shell_command(stop_cmd, timeout=30)
    _log("VPN stop rc=%s" % rc)

    wait_down = max(1, int(settings.get("vpn_wait_down") or 4))
    if (rc != 0) and _vpn_is_running():
        raise Exception(
            "Nie udalo sie zatrzymac OpenVPN.\nKomenda: %s\nBlad: %s" % (
                stop_cmd,
                (err or out or ("rc=%s" % rc))[:300],
            )
        )

    if not _wait_for_vpn_state(False, wait_down):
        raise Exception(
            "OpenVPN nadal dziala po probie zatrzymania.\nKomenda: %s" % stop_cmd
        )

    return {
        "enabled": True,
        "was_running": True,
        "should_resume": True,
        "start_cmd": start_cmd,
    }


def _resume_openvpn_after_download(vpn_state, status_cb=None):
    if not vpn_state or not vpn_state.get("should_resume"):
        return

    settings = _get_runtime_settings()
    start_cmd = vpn_state.get("start_cmd") or ""
    if not start_cmd:
        raise Exception("Brak komendy start dla OpenVPN.")

    if status_cb:
        try:
            status_cb(_("Uruchamianie OpenVPN..."))
        except Exception:
            pass

    rc, out, err = _run_shell_command(start_cmd, timeout=30)
    _log("VPN start rc=%s" % rc)

    wait_up = max(1, int(settings.get("vpn_wait_up") or 10))
    if (rc != 0) and (not _vpn_is_running()):
        raise Exception(
            "Nie udalo sie uruchomic OpenVPN ponownie.\nKomenda: %s\nBlad: %s" % (
                start_cmd,
                (err or out or ("rc=%s" % rc))[:300],
            )
        )

    if not _wait_for_vpn_state(True, wait_up):
        raise Exception(
            "OpenVPN nie uruchomil sie ponownie w oczekiwanym czasie.\nKomenda: %s" % start_cmd
        )


def _build_headers(url):
    return {
        "User-Agent": "Enigma2-Plugin/%s" % PLUGIN_VERSION,
        "Referer": url,
    }


def _build_opener():
    try:
        ssl_ctx = ssl._create_unverified_context()
    except AttributeError:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ssl_ctx),
    )
    return opener


class DownloadValidationError(Exception):
    pass


def _is_download_block_page(path, meta):
    _head_hex, head_ascii = _head_info(path, 512)
    final_url = (meta.get("final_url") or "").lower()
    ctype = (meta.get("content_type") or "").lower()
    ascii_low = head_ascii.lower()
    if "error-download-vp" in final_url or "error-download" in final_url:
        return True
    if "text/html" in ctype and ("error-download-vp" in ascii_low or "download rules" in ascii_low):
        return True
    return False


def _download_request(opener, url, dest, progress_cb=None):
    meta = {
        "downloaded": 0,
        "total": 0,
        "content_type": "",
        "final_url": url,
    }

    ensure_dir(os.path.dirname(dest))
    req = urllib.request.Request(url, data=None, headers=_build_headers(url))
    with opener.open(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        meta["final_url"] = resp.geturl()
        meta["content_type"] = resp.headers.get("Content-Type", "")
        total = int(resp.headers.get("Content-Length") or 0)
        meta["total"] = total

        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                meta["downloaded"] += len(chunk)
                if progress_cb:
                    try:
                        progress_cb(meta["downloaded"], total)
                    except Exception:
                        pass
    return meta


def _urllib_download(url, dest, progress_cb=None):
    opener = _build_opener()
    return _download_request(opener, url, dest, progress_cb)


def _urllib_download_cookie_ping(url, dest, progress_cb=None):
    opener = _build_opener()
    try:
        opener.open(
            urllib.request.Request(
                "https://picon.cz/",
                data=None,
                headers=_build_headers("https://picon.cz/"),
            ),
            timeout=10,
        ).close()
    except Exception:
        pass
    return _download_request(opener, url, dest, progress_cb)


def _validate_downloaded_archive(path, meta):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise DownloadValidationError("Pobrano 0 bajtow — plik jest pusty!")

    archive_type = _detect_archive_type(path)
    if archive_type:
        return archive_type

    head_hex, head_ascii = _head_info(path)
    is_html = head_ascii.lstrip().startswith(("<", "<!"))

    if _is_download_block_page(path, meta):
        raise DownloadValidationError(
            "Serwer picon.cz zablokowal pobieranie i zamiast archiwum zwrocil strone Error-DownLoad-VP.\n\n"
            "To nie jest blad rozpakowania 7z, tylko blokada po stronie serwera.\n"
            "Najczestsze przyczyny: VPN / proxy / CGNAT / nierozpoznane IP / zbyt czeste proby pobrania.\n\n"
            "Final URL: %s\nContent-Type: %s\nLog: %s" % (
                meta.get("final_url") or "brak",
                meta.get("content_type") or "brak",
                PLUGIN_LOG_FILE,
            )
        )

    raise DownloadValidationError(
        "Nieznany format archiwum%s\n"
        "Rozmiar: %s  Content-Type: %s\n"
        "URL: %s\n"
        "Hex: %s\n"
        "Tekst: %s" % (
            " (HTML — blad serwera!)" if is_html else "",
            _fmt_size(os.path.getsize(path)),
            meta.get("content_type") or "brak",
            meta.get("final_url") or "brak",
            head_hex,
            head_ascii[:64],
        )
    )


def _download_archive_with_retry(url, dest, progress_cb=None):
    errors = []
    for label, func in (("direct", _urllib_download), ("cookie-ping", _urllib_download_cookie_ping)):
        try:
            if os.path.exists(dest):
                os.remove(dest)
            meta = func(url, dest, progress_cb=progress_cb)
            archive_type = _validate_downloaded_archive(dest, meta)
            meta["archive_type"] = archive_type
            _log(
                "DOWNLOAD %s ok size=%s type=%s final=%s" % (
                    label,
                    _fmt_size(meta.get("downloaded") or 0),
                    meta.get("content_type") or "brak",
                    meta.get("final_url") or "brak",
                )
            )
            return meta
        except Exception as e:
            errors.append("%s: %s" % (label, e))
    raise Exception(" ; ".join(errors) or "Nie udalo sie pobrac archiwum.")


# ---------------------------------------------------------------------------
# PiconyScreen
# ---------------------------------------------------------------------------

class PiconyScreen(Screen):
    skin = """
    <screen name="PiconyScreen" position="center,center" size="900,560"
            title="Menedzer Piconow — picon.cz">

        <eLabel position="0,0" size="900,4" backgroundColor="#d282ff" />

        <widget name="key_red"    position="20,10"  size="190,32" font="Regular;20" halign="center" valign="center" foregroundColor="#ffffff" backgroundColor="#c43b3b" />
        <widget name="key_yellow" position="235,10" size="190,32" font="Regular;20" halign="center" valign="center" foregroundColor="#000000" backgroundColor="#d8c13f" />
        <widget name="key_blue"   position="450,10" size="190,32" font="Regular;20" halign="center" valign="center" foregroundColor="#ffffff" backgroundColor="#3a78c9" />

        <eLabel position="10,48" size="880,1" backgroundColor="#332244" />

        <widget source="list" render="Listbox"
                position="10,54" size="880,390"
                scrollbarMode="showOnDemand" transparent="1">
            <convert type="TemplatedMultiContent">
            {"template": [
                MultiContentEntryText(pos=(8,5),  size=(870,28), font=0,
                    color=0xffffff, flags=RT_HALIGN_LEFT|RT_VALIGN_CENTER, text=0),
                MultiContentEntryText(pos=(8,35), size=(870,20), font=1,
                    color=0x666666, flags=RT_HALIGN_LEFT|RT_VALIGN_CENTER, text=1)
            ],
            "fonts": [gFont("Regular",24), gFont("Regular",18)],
            "itemHeight": 58
            }
            </convert>
        </widget>

        <eLabel position="10,448" size="880,2" backgroundColor="#d282ff" />

        <widget name="status" position="10,454" size="880,26"
                font="Regular;20" halign="center"
                foregroundColor="#aaaaaa" backgroundColor="black" />
        <widget name="target" position="10,484" size="880,24"
                font="Regular;19" halign="center"
                foregroundColor="#55aaff" backgroundColor="black" />
        <widget name="count"  position="10,512" size="880,22"
                font="Regular;18" halign="center"
                foregroundColor="#505050" backgroundColor="black" />

        <eLabel position="0,556" size="900,4" backgroundColor="#d282ff" />

    </screen>"""

    TARGET_DIRS = [
        "/usr/share/enigma2/picons/",
        "/picon/",
        "/media/hdd/picon/",
        "/media/usb/picon/",
    ]

    def __init__(self, session):
        Screen.__init__(self, session)
        self.session = session

        self._all_packs = []
        self._filtered_packs = []
        self._search_query = ""
        self.target_dir_index = 0

        self["key_red"] = Label(_("Szukaj"))
        self["key_yellow"] = Label(_("Katalog"))
        self["key_blue"] = Label(_("Wyczysc"))
        self["list"] = List([])
        self["status"] = Label(_("Pobieranie listy piconow z GitHub..."))
        self["target"] = Label("")
        self["count"] = Label("")

        self["actions"] = ActionMap(
            ["OkCancelActions", "ColorActions", "DirectionActions"],
            {
                "ok": self.keyOK,
                "cancel": self.close,
                "back": self.close,
                "red": self.openSearch,
                "yellow": self.changeTargetDir,
                "blue": self.clearTargetDir,
                "up": self.keyUp,
                "down": self.keyDown,
            },
            -1,
        )

        self._refreshTargetLabel()
        self._loadListAsync()

    def _setStatus(self, msg):
        try:
            self["status"].setText(msg)
        except Exception:
            pass

    def _loadListAsync(self):
        def worker():
            log_path = os.path.join(PICON_TMP_DIR, "id_for_permalinks.log")
            ensure_dir(PICON_TMP_DIR)

            success = False
            for idx, url in enumerate(PICON_LIST_URLS, 1):
                reactor.callFromThread(
                    self._setStatus,
                    _("Pobieranie listy... (proba %d/%d)") % (idx, len(PICON_LIST_URLS)),
                )
                try:
                    proc = subprocess.Popen(
                        ["wget", "--no-check-certificate", "--prefer-family=IPv4",
                         "-q", "--tries=3", "--timeout=20", "-O", log_path, url],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    proc.communicate()
                    if os.path.exists(log_path) and os.path.getsize(log_path) > 1024:
                        success = True
                        break
                except Exception:
                    pass
                try:
                    os.remove(log_path)
                except Exception:
                    pass

            if not success:
                reactor.callFromThread(
                    self._setStatus,
                    _("Blad: nie udalo sie pobrac listy. Sprawdz polaczenie."),
                )
                return

            packs = []
            try:
                with io.open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.rstrip("\n\r")
                        if len(line) >= 5:
                            pack_id = line[:4].strip()
                            name = line[4:].strip()
                            if pack_id and name:
                                packs.append((name, pack_id))
            except Exception as e:
                reactor.callFromThread(self._setStatus, _("Blad parsowania listy: %s") % str(e))
                return

            reactor.callFromThread(self._applyFullList, packs)

        Thread(target=worker, daemon=True).start()

    def _applyFullList(self, packs):
        self._all_packs = packs
        self._filtered_packs = list(packs)
        self._rebuildListWidget()
        self._setStatus(_("Wybierz paczke i nacisnij OK  |  Czerwony = szukaj"))

    def openSearch(self):
        try:
            from Screens.VirtualKeyBoard import VirtualKeyBoard
            self.session.openWithCallback(
                self._searchCallback,
                VirtualKeyBoard,
                title=_("Szukaj paczki piconow:"),
                text=self._search_query,
            )
        except Exception:
            self._search_query = ""
            self._applyFilter()
            self._setStatus(_("Wyszukiwarka niedostepna — wyswietlono pelna liste"))

    def _searchCallback(self, text):
        if text is not None:
            self._search_query = text.strip()
        self._applyFilter()

    def _applyFilter(self):
        q = self._search_query.lower()
        if q:
            self._filtered_packs = [(n, i) for n, i in self._all_packs if q in n.lower()]
        else:
            self._filtered_packs = list(self._all_packs)
        self._rebuildListWidget()

    def _rebuildListWidget(self):
        entries = []
        for name, pack_id in self._filtered_packs:
            subtitle = "ID: %s  |  %s%s/" % (pack_id, PICON_DOWNLOAD_BASE, pack_id)
            entries.append((name, subtitle, name, pack_id))
        self["list"].setList(entries)

        total = len(self._all_packs)
        shown = len(self._filtered_packs)
        q = self._search_query
        if q:
            self["count"].setText(_('Filtr: "%s" — %d / %d wynikow') % (q, shown, total))
        elif total:
            self["count"].setText(_("%d paczek piconow dostepnych") % total)
        else:
            self["count"].setText("")

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

    def _refreshTargetLabel(self):
        self["target"].setText(_("Katalog docelowy: %s") % self.getTargetDir())

    def getTargetDir(self):
        return self.TARGET_DIRS[self.target_dir_index]

    def changeTargetDir(self):
        self.target_dir_index = (self.target_dir_index + 1) % len(self.TARGET_DIRS)
        self._refreshTargetLabel()

    def clearTargetDir(self):
        target = self.getTargetDir()
        self.session.openWithCallback(
            lambda answer: self._doClear(answer, target),
            MessageBox,
            _("Usunac wszystkie pliki PNG z katalogu?\n\n%s") % target,
            MessageBox.TYPE_YESNO,
        )

    def _doClear(self, answer, target):
        if not answer:
            return
        removed = 0
        try:
            ensure_dir(target)
            for fname in os.listdir(target):
                if fname.lower().endswith(".png"):
                    try:
                        os.remove(os.path.join(target, fname))
                        removed += 1
                    except Exception:
                        pass
            self.session.open(
                MessageBox,
                _("Usunieto %d plikow PNG z:\n%s") % (removed, target),
                MessageBox.TYPE_INFO, timeout=5,
            )
        except Exception as e:
            self.session.open(
                MessageBox,
                _("Blad czyszczenia:\n%s") % str(e),
                MessageBox.TYPE_ERROR, timeout=6,
            )

    def keyOK(self):
        sel = self["list"].getCurrent()
        if not sel:
            return
        name = sel[0]
        pack_id = sel[3]
        url = PICON_DOWNLOAD_BASE + pack_id + "/"

        self.session.openWithCallback(
            lambda answer: self._confirmInstall(answer, name, pack_id, url),
            MessageBox,
            _("Zainstalowac paczke piconow?\n\n%s\n\nKatalog docelowy:\n%s") % (
                name, self.getTargetDir()),
            MessageBox.TYPE_YESNO,
        )

    def _confirmInstall(self, answer, name, pack_id, url):
        if not answer:
            return

        target_dir = self.getTargetDir()
        archive_path = os.path.join(PICON_TMP_DIR, "picon_%s.download" % pack_id)
        extract_dir = os.path.join(PICON_TMP_DIR, "extract_%s" % pack_id)

        ensure_dir(PICON_TMP_DIR)
        ensure_dir(target_dir)

        if _find_7za() is None:
            self.session.openWithCallback(
                lambda ans: self._install7zThenDownload(
                    ans, name, pack_id, url, archive_path, extract_dir, target_dir),
                MessageBox,
                _("Pakiet 7zip nie jest zainstalowany.\n\nCzy zainstalowac go teraz?\n(opkg install 7zip)"),
                MessageBox.TYPE_YESNO,
            )
        else:
            self._doDownload(name, pack_id, url, archive_path, extract_dir, target_dir)

    def _install7zThenDownload(self, answer, name, pack_id, url,
                               archive_path, extract_dir, target_dir):
        if not answer:
            self._setStatus(_("Anulowano — 7zip jest wymagany."))
            return

        self._setStatus(_("Instalacja 7zip przez opkg..."))
        cmd = (
            "echo '>>> Instalacja 7zip...' && "
            "opkg update && opkg install 7zip && "
            "echo '>>> 7zip zainstalowany.' "
            "|| { echo '>>> BLAD instalacji 7zip!'; exit 1; }"
        )
        self.session.openWithCallback(
            lambda *a: self._after7zInstall(name, pack_id, url, archive_path, extract_dir, target_dir),
            Console,
            title=_("Instalacja 7zip"),
            cmdlist=[cmd],
            closeOnSuccess=False,
        )

    def _after7zInstall(self, name, pack_id, url, archive_path, extract_dir, target_dir):
        if _find_7za() is None:
            self._setStatus(_("Blad: 7zip nadal niedostepny."))
            self.session.open(
                MessageBox,
                _("Nie udalo sie zainstalowac 7zip.\n\nSprobuj recznie:\n  opkg install 7zip"),
                MessageBox.TYPE_ERROR, timeout=10,
            )
            return
        self._doDownload(name, pack_id, url, archive_path, extract_dir, target_dir)

    def _doDownload(self, name, pack_id, url, archive_path, extract_dir, target_dir):
        self._setStatus(_("Laczenie z picon.cz..."))
        _log("INSTALL start pack=%s id=%s" % (name, pack_id))

        def _on_progress(downloaded, total):
            if total:
                pct = int(downloaded * 100 / total)
                reactor.callFromThread(
                    self._setStatus,
                    _("Pobieranie %s — %d%% (%s / %s)") % (
                        name, pct, _fmt_size(downloaded), _fmt_size(total)),
                )
            else:
                reactor.callFromThread(
                    self._setStatus,
                    _("Pobieranie %s — %s") % (name, _fmt_size(downloaded)),
                )

        def worker():
            vpn_state = None
            vpn_warning = ""
            try:
                ensure_dir(PICON_TMP_DIR)
                ensure_dir(extract_dir)
                try:
                    os.remove(archive_path)
                except Exception:
                    pass

                vpn_state = _pause_openvpn_for_download(
                    status_cb=lambda txt: reactor.callFromThread(self._setStatus, txt)
                )

                meta = _download_archive_with_retry(url, archive_path, progress_cb=_on_progress)
                downloaded = meta["downloaded"]
                content_type = meta["content_type"]
                final_url = meta["final_url"]
                archive_type = meta.get("archive_type", "")

                if vpn_state and vpn_state.get("should_resume"):
                    try:
                        _resume_openvpn_after_download(
                            vpn_state,
                            status_cb=lambda txt: reactor.callFromThread(self._setStatus, txt)
                        )
                    except Exception as e:
                        vpn_warning = str(e)
                    finally:
                        vpn_state = None

                reactor.callFromThread(
                    self._setStatus,
                    _("Pobrano %s — rozpakowywanie...") % _fmt_size(downloaded),
                )
                reactor.callFromThread(
                    self._afterDownload,
                    name, archive_path, extract_dir, target_dir,
                    content_type, final_url, archive_type, vpn_warning,
                )

            except urllib.error.HTTPError as e:
                msg = "HTTP %d: %s\nURL: %s" % (e.code, e.reason, url)
                if vpn_state and vpn_state.get("should_resume"):
                    try:
                        _resume_openvpn_after_download(vpn_state)
                    except Exception as re:
                        msg += "\n\nUWAGA: %s" % str(re)
                reactor.callFromThread(self._downloadError, msg)
            except urllib.error.URLError as e:
                msg = str(e.reason)
                if vpn_state and vpn_state.get("should_resume"):
                    try:
                        _resume_openvpn_after_download(vpn_state)
                    except Exception as re:
                        msg += "\n\nUWAGA: %s" % str(re)
                reactor.callFromThread(self._downloadError, msg)
            except Exception as e:
                msg = str(e)
                if vpn_state and vpn_state.get("should_resume"):
                    try:
                        _resume_openvpn_after_download(vpn_state)
                    except Exception as re:
                        msg += "\n\nUWAGA: %s" % str(re)
                reactor.callFromThread(self._downloadError, msg)

        Thread(target=worker, daemon=True).start()

    def _downloadError(self, msg):
        _log("ERROR download: %s" % msg.replace("\n", " | "))
        self._setStatus(_("Blad pobierania!"))
        self.session.open(
            MessageBox,
            _("Blad pobierania piconow:\n%s\n\nLog: %s") % (msg, PLUGIN_LOG_FILE),
            MessageBox.TYPE_ERROR, timeout=12,
        )

    def _afterDownload(self, name, archive_path, extract_dir, target_dir,
                       content_type="", final_url="", archive_type="", vpn_warning=""):
        try:
            if not os.path.exists(archive_path) or os.path.getsize(archive_path) == 0:
                raise Exception("Plik archiwum jest pusty lub nie istnieje.")

            archive_type = archive_type or _detect_archive_type(archive_path)
            self._setStatus(_("Rozpakowywanie (%s)...") % (archive_type or "?"))
            ensure_dir(extract_dir)

            if archive_type == "zip":
                with zipfile.ZipFile(archive_path, "r") as zf:
                    zf.extractall(extract_dir)
            elif archive_type == "tar.gz":
                with tarfile.open(archive_path, "r:gz") as tf:
                    tf.extractall(extract_dir)
            elif archive_type == "7z":
                bin_7z = _find_7za()
                if not bin_7z:
                    raise Exception(
                        "Archiwum 7z — binarki 7za/7z nie znaleziono.\n"
                        "Zainstaluj: opkg install 7zip"
                    )
                proc = subprocess.Popen(
                    [bin_7z, "e", "-y", "-o" + extract_dir, archive_path, "*.png"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                _out, _err = proc.communicate()
                if proc.returncode != 0:
                    out_txt = _out.decode("utf-8", "ignore").strip()
                    err_txt = _err.decode("utf-8", "ignore").strip()
                    raise Exception(
                        "Blad %s (kod %d):\n%s" % (
                            bin_7z,
                            proc.returncode,
                            (err_txt or out_txt or "brak wyjscia")[:400],
                        )
                    )
            else:
                head_hex, head_ascii = _head_info(archive_path)
                is_html = head_ascii.lstrip().startswith(("<", "<!"))
                raise Exception(
                    "Nieznany format archiwum%s\n"
                    "Rozmiar: %s  Content-Type: %s\n"
                    "URL: ...%s\n"
                    "Hex: %s\n"
                    "Tekst: %s" % (
                        " (HTML — blad serwera!)" if is_html else "",
                        _fmt_size(os.path.getsize(archive_path)),
                        content_type or "brak",
                        (final_url or "")[-60:],
                        head_hex,
                        head_ascii[:48],
                    )
                )

            self._setStatus(_("Kopiowanie plikow PNG..."))
            copied = 0
            for root, _dirs, files in os.walk(extract_dir):
                for fname in files:
                    if fname.lower().endswith(".png"):
                        src = os.path.join(root, fname)
                        dst = os.path.join(target_dir, fname)
                        shutil.copy2(src, dst)
                        copied += 1

            try:
                shutil.rmtree(extract_dir, ignore_errors=True)
                os.remove(archive_path)
            except Exception:
                pass

            if copied == 0:
                raise Exception(
                    "Nie znaleziono plikow *.png w archiwum.\n"
                    "Paczka moze byc uszkodzona lub pusta."
                )

            _log("INSTALL success pack=%s copied=%d" % (name, copied))
            self._setStatus(_("Zainstalowano %d piconow") % copied)
            success_msg = _("Zainstalowano %d piconow\nz paczki: %s\n\nKatalog: %s") % (
                copied, name, target_dir)
            if vpn_warning:
                success_msg += _("\n\nUWAGA: OpenVPN nie zostal poprawnie uruchomiony ponownie.\n%s") % vpn_warning
            self.session.open(
                MessageBox,
                success_msg,
                MessageBox.TYPE_INFO, timeout=12,
            )

        except Exception as e:
            _log("ERROR install: %s" % str(e).replace("\n", " | "))
            self._setStatus(_("Blad instalacji!"))
            self.session.open(
                MessageBox,
                _("Blad instalacji piconow:\n%s") % str(e),
                MessageBox.TYPE_ERROR, timeout=10,
            )
