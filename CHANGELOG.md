# Changelog

The version is `APP_VERSION` in `app/main.py`. It shows in the UI header, `/healthz` and `/api/docs`.

## 1.0.0

First public release (Community Edition).

- Web UI and REST API: inventories (CSV), host editor with live validation and DNS check (A/PTR/CNAME), kickstart preview, builds, ISO library, profiles editor, backup/restore with automatic restore points.
- ISO build with `xorriso` replaying the original BIOS + UEFI boot records; per-host `<host>-<version>.iso`, one ISO per host.
- Kickstart: wipe all or boot disk, static management network + VLAN, installer boot options with the same static network (no DHCP needed), `--firstdisk` by full model or `disk:<id>`, hashed root password.
- First boot (non-Secure-Boot hosts): FQDN, DNS search, NTP with verification and fallback, SSH per profile, advanced/kernel settings, `/etc/vmware/config` lines, extra commands, NVMe memory tiering (9.1+ `memtier`, older `tierdevice`), new certificate with CN = FQDN; output to file and syslog. `use_firstboot: false` for Secure Boot hosts.
- Example profiles: Dell PowerEdge R640, Minisforum MS-A2 (entropy, RDSEED mask, Intel X710 LLDP/LRO/TSO).
- ISO download over HTTPS (media or UI account) and an optional media-only plain-HTTP listener with an allow-list for BMC virtual media.
- Ansible inventory export including the post-install settings.
