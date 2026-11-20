"""ESX ISO builder – web UI + REST API.

Data layout (volume /data):
  profiles.yaml            host profiles (defaults <- profile <- CSV row)
  inventories/*.csv        one CSV per host group (dell-r640, ms-a2-mgmt, ...)
  iso/*.iso                original ESX installer ISOs (VMware-VMvisor-Installer-<version>.x86_64.iso)
  output/<host>-<ver>.iso  built ISOs (ESX version in the name), served to iDRAC / Comet under /media/
  output/<host>.ks.cfg     the kickstart inside it, output/<host>.json the build manifest
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
import re
import secrets
import shutil
import socket
import threading
import uuid
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from backup import Backups
from build import IsoLibrary, build_iso, hash_password, render_ks, write_manifest
from inventory import FIELDS, Inventories, Profiles, effective, is_enabled, validate

DATA = Path(os.environ.get("DATA_DIR", "/data"))
SEED = Path(__file__).parent.parent / "seed"
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
# Optional plain-HTTP base URL for the ISO download only (iDRAC virtual media is much faster without TLS),
# e.g. http://esx-builder.example.com:8080 - served by a second, media-only listener (see entrypoint.sh)
MEDIA_URL = os.environ.get("MEDIA_URL", "").rstrip("/")
MEDIA_ONLY = os.environ.get("MEDIA_ONLY") == "1"


def _seed() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if not (DATA / "profiles.yaml").exists() and (SEED / "profiles.yaml").exists():
        shutil.copy(SEED / "profiles.yaml", DATA / "profiles.yaml")
    inv = DATA / "inventories"
    if not inv.exists():
        inv.mkdir()
        for f in (SEED / "inventories").glob("*.csv"):
            shutil.copy(f, inv / f.name)


_seed()
INV = Inventories(DATA / "inventories")
PROF = Profiles(DATA / "profiles.yaml")
LIB = IsoLibrary(DATA / "iso")
OUT = DATA / "output"
OUT.mkdir(exist_ok=True)
BAK = Backups(DATA)

APP_VERSION = "1.0.0"   # bump on every change; shown in the UI header, /healthz and /api/docs

app = FastAPI(title="ESX ISO builder", version=APP_VERSION, docs_url="/api/docs", redoc_url=None)
basic = HTTPBasic(realm="esxi-iso-builder")


if MEDIA_ONLY:
    # media-only listener (plain HTTP): nothing but the ISO download and the health check
    @app.middleware("http")
    async def _media_only(request: Request, call_next):
        if request.url.path.startswith("/media/") or request.url.path == "/healthz":
            return await call_next(request)
        return PlainTextResponse("not found", status_code=404)


def _check(creds: HTTPBasicCredentials, user_env: str, pass_env: str) -> None:
    user, pw = os.environ.get(user_env, ""), os.environ.get(pass_env, "")
    if not pw:
        raise HTTPException(503, f"{pass_env} is not set")
    ok = secrets.compare_digest(creds.username.encode(), user.encode()) & \
        secrets.compare_digest(creds.password.encode(), pw.encode())
    if not ok:
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": 'Basic realm="esxi-iso-builder"'})


def admin(creds: HTTPBasicCredentials = Depends(basic)) -> None:
    _check(creds, "UI_USER", "UI_PASSWORD")


def csrf(request: Request) -> None:   # state-changing calls must come from our own page / a script
    if request.method != "GET" and request.headers.get("x-builder") != "1":
        raise HTTPException(403, "missing X-Builder header")


ADMIN = [Depends(admin), Depends(csrf)]


# ---------------------------------------------------------------- helpers
def _root_hash() -> str:
    if os.environ.get("ESXI_ROOT_HASH"):
        return os.environ["ESXI_ROOT_HASH"]
    pw = os.environ.get("ESXI_ROOT_PASSWORD", "")
    if not pw:
        raise HTTPException(503, "set ESXI_ROOT_PASSWORD or ESXI_ROOT_HASH")
    return hash_password(pw)


def _rows(inv: str) -> list[dict]:
    try:
        return INV.load(inv)
    except FileNotFoundError:
        raise HTTPException(404, f"inventory '{inv}' not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


def _row(rows: list[dict], name: str) -> dict:
    for r in rows:
        if r["Name"].lower() == name.lower():
            return r
    raise HTTPException(404, f"host '{name}' not found")


def _iso_name(row: dict) -> str:
    """<host>-<ESX version>.iso - the version in the file name shows what is inside (and on the iDRAC/Comet)."""
    ver = re.sub(r"[^A-Za-z0-9._-]", "_", str(row.get("Version") or "")).strip("_")
    return f"{row['Name']}-{ver}.iso" if ver else f"{row['Name']}.iso"


def _media_url(iso: str) -> str:
    base = MEDIA_URL or PUBLIC_URL
    return f"{base}/media/{iso}" if base else f"/media/{iso}"


def _kernel_extra(h: dict) -> list[str]:
    extra = list(h["boot_options"])
    # Static network for the installer itself (documented ESX boot options). Without them the installer
    # tries DHCP first and stops on "Could not bring up network (Did not get an IP Address from DHCP
    # server)" until Enter or its timeout, on a VLAN without DHCP.
    if h.get("installer_network", True):
        extra += [f"netdevice={h['vmnic']}", f"ip={h['ip']}", f"netmask={h['mask']}", f"gateway={h['gateway']}"]
        if h["dns"]:
            extra.append(f"nameserver={h['dns'][0]}")
        if h["vlan"]:
            extra.append(f"vlanid={h['vlan']}")
    if h.get("osdata_size_mb"):
        extra.append(f"autoPartitionOSDataSize={int(h['osdata_size_mb'])}")
    return extra


def _status(row: dict) -> dict:
    # the manifest <host>.json names the ISO of the last build (its name carries the version)
    man = OUT / f"{row['Name']}.json"
    info = {}
    if man.exists():
        info = yaml.safe_load(man.read_text()) or {}
        iso = OUT / str(info.get("iso") or _iso_name(row))
        if not iso.exists():
            return {}
        info["url"] = _media_url(iso.name)
    return info


# ---------------------------------------------------------------- jobs
JOBS: dict[str, dict] = {}
_build_lock = threading.Lock()


def _run_build(job: dict, inv: str, names: list[str], version: str | None) -> None:
    try:
        root_hash = _root_hash()
        with _build_lock:   # one xorriso at a time – each ISO is ~600 MB of I/O
            rows = INV.load(inv)
            prof = PROF.load()
            for name in names:
                row = dict(_row(rows, name))
                if version:
                    row["Version"] = version
                job["current"] = name
                errs, _ = validate(row, [r for r in rows if r["Name"] != name] + [row], prof)
                if errs:
                    job["results"][name] = {"error": "; ".join(errs)}
                    continue
                try:
                    h = effective(row, prof)
                    ks = render_ks(h, root_hash)
                    base = LIB.resolve(row["Version"])
                    old = _status(row).get("iso")
                    info = build_iso(base, OUT / _iso_name(row), ks, _kernel_extra(h))
                    if old and old != info["iso"]:   # a host keeps one ISO: drop the one of the previous version
                        (OUT / old).unlink(missing_ok=True)
                    info.update(host=name, fqdn=h["fqdn"], version=row["Version"], inventory=inv,
                                bmc_type=h["bmc_type"], bmc_address=h["bmc_address"])
                    (OUT / f"{name}.ks.cfg").write_text(ks)
                    write_manifest(OUT, name, info)
                    info["url"] = _media_url(info["iso"])
                    job["results"][name] = info
                except Exception as e:  # noqa: BLE001 – report per host, keep going
                    job["results"][name] = {"error": str(e)}
    except HTTPException as e:
        job["error"] = e.detail
    except Exception as e:  # noqa: BLE001
        job["error"] = str(e)
    finally:
        job["current"] = None
        job["state"] = "done"


# ---------------------------------------------------------------- API
@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True, "version": APP_VERSION}


STATIC = Path(__file__).parent / "static"


@app.get("/static/{name}", include_in_schema=False)
def static(name: str):
    """Logo and favicons only - public, so the browser tab icon also shows on the login prompt."""
    p = STATIC / name
    if p.parent != STATIC or p.suffix != ".png" or not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/dns", dependencies=ADMIN)
def dns_check(name: str, ip: str | None = None):
    """Resolve a name as the builder's resolver sees it: A records, CNAME (alias) and PTR per address.
    `ip` (optional) = the address the name must resolve to, e.g. the host's management IP."""
    name = name.strip().rstrip(".")
    res = {"name": name, "canonical": None, "alias": False, "ips": [], "ptr": {}, "ip_match": None, "error": None}
    if not name:
        res["error"] = "empty"
        return res
    try:
        canonical, _aliases, ips = socket.gethostbyname_ex(name)
    except OSError as e:
        res["error"] = f"does not resolve ({e.strerror or e})"
        return res
    res["canonical"] = canonical
    res["alias"] = canonical.rstrip(".").lower() != name.lower()
    res["ips"] = sorted(set(ips), key=lambda a: tuple(int(x) for x in a.split(".")))
    for a in res["ips"]:
        try:
            res["ptr"][a] = socket.gethostbyaddr(a)[0]
        except OSError:
            res["ptr"][a] = None
    if ip:
        res["ip_match"] = ip.strip() in res["ips"]
    return res


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "logo-32.png", media_type="image/png")


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(admin)])
def index():
    return (Path(__file__).parent / "templates" / "index.html").read_text()


