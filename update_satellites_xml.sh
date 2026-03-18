#!/bin/sh
# Skrypt do pobierania satellites.xml z raportowaniem postępu

TARGET="/etc/tuxbox/satellites.xml"
VERSION="/etc/tuxbox/satellites.version"
TMP_FILE="/tmp/RaczQQUpdater/satellites.xml"
URL="http://raw.githubusercontent.com/OpenPLi/tuxbox-xml/master/xml/satellites.xml"

progress() {
    echo "PROGRESS:$1"
}

status() {
    echo "STATUS:$1"
}

progress 0
status "Rozpoczynam aktualizację satellites.xml"

progress 10
status "Pobieranie pliku..."

wget -O "$TMP_FILE" "$URL"
if [ $? -ne 0 ]; then
    status "Błąd: nie udało się pobrać satellites.xml"
    rm -f "$TMP_FILE"
    exit 1
fi

clear

progress 70
status "Sprawdzanie pobranego pliku..."

if [ ! -s "$TMP_FILE" ]; then
    status "Błąd: pobrany plik jest pusty"
    rm -f "$TMP_FILE"
    exit 1
fi

progress 85
status "Podmieniam satellites.xml..."

mv "$TMP_FILE" "$TARGET"
if [ $? -ne 0 ]; then
    status "Błąd: nie udało się zapisać pliku docelowego"
    rm -f "$TMP_FILE"
    exit 1
fi

clear
sync

progress 95
status "Pobieram wersję..."

grep File "$TARGET" | cut -d ' ' -f8 | cut -d 'T' -f1 > "$VERSION"

if [ -s "$VERSION" ]; then
    VER="$(cat "$VERSION")"
    status "Wersja: $VER"
else
    status "Nie udało się odczytać wersji"
fi

if [ -f "/etc/enigma2/satellites.xml" ]; then
    status "Usuwam /etc/enigma2/satellites.xml..."
    rm -f "/etc/enigma2/satellites.xml"
    if [ $? -ne 0 ]; then
        status "Błąd: nie udało się usunąć /etc/enigma2/satellites.xml"
        exit 1
    fi
else
    status "Brak /etc/enigma2/satellites.xml - pomijam"
fi

clear

progress 100
status "Pobrano i zapisano satellites.xml pomyślnie"

exit 0
