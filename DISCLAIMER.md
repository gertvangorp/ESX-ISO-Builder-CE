# Disclaimer

**Community project – not affiliated with, endorsed by or supported by Broadcom Inc. or VMware.**
VMware, ESX, ESXi, vSphere, vCenter, vSAN, NSX and VMware Cloud Foundation (VCF) are trademarks or
registered trademarks of Broadcom Inc. and/or its subsidiaries. Dell, iDRAC and PowerEdge are trademarks
of Dell Inc. Minisforum is a trademark of its owner. Other names are trademarks of their respective owners.
They are used here only to describe what the software works with.

**No Broadcom software is included.** You supply your own ESX installer ISOs and are responsible for
having the right licences and for complying with Broadcom's license terms.

**Destructive by design.** The generated kickstart wipes **all local disks** of the host it boots
(`clearpart --alldrives`). Check the inventory, the boot disk and the target host before you build or
install. Never boot a generated ISO on a host whose data you need.

**Lab and homelab use.** This tool and its example profiles are meant for labs, demos and homelabs. Some
options – for example the profiles for consumer hardware such as the Minisforum MS-A2, the RDSEED/CPUID
masks, or NVMe memory tiering on non-certified devices – are **not supported by Broadcom** and may affect
supportability, upgrades or stability. Do not use them in production without your own review and testing.

**Security.** The web UI uses HTTP Basic authentication; the optional media-only HTTP listener can serve
ISOs without a login to an allow-listed subnet (for BMC virtual media). Run it on a trusted management
network, use your own TLS certificate and strong passwords, and restrict the ports in your firewall.

**No warranty.** The software is provided "as is", without warranty of any kind, as set out in the
[LICENSE](LICENSE). You use it at your own risk; the authors are not liable for any damage, data loss or
downtime resulting from its use.