@app.get("/api/meta", dependencies=ADMIN)
def meta():
    p = PROF.load()
    return {
        "inventories": INV.names(),
        "profiles": {k: v.get("description", "") for k, v in p["profiles"].items()},
        "profile_boot": {k: v.get("boot_disk", "") or "" for k, v in p["profiles"].items()},
        "isos": LIB.list(),
        "public_url": PUBLIC_URL,
        "version": APP_VERSION,
        "root_password_set": bool(os.environ.get("ESXI_ROOT_PASSWORD") or os.environ.get("ESXI_ROOT_HASH")),
    }


@app.get("/api/inventories/{inv}", dependencies=ADMIN)
def get_inventory(inv: str):
    rows = _rows(inv)
    prof = PROF.load()
    out = []
    for r in rows:
        errs, warns = validate(r, rows, prof)
        out.append({"row": r, "errors": errs, "warnings": warns, "build": _status(r)})
    return {"name": inv, "hosts": out}


@app.post("/api/inventories/{inv}", dependencies=ADMIN)
async def create_inventory(inv: str, request: Request):
    """Create an inventory. Body: raw CSV (optional) – old PowerShell CSVs are converted."""
    if inv in INV.names():
        raise HTTPException(409, "inventory exists")
    BAK.snapshot(f"before-create-{inv}")
    text = (await request.body()).decode("utf-8-sig") if request.headers.get("content-length", "0") != "0" else ""
    try:
        INV.save(inv, INV.parse(text) if text.strip() else [])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"created": inv}


