# Running MKV Auto on Windows or macOS

MKV Auto needs a **Linux host** for the machine holding your optical drive. This
guide explains why, and points at the per-platform walkthrough.

- [Windows →](VM_SETUP_WINDOWS.md)
- [macOS →](VM_SETUP_MACOS.md)

## Why Docker Desktop is not enough

The container reads your drive directly (`--device=/dev/sr0`), and MakeMKV
issues raw SCSI commands to it for disc structure and decryption. That requires
the machine running Docker to *be* the machine holding the drive.

**Docker Desktop on Windows and macOS does not run containers on your machine.**
It runs them inside a Linux virtual machine, and that VM has no access to
physical optical drives. Your drive exists to Windows or macOS; it does not
exist inside the VM where the container lives.

On macOS, the documented `docker run` fails immediately:

```
docker: Error response from daemon: error gathering device information
while adding custom device "/dev/sr0": not a device node
```

Pointing `--device` at the real macOS node (`/dev/disk8` or similar) fails too —
`no such file or directory` — because the Linux VM cannot see it.

**`--privileged` does not help.** Privilege grants a container full use of the
devices *its kernel* has; it does not create devices that are absent. Inside
Docker Desktop the "host" is that Linux VM, which has virtual disks and no
optical drive. A fully privileged container on macOS lists no `/dev/sr*` at all.

## The fix: give Linux the drive

Run a Linux VM and hand the drive to *it* over USB passthrough. The VM becomes
a real Linux host with a real `/dev/sr0`, and MKV Auto runs there normally.

```
  Your Mac / PC
  └── VMware Fusion (Mac) / Workstation (Win)   ← passes the USB drive through
      └── Linux VM  (a real Linux host)
          └── Docker
              └── MKV Auto  →  /dev/sr0  ✅
```

This is **verified working**, not theoretical: a full Blu-ray rip completed this
way on Apple Silicon, producing a valid MKV.

### What will not work

| Approach | Why not |
|---|---|
| Docker Desktop + `--device` | The VM cannot see the drive; `docker run` fails |
| Docker Desktop + `--privileged` | Privilege is not presence — there is no device to permit |
| **Hyper-V** | No direct USB passthrough. Enhanced Session Mode redirects over RDP rather than presenting an optical device; Discrete Device Assignment is Windows Server only |
| **WSL2 + `usbipd-win`** | The stock WSL2 kernel omits the CD-ROM module (`sr_mod`), and MakeMKV's raw SCSI does not survive USB/IP reliably |

### Honest alternative

A VM works, but it is a workaround. If you have a spare machine, a NAS, or an
Unraid/TrueNAS box, install MKV Auto there instead — no passthrough layer, no
virtualisation quirks, and it can stay powered on without tying up your desktop.

## Before you start

**A USB optical drive.** An internal SATA drive cannot be passed to a VM this
way.

**Check your drive is healthy first.** Faulty USB optical drives are common and
produce failures that look like software bugs. During development one drive
kernel-panicked a Mac on connect while another worked perfectly on the same
ports. If a drive misbehaves at the OS level, it will not improve inside a VM.

**Expect the drive to have a different name than you think.** VM software often
presents its own virtual CD device, so your real drive may be `/dev/sr1` rather
than `/dev/sr0`. Both guides cover identifying the right one.

## After the VM is running

Follow the normal [Installation guide](INSTALLATION.md) *inside the VM* — from
the VM's point of view it is an ordinary Linux host.

Reach the web UI from your desktop browser at the VM's IP on port 8080. If the
VM uses NAT networking it is reachable from your machine but not the rest of
your LAN; switch it to **bridged** networking if you want other devices to
reach it.
