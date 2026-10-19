#!/usr/bin/env bash
#
# Put Garage's data blocks on a dedicated disk. Run once, with sudo, on the machine that
# hosts Garage:
#
#   sudo ./setup-data-disk.sh /dev/sdb /srv/garage/data wei
#
# Why a script instead of three commands in a README: the fstab entry is the fragile part.
# Without `nofail` a disk that fails to appear stops the VM booting; with `nofail` and a
# forgotten mount, Garage quietly writes a terabyte of blocks to the root filesystem instead.
# So this refuses to reuse a device that is already mounted, refuses a filesystem it does not
# know, and prints the check to run afterwards.
#
# It formats only an empty device, and asks before doing that.
set -euo pipefail

device="${1:?usage: $0 <device> <mountpoint> [owner]}"
mount_point="${2:?usage: $0 <device> <mountpoint> [owner]}"
owner="${3:-${SUDO_USER:-root}}"

[ "$(id -u)" = 0 ] || { echo "run this with sudo" >&2; exit 1; }
[ -b "$device" ] || { echo "$device is not a block device (try: lsblk)" >&2; exit 1; }

if findmnt -rno TARGET --source "$device" >/dev/null 2>&1; then
  echo "$device is already mounted -- refusing to touch it" >&2
  exit 1
fi

fs="$(blkid -o value -s TYPE "$device" 2>/dev/null || true)"
case "$fs" in
  '')
    echo "$device has no filesystem."
    read -r -p "Format it as ext4? This erases whatever is on it. [yes/no] " answer
    [ "$answer" = yes ] || { echo "nothing done"; exit 1; }
    mkfs.ext4 -L garage-data "$device"
    fs=ext4
    ;;
  ext4|xfs) echo "$device already has $fs -- keeping it" ;;
  *) echo "$device is $fs; Garage is happy with ext4 and xfs, and this will not format anything else" >&2
     exit 1 ;;
esac

uuid="$(blkid -o value -s UUID "$device")"
[ -n "$uuid" ] || { echo "could not read a UUID from $device" >&2; exit 1; }

if grep -q "UUID=$uuid" /etc/fstab; then
  echo "fstab already has an entry for UUID=$uuid"
else
  printf 'UUID=%s %s %s defaults,nofail 0 2\n' "$uuid" "$mount_point" "$fs" >> /etc/fstab
  echo "added to /etc/fstab (nofail: a missing disk must not stop the boot)"
fi

mkdir -p "$mount_point"
systemctl daemon-reload
mountpoint -q "$mount_point" || mount "$mount_point"
chown -R "$owner:$owner" "$mount_point"

echo
echo "==> $device is mounted at $mount_point ($(df -h --output=size,avail "$mount_point" | tail -1 | awk '{print $1" total, "$2" free"}'))"
echo
cat <<EOF
Next, with the service stopped (no sudo needed):

  systemctl --user stop garage
  rsync -a ~/garage/data/ $mount_point/     # existing blocks, if any
  # then set data_dir = "$mount_point" in ~/.config/garage/garage.toml
  systemctl --user start garage

And check that it really is on the disk, not on /:

  findmnt --target ~/garage/data
  df -h ~/garage/data

One more thing worth doing once: tell Garage how much room it has, so its layout maths is
not guessing (from the machine running garage):

  garage -c ~/.config/garage/garage.toml layout assign -z dc1 -c 900GB <node-id>
  garage -c ~/.config/garage/garage.toml layout apply --version 2
EOF