@app.put("/api/inventories/{inv}/csv", dependencies=ADMIN)
async def replace_csv(inv: str, request: Request):
    rows = INV.parse((await request.body()).decode("utf-8-sig"))
    BAK.snapshot(f"before-import-{inv}")
    INV.save(inv, rows)
    return {"hosts": len(rows)}


@app.get("/api/inventories/{inv}/csv", dependencies=ADMIN)
def download_csv(inv: str):
    return PlainTextResponse(INV.dump(_rows(inv)), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{inv}.csv"'})


@app.delete("/api/inventories/{inv}", dependencies=ADMIN)
def delete_inventory(inv: str):
    _rows(inv)
    BAK.snapshot(f"before-delete-{inv}")
    INV.delete(inv)
    return {"deleted": inv, "backup": f"{inv}.csv.bak"}


@app.put("/api/inventories/{inv}/hosts/{name}", dependencies=ADMIN)
async def upsert_host(inv: str, name: str, request: Request):
    """Add or update a host. Saves even with validation errors (they block builds, not edits)."""
    data = await request.json()
    rows = _rows(inv)
    new = {k: str(data.get(k, "")).strip() for k in FIELDS}
    new["Name"] = new["Name"] or name
    existing = next((i for i, r in enumerate(rows) if r["Name"].lower() == name.lower()), None)
    if existing is None:
        rows.append(new)
    else:
        rows[existing] = new
    errs, warns = validate(new, rows, PROF.load())
    BAK.snapshot(f"before-edit-{new['Name']}")
    INV.save(inv, rows)
    return {"row": new, "errors": errs, "warnings": warns}


@app.delete("/api/inventories/{inv}/hosts/{name}", dependencies=ADMIN)
def delete_host(inv: str, name: str):
    rows = _rows(inv)
    row = _row(rows, name)
    rows.remove(row)
    BAK.snapshot(f"before-remove-{name}")
    INV.save(inv, rows)
    return {"deleted": name}


@app.get("/api/inventories/{inv}/hosts/{name}/ks", dependencies=ADMIN, response_class=PlainTextResponse)
def preview_ks(inv: str, name: str, version: str | None = None):
    rows = _rows(inv)
    row = dict(_row(rows, name))
    if version:
        row["Version"] = version
    prof = PROF.load()
    errs, _ = validate(row, [r for r in rows if r["Name"] != name] + [row], prof)
    if errs:
        raise HTTPException(422, "; ".join(errs))
    h = effective(row, prof)
    ks = render_ks(h, "<sha512-hash-of-ESXI_ROOT_PASSWORD>")
    extra = " ".join(_kernel_extra(h))
    return f"# boot.cfg kernelopt += {extra + ' ' if extra else ''}ks=cdrom:/KS.CFG\n" + ks


@app.post("/api/inventories/{inv}/build", dependencies=ADMIN)
async def build(inv: str, request: Request):
    """Body: {"hosts": [...] (default: all enabled), "version": "<override>"}. Returns a job id."""
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    rows = _rows(inv)
    names = body.get("hosts") or [r["Name"] for r in rows if is_enabled(r)]
    for n in names:
        _row(rows, n)
    job = {"id": uuid.uuid4().hex[:12], "inventory": inv, "hosts": names, "state": "running",
           "current": None, "results": {}, "error": None}
    JOBS[job["id"]] = job
    threading.Thread(target=_run_build, args=(job, inv, names, body.get("version") or None), daemon=True).start()
    return job


@app.get("/api/jobs/{job_id}", dependencies=ADMIN)
def job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "no such job")
    return JOBS[job_id]


