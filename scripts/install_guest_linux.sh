#!/bin/bash
set -euo pipefail
umask 077

if [[ "$(uname -s)" != Linux || "$(uname -m)" != aarch64 ]]; then
    echo 'Install only inside the Ubuntu 24.04 ARM64 guest.' >&2
    exit 2
fi
if [[ $EUID != 0 || ( $# != 1 && $# != 3 ) ]]; then
    echo 'Usage: sudo bash scripts/install_guest_linux.sh DESKTOP_USER [DISPLAY XAUTHORITY]' >&2
    exit 2
fi
source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 ]]; then
    echo 'Ubuntu 24.04 LTS is required.' >&2
    exit 2
fi
guest_uid="$(id -u -- "$1")"
if [[ "$guest_uid" -lt 1000 ]]; then
    echo 'Choose an ordinary desktop user, not root or a system account.' >&2
    exit 2
fi
if systemctl is-active --quiet adaptive-os-guest.service; then
    echo 'Stop adaptive-os-guest.service before updating its code; applications remain open.' >&2
    exit 2
fi
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
for executable in /usr/bin/python3 /usr/bin/firefox /usr/bin/geany /usr/bin/dbus-run-session /usr/bin/xdpyinfo; do
    if [[ ! -x "$executable" ]]; then
        echo "Missing $executable; complete docs/guest-linux.md prerequisites first." >&2
        exit 2
    fi
done
install -d -o root -g root -m 0755 /opt/adaptive-os-guest/adaptive_os/guest_linux /etc/adaptive-os
install -o root -g root -m 0644 "$project_root/src/adaptive_os/__init__.py" /opt/adaptive-os-guest/adaptive_os/__init__.py
for module in __init__ protocol core linux resources network service; do
    install -o root -g root -m 0644 "$project_root/src/adaptive_os/guest_linux/$module.py" "/opt/adaptive-os-guest/adaptive_os/guest_linux/$module.py"
done
install -o root -g root -m 0644 "$project_root/scripts/guest_service_entry.py" /opt/adaptive-os-guest/service.py
/usr/bin/python3 -I - "$guest_uid" "${2:-}" "${3:-}" <<'PY'
import pwd
import sys
from pathlib import Path
sys.path.insert(0, '/opt/adaptive-os-guest')
from adaptive_os.guest_linux.linux import configure_display, load_config
uid, display, authority = int(sys.argv[1]), sys.argv[2], sys.argv[3]
path = Path('/etc/adaptive-os/guest.json')
if path.exists() and load_config(path)['uid'] != uid:
    raise SystemExit('Existing guest configuration belongs to another user; refusing replacement.')
if display or not path.exists():
    configure_display(uid, display or ':0', authority or pwd.getpwuid(uid).pw_dir + '/.Xauthority', path)
PY
install -o root -g root -m 0644 "$project_root/guest/systemd/adaptiveos.slice" /etc/systemd/system/adaptiveos.slice
install -o root -g root -m 0644 "$project_root/guest/systemd/adaptive-os-guest.service" /etc/systemd/system/adaptive-os-guest.service
systemctl daemon-reload
systemctl enable --now adaptive-os-guest.service
echo 'Guest service installed. Run ./start.sh --backend guest-linux as the desktop user.'
