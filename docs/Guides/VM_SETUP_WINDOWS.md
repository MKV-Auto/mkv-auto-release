# MKV Auto on Windows — Linux VM setup

Read [why this is necessary](VM_SETUP.md) first if you have not.

> **Not yet verified end to end.** The equivalent macOS procedure was tested
> completely — real disc, real rip, valid MKV. This Windows guide follows the
> same shape and uses well-established mechanisms, but nobody has run it start
> to finish. If you try it, please
> [open an issue](https://github.com/MKV-Auto/mkv-auto-release/issues) saying
> whether it worked — success reports are as useful as failures here.

## Choose your VM software

**Use VMware Workstation Pro.** It is the Windows counterpart of VMware Fusion,
which is the product this project verified on macOS — same vendor, same USB
implementation lineage. It is also free.

| | Verdict |
|---|---|
| **VMware Workstation Pro** | **Recommended.** Free, no licence key, same USB stack family as the tested macOS path |
| **VirtualBox** | Should work — mature USB passthrough, needs the Extension Pack. Not tested by us; see the callout below |
| **Hyper-V** | **Will not work.** No direct USB passthrough — see [VM_SETUP.md](VM_SETUP.md) |
| **WSL2 + `usbipd-win`** | **Will not work.** Stock WSL2 kernel omits `sr_mod` |

> **Using VirtualBox instead? Please report back.** It is a reasonable choice —
> its USB passthrough is mature — we simply have not verified it with this app.
> If you try it, [open an issue](https://github.com/MKV-Auto/mkv-auto-release/issues)
> with your Windows version, VirtualBox version, drive model, and whether a rip
> completed. Enough reports and we will document it as a supported path.
>
> VirtualBox specifics if you go that route: install the **Extension Pack**
> (USB 2.0/3.0 passthrough does not exist without it, and USB 1.1 is far too
> slow), then *Settings → USB → USB 3.0 (xHCI) Controller* and add a device
> filter for your drive. The rest of this guide applies unchanged from step 4.

## 1. Install VMware Workstation Pro

Download from the Broadcom support portal. **Workstation Pro is free for
personal and commercial use and needs no licence key** (17.5.2 and later) — you
only need a free Broadcom account to reach the download.

## 2. Create the VM

Download **Ubuntu Server 24.04 amd64**. Create a VM with:

- **Type** Linux / Ubuntu (64-bit)
- **RAM** 4 GB minimum, **8 GB recommended**
- **Disk** **200 GB minimum**. Rips land in the VM's `/data` volume before they
  go anywhere else, and a single 4K disc can be around 70 GB, so 60 GB is not
  enough to finish one job. Add more if you keep rips on the VM rather than
  transferring them off.

Install Ubuntu Server normally — no desktop environment needed. Enable OpenSSH
during install so you can work from a terminal rather than the VM console.

## 3. Pass the drive through

With the drive plugged in and the VM running:

**VM → Removable Devices → your optical drive → Connect (Disconnect from Host)**

<!-- SCREENSHOT: Workstation's VM > Removable Devices menu with the optical drive -->

To make it attach automatically, set the USB controller to **USB 3.1** under
*VM → Settings → USB Controller* while the VM is shut down.

Also make sure **Windows is not holding the drive**: close any Explorer window
showing it, and disable AutoPlay for it. Windows grabbing the device is a common
cause of passthrough failing.

## 4. Verify the drive reached Linux

```bash
lsusb
ls -l /dev/sr*
```

You may see more than one device — VM software often presents its own virtual
CD. Confirm which one is real:

```bash
sudo dd if=/dev/sr0 of=/dev/null bs=1M count=50 status=progress
```

50 MB without errors means passthrough works. If `/dev/sr0` is the virtual
drive, try `/dev/sr1`.

## 5. Install Docker and MKV Auto

```bash
sudo apt update && sudo apt install -y docker.io
sudo usermod -aG docker $USER && newgrp docker
```

```bash
docker run -d \
  --name mkv-auto \
  -p 8080:80 \
  -v mkv-data:/data \
  --device=/dev/sr0 \
  --privileged \
  --restart unless-stopped \
  ghcr.io/mkv-auto/mkv-auto-release:latest
```

Use whichever `/dev/srN` you confirmed above, or pass both if unsure.

## 6. Open the web UI

```bash
hostname -I
```

Browse to `http://<VM-IP>:8080` from Windows. The default NAT networking makes
the VM reachable from your PC but not the rest of your LAN;
switch the adapter to **Bridged** if you want other devices to reach it.

## Troubleshooting

**Drive not listed under Removable Devices** — Windows is probably holding it.
Close any Explorer window showing the drive and disable AutoPlay for it.
(On VirtualBox, this symptom usually means the Extension Pack is missing.)

**Drive appears but reads fail** — check the USB controller version in
*VM → Settings → USB Controller*; USB 1.1 is far too slow for ripping.

**`MSG:5010 Failed to open disc`** — you are probably targeting a virtual CD
device rather than your real drive. Try the other `/dev/srN`.

**Windows keeps reclaiming the drive** — reconnect it from *VM → Removable
Devices*. Setting the controller to USB 3.1 makes reattachment more reliable.
(On VirtualBox, add a USB device filter instead.)

**Multi-drive setups** — VM passthrough generally does not expose stable
per-drive serials, so MKV Auto falls back to a `sysfs`-derived identity. Fine
for a single drive; multi-drive under a VM cannot rely on stable identity if
devices renumber.