@app.get("/api/inventories/{inv}/ansible", dependencies=ADMIN, response_class=PlainTextResponse)
def ansible_inventory(inv: str):
    """Ansible inventory for your own automation: IP, FQDN, BMC, ISO URL + SHA-256 and the post-install settings."""
    rows = _rows(inv)
    prof = PROF.load()
    hosts = {}
    for r in rows:
        if not is_enabled(r) or validate(r, rows, prof)[0]:
            continue
        h = effective(r, prof)
        st = _status(r)
        hosts[r["Name"]] = {
            "ansible_host": h["ip"], "esxi_fqdn": h["fqdn"], "esxi_profile": h["profile"],
            "esxi_version": r["Version"], "esxi_vmnic": h["vmnic"], "esxi_vlan": h["vlan"],
            "esxi_ntp_servers": h["ntp"], "esxi_tier_disk": h["tier_disk"] or None,
            "bmc_type": h["bmc_type"], "bmc_address": h["bmc_address"] or None,
            "esxi_iso_url": st.get("url"), "esxi_iso_sha256": st.get("sha256"),
            # post-install settings for your automation (vSphere API) - %firstboot does not run on Secure Boot hosts
            "esxi_dns_servers": h["dns"][:2], "esxi_dns_search": h["dns_search"],
            "esxi_ssh": h["ssh"], "esxi_suppress_shell_warning": h["suppress_shell_warning"],
            "esxi_advanced_settings": h["advanced_settings"], "esxi_kernel_settings": h["kernel_settings"],
            "esxi_vmware_config": h["vmware_config"], "esxi_tier_pct": h["tier_pct"],
            "esxi_i40en_lldp_off": h.get("i40en_lldp_off", False),
        }
    group = "esxi_" + inv.replace("-", "_").replace(".", "_")
    return yaml.safe_dump({"all": {"children": {group: {"hosts": hosts}}}}, sort_keys=False)


@app.get("/api/isos", dependencies=ADMIN)
def list_isos():
    built = []
    for m in sorted(OUT.glob("*.json")):
        info = yaml.safe_load(m.read_text()) or {}
        if (OUT / info.get("iso", "-")).exists():
            info["url"] = _media_url(info["iso"])
            built.append(info)
    return {"base": LIB.list(), "built": built}


