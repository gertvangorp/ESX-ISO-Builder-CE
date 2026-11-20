"""CSV host inventories + host profiles -> effective host settings."""
from __future__ import annotations

import csv
import io
import ipaddress
import os
import re
import tempfile
import threading
from pathlib import Path

import yaml

FIELDS = [
    "Name", "Enabled", "Profile", "Version", "Hostname", "VMnic", "VLANID",
    "IP", "Mask", "Gateway", "DNS", "NTP", "BootDisk", "TierDisk", "TierPct",
    "BmcType", "BmcAddress", "Notes",
]
# columns of the old PowerShell CSVs
LEGACY = {"InstallDisk": "BootDisk", "DNS1": "DNS"}
LIST_SEP = re.compile(r"[;,\s]+")
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", re.I)
FQDN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)
INV_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_lock = threading.RLock()


def split_list(v: str | None) -> list[str]:
    return [x for x in LIST_SEP.split(v or "") if x]


# ---------------------------------------------------------------- CSV store
class Inventories:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def path(self, inv: str) -> Path:
        if not INV_RE.match(inv):
            raise ValueError(f"invalid inventory name '{inv}'")
        return self.folder / f"{inv}.csv"

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.folder.glob("*.csv"))

    @staticmethod
    def parse(text: str) -> list[dict]:
        rows = []
        for raw in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
            row = {LEGACY.get(k.strip(), k.strip()): (v or "").strip() for k, v in raw.items() if k}
            if not any(row.values()):
                continue
            out = {f: row.get(f, "") for f in FIELDS}
            if not out["Enabled"]:
                out["Enabled"] = "yes"
            rows.append(out)
        return rows

    @staticmethod
    def dump(rows: list[dict]) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({f: r.get(f, "") for f in FIELDS})
        return buf.getvalue()

    def load(self, inv: str) -> list[dict]:
        p = self.path(inv)
        if not p.exists():
            raise FileNotFoundError(inv)
        return self.parse(p.read_text(encoding="utf-8"))

    def save(self, inv: str, rows: list[dict]) -> None:
        p = self.path(inv)
        with _lock:
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".tmp-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.dump(rows))
            if p.exists():  # keep one previous version
                p.replace(p.with_suffix(".csv.bak"))
            os.replace(tmp, p)

    def delete(self, inv: str) -> None:
        p = self.path(inv)
        with _lock:
            p.replace(p.with_suffix(".csv.bak"))


# ---------------------------------------------------------------- profiles
class Profiles:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        return {"defaults": data.get("defaults") or {}, "profiles": data.get("profiles") or {}}

    def names(self) -> list[str]:
        return sorted(self.load()["profiles"])


def effective(row: dict, profiles: dict) -> dict:
    """defaults <- profile <- CSV row  ->  settings used by the kickstart template."""
    prof = profiles["profiles"].get(row.get("Profile", ""), {})
    s = {**profiles["defaults"], **prof}
    fqdn = row["Hostname"].lower().rstrip(".")
    domain = fqdn.split(".", 1)[1] if "." in fqdn else ""
    ntp = split_list(row.get("NTP")) or list(s.get("ntp_servers") or [])
    tier_pct = row.get("TierPct") or s.get("tier_pct") or 100
    return {
        "name": row["Name"],
        "profile": row.get("Profile", ""),
        "description": prof.get("description", ""),
        "version": row.get("Version", ""),
        "fqdn": fqdn,
        "domain": domain,
        "vmnic": row.get("VMnic") or s.get("vmnic", "vmnic0"),
        "vlan": int(row["VLANID"]) if row.get("VLANID") else 0,
        "ip": row["IP"],
        "mask": row["Mask"],
        "gateway": row["Gateway"],
        "dns": split_list(row.get("DNS")),
        "dns_search": [d for d in dict.fromkeys([domain, *(s.get("dns_search") or [])]) if d],
        "ntp": ntp,
        "boot_disk": row.get("BootDisk") or s.get("boot_disk", ""),
        "tier_disk": row.get("TierDisk", ""),
        "tier_pct": int(tier_pct),
        "clearpart": s.get("clearpart", "alldrives"),
        "install_flags": list(s.get("install_flags") or []),
        "osdata_size_mb": s.get("osdata_size_mb"),
        "boot_options": list(s.get("boot_options") or []),
        "ssh": bool(s.get("ssh", True)),
        "esx_shell": bool(s.get("esx_shell", False)),
        "suppress_shell_warning": bool(s.get("suppress_shell_warning", True)),
        "regenerate_certificates": bool(s.get("regenerate_certificates", True)),
        "advanced_settings": list(s.get("advanced_settings") or []),
        "kernel_settings": list(s.get("kernel_settings") or []),
        "vmware_config": [str(l).strip() for l in (s.get("vmware_config") or []) if str(l).strip()],
        "firstboot_commands": list(s.get("firstboot_commands") or []),
        "bmc_type": row.get("BmcType") or s.get("bmc_type", "none"),
        "bmc_address": row.get("BmcAddress", ""),
        "i40en_lldp_off": bool(s.get("i40en_lldp_off", False)),
        "use_firstboot": bool(s.get("use_firstboot", True)),
        "installer_network": bool(s.get("installer_network", True)),
    }


