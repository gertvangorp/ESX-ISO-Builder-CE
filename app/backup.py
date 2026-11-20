"""Configuration backup / restore.

A backup is a zip with everything you configured in the app – not the ISOs (rebuildable, ~600 MB each):
  profiles.yaml, inventories/*.csv, output/*.json (build manifests), manifest.json
Restore points are written automatically to /data/backups before every change (BACKUP_KEEP, default 30).
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import zipfile
from pathlib import Path

import yaml

ALLOWED = re.compile(r"^(profiles\.yaml|inventories/[A-Za-z0-9_.-]{1,64}\.csv|output/[A-Za-z0-9_.-]{1,80}\.json|manifest\.json)$")


class Backups:
    def __init__(self, data: Path, keep: int | None = None):
        self.data = data
        self.folder = data / "backups"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.keep = keep or int(os.environ.get("BACKUP_KEEP", "30"))

    # ---------------------------------------------------------------- create
    def build_zip(self, reason: str = "manual") -> bytes:
        buf = io.BytesIO()
        files = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in self._config_files():
                z.write(self.data / rel, rel)
                files.append(rel)
            z.writestr("manifest.json", json.dumps({
                "app": "esx-iso-builder", "format": 1, "reason": reason, "digest": self.digest(),
                "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "files": files}, indent=2))
        return buf.getvalue()

    def _config_files(self) -> list[str]:
        out = []
        if (self.data / "profiles.yaml").exists():
            out.append("profiles.yaml")
        out += [f"inventories/{p.name}" for p in sorted((self.data / "inventories").glob("*.csv"))]
        out += [f"output/{p.name}" for p in sorted((self.data / "output").glob("*.json"))]
        return out

    def digest(self) -> str:
        h = hashlib.sha256()
        for rel in self._config_files():
            h.update(rel.encode()); h.update(b"\0"); h.update((self.data / rel).read_bytes())
        return h.hexdigest()

    def snapshot(self, reason: str) -> str | None:
        """Restore point in /data/backups (skipped when nothing changed since the last one), oldest pruned."""
        latest = self.list()[:1]
        if latest:
            try:
                with zipfile.ZipFile(self.folder / latest[0]["file"]) as z:
                    if json.loads(z.read("manifest.json")).get("digest") == self.digest():
                        return latest[0]["file"]
            except (KeyError, zipfile.BadZipFile, json.JSONDecodeError):
                pass
        tag = re.sub(r"[^a-z0-9-]+", "-", reason.lower()).strip("-")[:40]
        name = f"config-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}{'-' + tag if tag else ''}.zip"
        path = self.folder / name
        n = 1
        while path.exists():   # two changes in the same second
            path = self.folder / name.replace(".zip", f"-{n}.zip"); n += 1
        path.write_bytes(self.build_zip(reason))
        for old in self.list()[self.keep:]:
            (self.folder / old["file"]).unlink(missing_ok=True)
        return path.name

    def list(self) -> list[dict]:
        items = []
        for p in self.folder.glob("config-*.zip"):
            st = p.stat()
            items.append({"file": p.name, "size": st.st_size, "mtime": st.st_mtime,
                          "created": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
        return sorted(items, key=lambda i: i["mtime"], reverse=True)

    def path(self, name: str) -> Path:
        if not re.match(r"^config-[A-Za-z0-9_.-]+\.zip$", name):
            raise ValueError("bad backup name")
        p = self.folder / name
        if not p.exists():
            raise FileNotFoundError(name)
        return p

    # ---------------------------------------------------------------- restore
    def check(self, blob: bytes) -> dict:
        """Validate a backup zip without touching /data. Returns its manifest + file list."""
        try:
            z = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile:
            raise ValueError("not a zip file")
        names = [n for n in z.namelist() if not n.endswith("/")]
        bad = [n for n in names if not ALLOWED.match(n)]
        if bad:
            raise ValueError(f"unexpected files in backup: {', '.join(bad[:5])}")
        if sum(i.file_size for i in z.infolist()) > 50 << 20:
            raise ValueError("backup larger than 50 MB uncompressed – not a config backup")
        if "profiles.yaml" in names:
            data = yaml.safe_load(z.read("profiles.yaml"))
            if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
                raise ValueError("profiles.yaml in the backup is invalid")
        for n in names:
            if n.endswith(".csv"):
                hdr = next(csv.reader(io.StringIO(z.read(n).decode("utf-8-sig"))), [])
                if "Name" not in hdr:
                    raise ValueError(f"{n}: no Name column")
            elif n.endswith(".json"):
                json.loads(z.read(n))
        manifest = json.loads(z.read("manifest.json")) if "manifest.json" in names else {}
        return {"manifest": manifest, "files": [n for n in names if n != "manifest.json"]}

    def restore(self, blob: bytes, mode: str = "replace") -> dict:
        """mode=replace: inventories not in the backup are removed (kept as .csv.bak).
           mode=merge:   only files in the backup are written."""
        info = self.check(blob)
        before = self.snapshot("before-restore")
        z = zipfile.ZipFile(io.BytesIO(blob))
        inv_dir = self.data / "inventories"
        if mode == "replace":
            keep = {Path(n).name for n in info["files"] if n.startswith("inventories/")}
            for p in inv_dir.glob("*.csv"):
                if p.name not in keep:
                    p.replace(p.with_suffix(".csv.bak"))
        for n in info["files"]:
            dest = self.data / n
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".restore-{dest.name}")
            tmp.write_bytes(z.read(n))
            os.replace(tmp, dest)
        return {"restored": info["files"], "mode": mode, "restore_point": before}