@app.put("/api/isos/{filename}", dependencies=ADMIN)
async def upload_iso(filename: str, request: Request):
    """Streams a base ESX ISO into /data/iso (raw body, no multipart)."""
    if not filename.lower().endswith(".iso") or "/" in filename or filename.startswith("."):
        raise HTTPException(400, "name must be <file>.iso")
    dest = LIB.folder / filename
    part = dest.with_suffix(".iso.part")
    with part.open("wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    with part.open("rb") as f:   # ISO 9660 primary volume descriptor at 32 KiB
        f.seek(0x8001)
        if f.read(5) != b"CD001":
            part.unlink()
            raise HTTPException(400, "not an ISO 9660 image")
    part.replace(dest)
    return {"stored": filename, "size": dest.stat().st_size}


@app.delete("/api/isos/{kind}/{filename}", dependencies=ADMIN)
def delete_iso(kind: str, filename: str):
    folder = {"base": LIB.folder, "built": OUT}.get(kind)
    if not folder or "/" in filename:
        raise HTTPException(400, "bad request")
    p = folder / filename
    if not p.exists():
        raise HTTPException(404, "not found")
    p.unlink()
    if kind == "built":
        for m in OUT.glob("*.json"):   # manifest + kickstart of the host whose last build this ISO is
            if (yaml.safe_load(m.read_text()) or {}).get("iso") == filename:
                m.unlink(missing_ok=True)
                (OUT / f"{m.stem}.ks.cfg").unlink(missing_ok=True)
    return {"deleted": filename}


@app.get("/api/profiles", dependencies=ADMIN, response_class=PlainTextResponse)
def get_profiles():
    return PROF.path.read_text()


@app.put("/api/profiles", dependencies=ADMIN)
async def put_profiles(request: Request):
    text = (await request.body()).decode()
    try:
        data = yaml.safe_load(text)
        assert isinstance(data, dict) and isinstance(data.get("profiles"), dict)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"invalid profiles.yaml: {e}")
    BAK.snapshot("before-profiles")
    shutil.copy(PROF.path, PROF.path.with_suffix(".yaml.bak"))
    PROF.path.write_text(text)
    return {"saved": True}


# ---------------------------------------------------------------- configuration backup / restore
@app.get("/api/backup", dependencies=ADMIN)
def download_backup():
    """Fresh backup of all configuration (profiles, inventories, build manifests) – no ISOs."""
    name = f"esxi-builder-config-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return Response(BAK.build_zip("download"), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/backups", dependencies=ADMIN)
def list_backups():
    return {"backups": BAK.list(), "keep": BAK.keep}


@app.post("/api/backups", dependencies=ADMIN)
def create_backup():
    return {"created": BAK.snapshot("manual")}


@app.get("/api/backups/{name}", dependencies=ADMIN)
def get_backup(name: str):
    try:
        return FileResponse(BAK.path(name), media_type="application/zip", filename=name)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "not found")


@app.delete("/api/backups/{name}", dependencies=ADMIN)
def delete_backup(name: str):
    try:
        BAK.path(name).unlink()
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "not found")
    return {"deleted": name}


@app.post("/api/restore", dependencies=ADMIN)
async def restore(request: Request, mode: str = "replace", dry_run: bool = False, name: str | None = None):
    """Body: backup zip (raw), or ?name=<restore point>. mode=replace|merge, dry_run=true only validates."""
    if mode not in ("replace", "merge"):
        raise HTTPException(400, "mode must be replace or merge")
    try:
        blob = BAK.path(name).read_bytes() if name else await request.body()
        if dry_run:
            return BAK.check(blob)
        with _build_lock:   # never restore under a running build
            return BAK.restore(blob, mode)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- media for iDRAC / Comet
media_basic = HTTPBasic(realm="esxi-media", auto_error=False)


# iDRAC9 does not answer a Basic challenge on plain HTTP shares (it sends one HEAD without credentials and
# gives up). On the media-only HTTP listener, clients from MEDIA_HTTP_ALLOW (CIDRs, e.g. the iDRAC subnet) may
# download without a login; everyone else still needs the media account.
MEDIA_HTTP_ALLOW = [ipaddress.ip_network(n, strict=False)
                    for n in os.environ.get("MEDIA_HTTP_ALLOW", "").replace(",", " ").split() if n]


def _media_ip_allowed(request: Request) -> bool:
    if not (MEDIA_ONLY and MEDIA_HTTP_ALLOW and request.client):
        return False
    try:
        ip = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    return any(ip in n for n in MEDIA_HTTP_ALLOW)


def media_auth(request: Request, creds: HTTPBasicCredentials | None = Depends(media_basic)) -> None:
    if os.environ.get("MEDIA_AUTH", "basic") == "none" or _media_ip_allowed(request):
        return
    if creds is None:
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": 'Basic realm="esxi-media"'})
    # The UI login also may download: a browser logged in to the UI sends the admin credentials with the
    # download link and does not ask again, so media-only would make the ISO link fail in the UI.
    try:
        _check(creds, "UI_USER", "UI_PASSWORD")
        return
    except HTTPException:
        pass
    _check(creds, "MEDIA_USER", "MEDIA_PASSWORD")


@app.api_route("/media/{filename}", methods=["GET", "HEAD"], dependencies=[Depends(media_auth)])
def media(filename: str):
    p = OUT / filename
    if "/" in filename or not filename.endswith(".iso") or not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type="application/octet-stream")   # supports Range requests
