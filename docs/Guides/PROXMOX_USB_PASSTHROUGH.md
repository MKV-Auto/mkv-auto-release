# Proxmox USB passthrough for mkv-auto

This guide captures findings from the 2026-06-23 debugging session where a Proxmox VM running mkv-auto lost the ability to see its USB optical drive after a host-side USB stack hiccup. If you're running mkv-auto inside a Proxmox guest and your drives are intermittent — or you can't re-enumerate them without rebooting the VM — this guide is for you.

Bare-metal mkv-auto users can skip this doc.

## TL;DR

For multi-drive testing inside a Proxmox guest, **pass through an entire USB host controller** (PCIe passthrough), not individual USB devices. Device-level passthrough breaks in subtle ways that are hard to diagnose from inside the guest.

If you only have a single drive and never plan to test concurrent rips, device-level passthrough is fine — just be ready to detach + re-attach the device in Proxmox whenever the guest VM loses track of it.

## The symptoms (so you know it's this)

The fingerprints of a stuck Proxmox passthrough state look like host kernel issues but are actually upstream:

```
# Multiple phantom entries for the same physical drive
$ lsusb | grep -i pioneer
Bus 002 Device 043: ID 08e4:017a Pioneer Corp. BD-XD07 BD/DVD/CD Writer
Bus 002 Device 044: ID 08e4:017a Pioneer Corp. BD-XD07 BD/DVD/CD Writer   # phantom

# Kernel can't read the USB descriptor — ETIMEDOUT
$ dmesg | grep -i 'descriptor read'
usb 2-2: device descriptor read/64, error -110
usb 2-2: device descriptor read/64, error -110
usb 2-2: device descriptor read/64, error -110

# After multiple retries, the device renumbers and gets stuck
usb 2-1: new high-speed USB device number 44 using xhci_hcd
usb 2-1: can't set config #1, error -32   # ENODEV — passthrough handle exists but device doesn't respond

# Userspace sees a dangling symlink
$ ls -la /dev/disk/by-id/usb-PIONEER_*
lrwxrwxrwx 1 root root 9 ... usb-PIONEER_..._1958040110900395-0:0 -> ../../sr1

$ ls /dev/sr1
ls: cannot access '/dev/sr1': No such file or directory
```

Physically unplugging the drive **does not** produce kernel events inside the guest. The hot-plug events stop at the Proxmox layer because device-level passthrough is configured statically — Proxmox is holding a USB device handle that no longer corresponds to a working device, and it doesn't surface the disconnect to the guest.

## Why device-level passthrough breaks

Proxmox's per-device passthrough (`usbN: ...`) is a mapping from a host USB device path to a guest USB controller's port. When the host kernel:

- resets the USB device (controller error, descriptor timeout)
- renumbers it (hot-plug, eject, USB power management)
- loses access to it (USB stack flake)

…Proxmox doesn't always cleanly re-establish the passthrough. The guest's QEMU USB controller still holds a "device is attached" state, but the underlying device handle is dead. Symptoms inside the guest match what you'd see from a physically broken drive, which makes diagnosis hard.

For multi-drive rip workloads, this is exactly the failure mode you'll trip — sustained `makemkvcon` reads stress the host USB stack, the drive controller hiccups, the host kernel resets, and Proxmox loses track. Even if you fix the underlying hardware issue (e.g., move drives to separate USB host controllers), the Proxmox layer remains a single point of fragility.

## The fix: controller passthrough

Pass an entire **USB host controller** (PCI device) to the guest instead of individual USB devices. The guest's kernel owns the controller end-to-end; hot-plug events propagate natively; the controller is the unit of passthrough state, not each device.

### How to configure

1. **Identify the USB host controller** you want to pass through. On the Proxmox host:

   ```bash
   lspci -nn | grep -iE 'usb'
   # 00:14.0 USB controller [0c03]: Intel Corporation 200 Series Chipset Family USB 3.0 xHCI [8086:a2af]
   ```

2. **Enable IOMMU** on the Proxmox host if you haven't already. Add to `/etc/default/grub`:

   ```
   GRUB_CMDLINE_LINUX_DEFAULT="quiet intel_iommu=on iommu=pt"
   # or for AMD:
   GRUB_CMDLINE_LINUX_DEFAULT="quiet amd_iommu=on iommu=pt"
   ```

   Then `update-grub && reboot`.

3. **Bind the controller to vfio-pci** so the host kernel releases it:

   In `/etc/modprobe.d/vfio.conf`:
   ```
   options vfio-pci ids=8086:a2af
   ```
   (Use the `[xxxx:xxxx]` ID from step 1.)

   Then `update-initramfs -u && reboot`.

4. **Pass the controller to the VM**. In Proxmox web UI: VM → Hardware → Add → PCI Device → select the USB controller. Or in the VM config (`/etc/pve/qemu-server/<vmid>.conf`):

   ```
   hostpci0: 0000:00:14.0,pcie=1
   ```

5. **Restart the VM**.

### Verify

Inside the guest:

```bash
$ lspci | grep -iE 'usb'
00:10.0 USB controller: Intel Corporation 200 Series Chipset Family USB 3.0 xHCI
```

Any USB device you plug into a port on that controller now appears natively to the guest — including hot-plug events. mkv-auto's `/drives/snapshot` will pick up newly inserted drives without restarting the container.

## What to do if you're stuck mid-session

When you hit the symptoms above and need to recover without rebuilding the VM:

1. **Detach the USB device in Proxmox**:
   ```bash
   # On the Proxmox host
   qm set <vmid> --delete usb0  # or whichever usbN holds the affected drive
   ```

2. **Wait 5 seconds**, then **re-attach**:
   ```bash
   qm set <vmid> --usb0 host=08e4:017a  # vendor:product from lsusb
   ```

3. **Verify inside the guest**:
   ```bash
   ls /dev/sr*
   curl http://localhost:8080/api/drives/snapshot
   ```

4. **Restart the mkv-auto container** so it picks up the device cleanly:
   ```bash
   docker restart mkv-auto
   ```

If steps 1–3 don't work, the controller may be stuck at the QEMU level. **Reboot the VM** as a last resort.

## What we learned in the live session

The 2026-06-23 debugging session that motivated this doc had this sequence:

1. Pioneer XD06U was working fine inside the Proxmox guest's mkv-auto container.
2. A two-rip stress test triggered a USB bandwidth-contention failure mode at the host USB layer — controller resets, device disconnects.
3. Host eventually stabilized with the Pioneer attached, but **the guest's view stayed broken** for hours.
4. Symptoms: `lsusb` showed phantom entries, `dmesg` showed `error -110` and `error -32`, no kernel events on physical unplug, `/dev/sr*` missing entirely.
5. Fixing the issue at the Proxmox level (re-attaching the USB device via `qm set`) restored guest visibility.

The mkv-auto codebase has no way to detect or work around this — the failure is upstream of the container, upstream of the guest kernel. The right answer is to configure passthrough correctly from the start.

## Related

- USB bandwidth contention — the host USB layer can hiccup under multi-drive load (shared host controllers); putting drives on separate USB host controllers helps on bare metal too. This Proxmox guide is in addition to that.
- By-id stable drive identity — the codebase tracks drives by stable `/dev/disk/by-id` identity. It works correctly under controller passthrough; fails silently when device passthrough loses track of the device handle.
- [`HOST_OPTICAL_SETUP.md`](../HOST_OPTICAL_SETUP.md) — host-side device setup (separate from virtualization).
