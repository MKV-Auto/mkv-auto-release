# Udev hook for MakeMKV-Auto

This hook lets the OS notify MakeMKV-Auto when a disc is inserted/ejected so the drive manager can rescan automatically.

## Files
- `99-mkva-disc.rules.template`: Template file with placeholders (used by manage.sh and Docker)
- `../../Backend/drive_manager/udev_notify.py`: Python script that sends events to Drive Manager via Unix Domain Socket (UDS)

## Automatic Install (Recommended)

Use `manage.sh` which automatically installs the rules with correct paths:

```bash
cd /home/user/MKV-Auto
./manage.sh start  # Or restart
```

This generates rules from the template and installs them automatically.

## Manual Install

If you need to install manually without manage.sh:

```bash
# Generate rules from template
cd /home/user/MKV-Auto
sed -e "s|__MKVAUTO_ROOT__|$(pwd)|g" \
    -e "s|__MKVAUTO_TMP_DIR__|$(pwd)/tmp|g" \
    -e "s|__PYTHON_BIN__|/usr/bin/python3|g" \
    -e "s|__UDEV_NOTIFY_SCRIPT__|$(pwd)/Backend/drive_manager/udev_notify.py|g" \
    Deploy/udev/99-mkva-disc.rules.template | sudo tee /etc/udev/rules.d/99-mkva-disc.rules

# Reload udev
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=block --action=change
```

## How it works
- udev emits `ACTION=change` with `DISK_MEDIA_CHANGE=1` for **both insert and eject** (optical drives don't use DISK_MEDIA_CHANGE=2)
- The rule triggers `udev_notify.py` on any media change event
- `udev_notify.py` queries the actual media state using `udevadm info` to check `ID_CDROM_MEDIA`:
  - `ID_CDROM_MEDIA=1` → Media present → sends `{"action": "insert", ...}`
  - `ID_CDROM_MEDIA=0` or missing → No media → sends `{"action": "eject", ...}`
- The Drive Manager UDS server processes these events and:
  - On insertion: Invalidates cache for the disc, allowing fresh scans
  - On ejection: Clears cached disc info, triggers a rescan, and emits drive change events
- The frontend detects the ejected disc and clears the screen automatically.
- Built-in debouncing (0.5s) prevents duplicate processing of multiple change events

## Benefits of UDS over HTTP
- **Faster**: No network overhead, direct IPC
- **More reliable**: No dependency on HTTP server availability
- **Simpler**: No curl/systemd-run workarounds needed
- **Lower latency**: Direct socket communication

The UDS socket is located at `{MKVAUTO_TMP_DIR}/drive_manager.sock` and is created automatically when the Drive Manager service starts.
