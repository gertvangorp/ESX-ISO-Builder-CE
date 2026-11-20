# Troubleshooting

Quick checks on a freshly installed host (console or SSH):

```bash
grep -i secureboot /var/log/kickstart.log                                   # firstboot skipped?
esxcli system settings advanced list -o /UserVars/SuppressShellWarning     # 1 = firstboot ran
grep esx-firstboot /var/log/syslog.log | tail -50                           # firstboot output
esxcli system ntp get
esxcli system settings kernel list -o entropySources                       # MS-A2: 2
esxcli system module parameters list -m i40en | grep -i lldp                # MS-A2: 0,0
esxcli memtier status get                                                   # tiering
esxcli storage core device list | grep -E '^(t10|eui|naa)|Model:'           # disk ids / models
```

## Kickstart and install

| Symptom | Cause | Fix |
|---|---|---|
| `install --overwritevsan specified but disk … is not claimed by vSAN` | `--overwritevsan` refuses a boot disk that is not a vSAN disk (e.g. Dell BOSS) | Leave it out of `install_flags`; `clearpart --alldrives` already wipes the vSAN disks |
| `install --firstdisk specified, but no suitable disk was found` | `--firstdisk` matches the **full** model string (or vendor/driver), not a part of it | Use the full model (`FIKWOT FN955 1TB`, not `FIKWOT`) or `disk:<device id>`. Read models in the installer: Alt+F1, `root`, empty password, `esxcli storage core device list` |
| Several identical disks – ESX lands on the "wrong" one | `--firstdisk=<model>` takes the first match | After the first install note the boot disk id and set `BootDisk` to `disk:<device id>` per host |
| Installer waits on `Could not bring up network (Did not get an IP Address from DHCP server)` | Before reading KS.CFG the installer brings up its own network with DHCP | `installer_network: true` (default) adds the static IP/VLAN as boot options |
| `no unique base ISO for version` | The base ISO file name must contain the `Version` text, and only one ISO may match | Name custom ISOs with the full build, e.g. `ESX-9.1.1.0.25714478-custom.iso` |
| VCF Installer: certificate / hostname mismatch | DNS A/PTR wrong at install time | Fix DNS, then `/sbin/generate-certificates` and reboot |

## First boot (host settings)

| Symptom | Cause | Fix |
|---|---|---|
| No NTP, `SuppressShellWarning` 0; `kickstart.log`: `SecureBoot is enabled, skipping execution of scripts in /var/lib/vmware/firstboot` | **ESX never runs `%firstboot` on UEFI Secure Boot hosts** | Apply the settings after the install (vSphere API, PowerCLI, Ansible – the inventory export lists them) and set `use_firstboot: false`; or turn Secure Boot off |
| Same hardware, different result per host | Secure Boot differs per host in the BIOS | Make the BIOS settings identical |
| `/var/log/esx-firstboot.log` missing although the settings were applied | The file did not survive the final reboot | `grep esx-firstboot /var/log/syslog.log` |
| `/var/lib/vmware/firstboot/` empty | Normal after a successful firstboot | – |
| Memory tiering not configured | `TierDisk` empty at build time, or it matches 0 or several partition-free disks | Set `TierDisk` to the **serial** of the free disk of **that** host. By hand: `esxcli system maintenanceMode set --enable true; esxcli memtier enable -d /vmfs/devices/disks/<id> -r 400; esxcli system maintenanceMode set --enable false` |

## BMC virtual media

| Symptom | Cause | Fix |
|---|---|---|
| iDRAC Redfish `400 QueryNotSupported` by FQDN, works by IP | iDRAC Host Header Check | iDRAC Settings → Network → Common: DNS iDRAC Name + static DNS domain (or disable the check) |
| `InsertMedia` 500 `Unable to locate the ISO or IMG image file …` | Wrong media credentials, or the iDRAC cannot reach/resolve the builder | Check the media account, the iDRAC DNS servers and the route to the builder |
| Same 500 on the plain-HTTP URL, builder log shows one `HEAD … 401` | iDRAC9 does not answer a Basic auth challenge on plain HTTP | `MEDIA_HTTP_ALLOW=<BMC subnet>` |
| ISO reads slowly from the iDRAC | HTTPS remote file share: many small range requests, TLS on the iDRAC CPU | Media-only HTTP listener (`MEDIA_HTTP_PORT`, `MEDIA_URL`) |

## Builder

| Symptom | Cause | Fix |
|---|---|---|
| Copied ISO URL has the wrong host name | `PUBLIC_URL` / `MEDIA_URL` in `.env` | Set them to the FQDN the BMCs use, `docker compose up -d` |
| Profile changes from a new version do not show up | `profiles.yaml` and inventories are only seeded on the **first** start | Edit them in the *Profiles* tab |
| UI: the ISO link downloads nothing | The browser blocks HTTP downloads from an HTTPS page / sends the cached UI login | Links use `/media/<iso>` over HTTPS and accept the UI login |
| UI: **URL** button does nothing | Clipboard blocked | It shows the URL in a prompt instead |
| Build error in the UI | xorriso / base ISO problem | The message shows the xorriso stderr tail; `docker compose logs builder` |
| UI login never times out | HTTP Basic auth: the browser keeps it until closed | Close the browser or use a private window |
