#!/bin/sh
# RaczQQ Updater - instalacja/aktualizacja potrzebnych plików (Py2)
# v1.0
set -e

REPO="QraczQQ/RaczQQUpdater"
BRANCH="main"
RAW="https://raw.githubusercontent.com/${REPO}/${BRANCH}"

BASE="/usr/lib/enigma2/python/Plugins"
DST="$BASE/Extensions/RaczQQUpdater"
OLD="$BASE/Extensions/RaczQQUpdater"

FILES="__init__.py plugin.py plugin.version plugin.png pluginhd.png installer.sh update_satellites_xml.sh"

download() {
    url="$1"; out="$2"
    if command -v wget >/dev/null 2>&1; then
        wget -4 -U "Enigma2" -O "$out" "$url"
        return
    fi
    if command -v curl >/dev/null 2>&1; then
        curl -L -A "Enigma2" --ipv4 -o "$out" "$url"
        return
    fi
    echo "Brak wget/curl - nie można pobrać plików."
    exit 1
}

echo "[RaczQQ Updater] Instalacja/aktualizacja z: $RAW"
mkdir -p "$DST"

if [ ! -f "$DST/__init__.py" ]; then echo '# -*- coding: utf-8 -*-' > "$DST/__init__.py"; fi

for f in $FILES; do
    echo "Pobieram: $f"
    download "$RAW/$f" "$DST/$f"
done

chmod 644 "$DST/"*.py "$DST/"*.txt "$DST/"*.png 2>/dev/null || true
chmod 755 "$DST/"*.sh 2>/dev/null || true
chmod 644 "$DST/__init__.py" 2>/dev/null || true

if [ -d "$OLD" ]; then
    rm -rf "$OLD"
fi

sync || true

echo "[RaczQQ Updater] Restart GUI..."
if command -v init >/dev/null 2>&1; then
    init 4 || true
    sleep 2
    init 3 || true
else
    killall -9 enigma2 2>/dev/null || true
fi

echo "[RaczQQ Updater] OK"
exit 0
