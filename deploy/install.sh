#!/usr/bin/env bash
# Installiert das Sungrow-Dashboard als systemd-Dienst unter Ubuntu.
# Aufruf im kopierten Projektordner:   sudo bash deploy/install.sh
# Erneuter Aufruf aktualisiert die Installation (settings.json bleibt erhalten).
set -euo pipefail

DEST=/opt/sungrow-dashboard
SVC=sungrow-dashboard
SRC="$(cd "$(dirname "$0")/.." && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen."; exit 1; }

apt-get update -qq
apt-get install -y -qq python3 python3-venv

id -u sungrow >/dev/null 2>&1 || useradd --system --home "$DEST" --shell /usr/sbin/nologin sungrow

mkdir -p "$DEST/static"
install -m 644 "$SRC/server.py" "$SRC/registers.py" "$SRC/mqtt_ha.py" "$SRC/requirements.txt" "$DEST/"
install -m 644 "$SRC/static/index.html" "$DEST/static/"
# Einstellungen nur bei der Erstinstallation übernehmen
if [ -f "$SRC/settings.json" ] && [ ! -f "$DEST/settings.json" ]; then
  install -m 600 "$SRC/settings.json" "$DEST/"
fi

[ -x "$DEST/.venv/bin/python" ] || python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install --quiet --upgrade -r "$DEST/requirements.txt"

chown -R sungrow:sungrow "$DEST"

install -m 644 "$SRC/deploy/$SVC.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SVC" >/dev/null
systemctl restart "$SVC"

sleep 2
systemctl --no-pager --lines=5 status "$SVC" || true
echo
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):8080"
