"""Kickstart rendering + ESX ISO building (xorriso, boot records replayed from the original ISO)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES = Path(__file__).parent / "templates"
VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:\.\d+)?(?:[.-]\d{6,})?)")

_env = Environment(loader=FileSystemLoader(TEMPLATES), undefined=StrictUndefined,
                   trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
_env.filters["shquote"] = lambda v: shlex.quote(str(v))


# ---------------------------------------------------------------- versions
def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def boot_arg(disk: str) -> str:
    """'disk:<device id>' -> --disk=, anything else -> --firstdisk=<model/driver/local,...>."""
    if disk.lower().startswith("disk:"):
        return f'--disk="{disk[5:].strip()}"'
    return f'--firstdisk="{disk}"'


def render_ks(h: dict, root_hash: str) -> str:
    v = version_tuple(h["version"])
    return _env.get_template("ks.cfg.j2").render(
        h=h, root_hash=root_hash, boot_arg=boot_arg(h["boot_disk"]),
        memtier_v2=v >= (9, 1),
        generated=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


def hash_password(password: str) -> str:
    out = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


# ---------------------------------------------------------------- base ISOs
class IsoLibrary:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.folder.glob("*.iso")):
            m = VERSION_RE.search(p.name)
            out.append({"file": p.name, "version": m.group(1) if m else p.stem,
                        "size": p.stat().st_size})
        return out

    def resolve(self, version: str) -> Path:
        """CSV 'Version' -> base ISO: exact version, then file name, then unique prefix (e.g. 9.1.1)."""
        isos = self.list()
        for key in ("version", "file"):
            for i in isos:
                if i[key] == version:
                    return self.folder / i["file"]
        hits = [i for i in isos if i["version"].startswith(version)]
        if len(hits) == 1:
            return self.folder / hits[0]["file"]
        raise FileNotFoundError(f"no unique base ISO for version '{version}' in {self.folder}")


# ---------------------------------------------------------------- xorriso
def _x(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["xorriso", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"xorriso {' '.join(args[:4])} … failed:\n{p.stderr[-2000:]}")
    return p


def iso_files(iso: Path) -> list[str]:
    p = _x("-indev", str(iso), "-find", "/", "-type", "f")
    return [l.strip().strip("'") for l in p.stdout.splitlines() if l.strip().startswith("'/")]


def _find(files: list[str], wanted: str) -> str | None:
    return next((f for f in files if f.lower() == wanted.lower()), None)


def patch_kernelopt(text: str, extra: list[str]) -> str:
    def fix(m: re.Match) -> str:
        opts = [o for o in m.group(1).split() if not o.lower().startswith("ks=")]
        keys = {o.split("=")[0].lower() for o in opts}
        if "runweasel" not in keys:
            opts.append("runweasel")
        for e in extra:
            if e.split("=")[0].lower() not in keys:
                opts.append(e)
        return "kernelopt=" + " ".join(opts + ["ks=cdrom:/KS.CFG"])
    new, n = re.subn(r"(?m)^kernelopt=(.*)$", fix, text)
    if n == 0:
        new = text.rstrip("\n") + "\nkernelopt=runweasel ks=cdrom:/KS.CFG\n"
    return new


def build_iso(base: Path, out: Path, ks_text: str, kernel_extra: list[str]) -> dict:
    files = iso_files(base)
    cfgs = [f for f in (_find(files, "/boot.cfg"), _find(files, "/efi/boot/boot.cfg")) if f]
    if not cfgs:
        raise RuntimeError(f"{base.name}: no boot.cfg found – is this an ESX installer ISO?")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="esxi-") as tmp:
        t = Path(tmp)
        maps: list[str] = []
        for i, cfg in enumerate(cfgs):
            local = t / f"boot{i}.cfg"
            _x("-osirrox", "on", "-indev", str(base), "-extract", cfg, str(local))
            local.chmod(0o644)
            local.write_text(patch_kernelopt(local.read_text(errors="replace"), kernel_extra))
            maps += ["-map", str(local), cfg]
        ks = t / "KS.CFG"
        ks.write_text(ks_text.replace("\r\n", "\n"))
        ks_path = _find(files, "/ks.cfg") or "/KS.CFG"
        maps += ["-map", str(ks), ks_path]
        part = out.with_suffix(".iso.part")
        part.unlink(missing_ok=True)
        # Replay the original El Torito (BIOS + UEFI) boot records instead of rebuilding them
        _x("-indev", str(base), "-outdev", str(part), "-boot_image", "any", "replay", *maps,
           "-commit")
        # verify: KS back out of the new image, boot records still there
        chk = t / "check.cfg"
        _x("-osirrox", "on", "-indev", str(part), "-extract", ks_path, str(chk))
        if chk.read_text() != ks.read_text():
            raise RuntimeError("verification failed: KS.CFG in the new ISO differs")
        eltorito = _x("-indev", str(part), "-report_el_torito", "plain").stdout
        platforms = sorted(set(re.findall(r"El Torito boot img :\s+\d+\s+(BIOS|UEFI)", eltorito)))
        part.replace(out)
    sha = hashlib.sha256()
    with out.open("rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            sha.update(chunk)
    return {"iso": out.name, "base": base.name, "sha256": sha.hexdigest(),
            "size": out.stat().st_size, "boot": platforms, "ks_path": ks_path,
            "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}


def write_manifest(folder: Path, name: str, info: dict) -> None:
    (folder / f"{name}.json").write_text(json.dumps(info, indent=2))


def tool_ok() -> bool:
    return shutil.which("xorriso") is not None and shutil.which("openssl") is not None