# ---------------------------------------------------------------- validation
def validate(row: dict, rows: list[dict], profiles: dict) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Errors block a build."""
    e: list[str] = []
    w: list[str] = []
    name = row.get("Name", "")
    if not NAME_RE.match(name):
        e.append("Name: letters, digits and '-' only")
    if row.get("Profile") not in profiles["profiles"]:
        e.append(f"Profile '{row.get('Profile')}' not in profiles.yaml")
    fqdn = row.get("Hostname", "").lower()
    if not FQDN_RE.match(fqdn):
        e.append("Hostname must be an FQDN (VCF requires forward + reverse DNS for it)")
    elif fqdn.split(".")[0] != name.lower():
        w.append("short hostname differs from Name")
    if row.get("Version", "") == "":
        e.append("Version is empty")
    try:
        vlan = int(row.get("VLANID") or 0)
        if not 0 <= vlan <= 4094:
            raise ValueError
    except ValueError:
        e.append("VLANID must be 0-4094 (0 = untagged)")
    try:
        ip = ipaddress.IPv4Address(row.get("IP", ""))
        net = ipaddress.IPv4Network(f"{ip}/{row.get('Mask', '')}", strict=False)
        gw = ipaddress.IPv4Address(row.get("Gateway", ""))
        if gw not in net:
            e.append(f"Gateway {gw} is not in {net}")
        if ip in (net.network_address, net.broadcast_address) or ip == gw:
            e.append("IP is the network, broadcast or gateway address")
    except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError):
        e.append("IP / Mask / Gateway invalid")
    dns = split_list(row.get("DNS"))
    if not dns:
        e.append("DNS: at least one server")
    if len(dns) > 2:
        w.append("only the first 2 DNS servers are used by the installer network line")
    for d in dns:
        try:
            ipaddress.IPv4Address(d)
        except ValueError:
            e.append(f"DNS '{d}' is not an IPv4 address")
    eff_boot = row.get("BootDisk") or profiles["profiles"].get(row.get("Profile", ""), {}).get("boot_disk", "")
    if not eff_boot:
        e.append("BootDisk empty and the profile has no boot_disk")
    if row.get("TierPct"):
        try:
            if not 1 <= int(row["TierPct"]) <= 400:
                raise ValueError
        except ValueError:
            e.append("TierPct must be 1-400")
    if row.get("TierDisk") and row.get("TierDisk") == eff_boot:
        w.append("TierDisk equals BootDisk – use a serial number so only the tier device matches")
    if row.get("Enabled", "yes").lower() not in ("yes", "no", "true", "false", "1", "0", ""):
        e.append("Enabled: yes/no")
    for other in rows:
        if other is row:
            continue
        if other.get("Name", "").lower() == name.lower():
            e.append("duplicate Name")
        if other.get("IP") == row.get("IP"):
            e.append(f"IP also used by {other.get('Name')}")
        if other.get("Hostname", "").lower() == fqdn:
            e.append(f"Hostname also used by {other.get('Name')}")
    return e, w


def is_enabled(row: dict) -> bool:
    return row.get("Enabled", "yes").lower() in ("yes", "true", "1", "")
