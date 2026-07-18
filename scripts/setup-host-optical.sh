#!/bin/bash
# Run this script on the HOST (not in Docker) to prevent disc auto-reinsertion
# Only needed if container's automatic setup doesn't work

set -e

echo "=== MKV-Auto Host Optical Drive Setup ==="
echo ""
echo "This script configures your host to prevent optical drives from"
echo "auto-reingesting discs when they're ejected."
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "ERROR: Must run as root (use sudo)"
    echo "Example: sudo $0"
    exit 1
fi

# 1. Disable CD-ROM autoclose
echo "1. Disabling CD-ROM autoclose..."
if echo 0 > /proc/sys/dev/cdrom/autoclose 2>/dev/null; then
    echo "   ✓ Set autoclose=0"
else
    echo "   ✗ Failed to set autoclose"
    exit 1
fi

# Make permanent across reboots
if ! grep -q "dev.cdrom.autoclose" /etc/sysctl.conf 2>/dev/null; then
    echo "dev.cdrom.autoclose = 0" >> /etc/sysctl.conf
    echo "   ✓ Made permanent in /etc/sysctl.conf"
else
    echo "   ✓ Already configured in /etc/sysctl.conf"
fi

# 2. Configure udisks2 to ignore optical drives (optional)
echo ""
echo "2. Configuring udisks2 (optional)..."
UDISKS_CONF="/etc/udisks2/mount_options.conf"
if [ -f "$UDISKS_CONF" ]; then
    if ! grep -q "optical" "$UDISKS_CONF" 2>/dev/null; then
        cat >> "$UDISKS_CONF" <<'EOF'

# Don't auto-mount optical media (managed by MKV-Auto container)
[optical]
allow = *
defaults = noauto
EOF
        echo "   ✓ Configured udisks2 to not auto-mount optical media"
    else
        echo "   ✓ udisks2 already configured"
    fi
else
    echo "   ⚠ udisks2 config not found (may not be needed)"
fi

# 3. Verify settings
echo ""
echo "=== Verification ==="
AUTOCLOSE=$(cat /proc/sys/dev/cdrom/autoclose 2>/dev/null || echo "unknown")
if [ "$AUTOCLOSE" = "0" ]; then
    echo "✓ CD-ROM autoclose: disabled"
else
    echo "✗ CD-ROM autoclose: still enabled ($AUTOCLOSE)"
fi

echo ""
echo "=== Setup Complete ==="
echo "Optical drives are now configured to work properly with MKV-Auto."
echo "No restart required."
echo ""
echo "Test by ejecting a disc - it should stay out now!"
