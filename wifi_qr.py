"""Wifi provisioning from a QR code: parse the standard WIFI: payload phones
show under "share wifi", and join that network through NetworkManager.

Self-check: python3 wifi_qr.py
"""
import fcntl
import logging
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

WIFI_DEV = "wlan0"
# upstream's hotspot fallback (agb-hotspot.sh, fired by a timer and by an NM
# dispatcher hook) takes this lock and then runs `nmcli connection up` on the
# saved networks, which aborts whatever activation is in flight. Holding the
# lock keeps it out while we join.
_HOTSPOT_LOCK = "/run/agb-hotspot.lock"
_HOTSPOT_CON = "AGB-Hotspot"
_HOTSPOT_SCRIPT = Path("/usr/local/sbin/agb-hotspot.sh")


def parse(text: str) -> dict | None:
    """WIFI:S:<ssid>;T:<auth>;P:<password>;H:<hidden>;; → {"S": ..., "P": ...}.
    Fields come in any order and \\ ; , : " are backslash-escaped. Returns
    None when the text is not a wifi code."""
    if not text.upper().startswith("WIFI:"):
        return None
    fields, key, buf, esc = {}, None, "", False
    for ch in text[5:] + ";":  # the extra ; closes a last field left open
        if esc:
            buf, esc = buf + ch, False
        elif ch == "\\":
            esc = True
        elif ch == ":" and key is None:
            key, buf = buf.upper(), ""
        elif ch == ";":
            if key is not None:
                # the format quotes values that could be mistaken for hex
                if len(buf) >= 2 and buf[0] == buf[-1] == '"':
                    buf = buf[1:-1]
                fields[key] = buf
            key, buf = None, ""
        else:
            buf += ch
    return fields if fields.get("S") else None


def _nmcli(*args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)


def _saved_uuids() -> set:
    return set(_nmcli("-g", "UUID", "connection", "show").stdout.split())


def join(ssid: str, password: str = "", hidden: bool = False) -> bool:
    """Save the network and connect to it. On failure nothing stays saved:
    upstream retries every saved network at boot, ~25 s per unreachable one."""
    with open(_HOTSPOT_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = _saved_uuids()
        # one radio: it cannot scan for the new network while it is the hotspot
        _nmcli("connection", "down", _HOTSPOT_CON)
        _nmcli("device", "wifi", "rescan", "ifname", WIFI_DEV)
        time.sleep(3)
        cmd = ["--wait", "45", "device", "wifi", "connect", ssid, "ifname", WIFI_DEV]
        if password:
            cmd += ["password", password]
        if hidden:
            cmd += ["hidden", "yes"]
        r = _nmcli(*cmd, timeout=90)
        if r.returncode == 0:
            log.info("Joined wifi %r", ssid)
            return True
        # log nmcli's message only, never the command: it carries the password
        log.error("Could not join wifi %r: %s", ssid, r.stderr.strip())
        for uuid in _saved_uuids() - before:
            _nmcli("connection", "delete", "uuid", uuid)
        return False


def restore():
    """Hand the radio back to upstream's fallback (saved networks first, the
    hotspot otherwise) after a failed join left it with neither."""
    if _HOTSPOT_SCRIPT.exists():
        subprocess.Popen([str(_HOTSPOT_SCRIPT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    assert parse("WIFI:T:WPA;S:Mochitos;P:secreto123;;") == {"T": "WPA", "S": "Mochitos", "P": "secreto123"}
    assert parse(r"WIFI:S:caf\;e\:bar;P:a\\b\,c\"d;H:true;;") == {"S": "caf;e:bar", "P": 'a\\b,c"d', "H": "true"}
    assert parse('WIFI:S:"1234";P:"abcd";;') == {"S": "1234", "P": "abcd"}
    assert parse("WIFI:S:x;P:a:b") == {"S": "x", "P": "a:b"}  # bare colon, no terminator
    assert parse("wifi:T:nopass;S:Open;;") == {"T": "nopass", "S": "Open"}
    assert parse("https://example.com") is None
    assert parse("WIFI:T:WPA;P:nossid;;") is None
    print("wifi_qr self-check OK")
