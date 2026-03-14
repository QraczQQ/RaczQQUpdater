#!/bin/bash

KATALOG="/usr/lib/enigma2/python/Plugins/Extensions/RaczQQUpdater"
SCIEZKA_TEMP="$KATALOG/backup/"
VER_FILE="$KATALOG/plugin.version"

if [ -r "$VER_FILE" ]; then
    VER=$(tr -d '\r\n' < "$VER_FILE")
    [ -z "$VER" ] && VER="unknown"
else
    VER="unknown"
fi

NAZWA_ARCHIWUM="raczqq_updater$(date +%Y%m%d)_ver_$VER.tar.gz"
SCIEZKA_ARCHIWUM="${SCIEZKA_TEMP}${NAZWA_ARCHIWUM}"

echo "Przygotowywanie archiwum"

mkdir -p "$SCIEZKA_TEMP"

tar -czvf "$SCIEZKA_ARCHIWUM" \
    -C "$KATALOG" \
    --exclude="./backup" \
    --exclude="*/__pycache__" \
    --exclude="*.pyc" \
	--exclude="*.pyo" \
    .
sleep 2
