#!/usr/bin/env bash
#
# Give the node a role, then create the key and bucket the archive uses. Run after install.sh,
# on the machine that hosts Garage. Safe to re-run: it skips what already exists and prints
# the credentials at the end.
#
#   ./bootstrap.sh                    # bucket and key both "note-seq"
#   BUCKET=pieces KEY_NAME=work CAPACITY=100GB ./bootstrap.sh
#
# The layout step is the one people forget: a node with no role assigned accepts no writes at
# all ("NO ROLE ASSIGNED" in `garage status`), and S3 clients get a 503 that explains nothing.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BUCKET="${BUCKET:-note-seq}"
KEY_NAME="${KEY_NAME:-note-seq}"
# Garage uses this for placement decisions and for reporting free space; it should match the
# disk under `data_dir`, not a number typed once. Derived from the filesystem when unset,
# leaving a little headroom.
if [ -z "${CAPACITY:-}" ]; then
  if [ -d "$DATA_DIR" ]; then
    size_gb="$(df -B1G --output=size "$DATA_DIR" 2>/dev/null | tail -1 | tr -d ' ')"
    if [ -n "${size_gb:-}" ] && [ "$size_gb" -gt 20 ] 2>/dev/null; then
      CAPACITY="$((size_gb - size_gb / 20))GB"      # 5% held back
    fi
  fi
  CAPACITY="${CAPACITY:-100GB}"
fi
ZONE="${ZONE:-dc1}"

if command -v garage >/dev/null 2>&1; then
  BIN=garage
  CONFIG="${CONFIG:-/etc/garage.toml}"
elif [ -x "$HOME/.local/bin/garage" ]; then
  BIN="$HOME/.local/bin/garage"
  CONFIG="${CONFIG:-$HOME/.config/garage/garage.toml}"
else
  echo "no garage binary found (install.sh puts it in ~/.local/bin or /usr/local/bin)" >&2
  exit 1
fi

g() { "$BIN" -c "$CONFIG" "$@" 2>&1 | sed 's/\x1b\[[0-9;]*m//g'; }

# The data directory, read from the config rather than assumed: it is the dedicated disk when
# setup-data-disk.sh has been run, and `df` on the wrong path would size the node wrongly.
DATA_DIR="$(grep -E '^data_dir' "$CONFIG" 2>/dev/null | head -1 | cut -d'"' -f2 || true)"
say() { printf '==> %s\n' "$*"; }

say "waiting for Garage"
for i in $(seq 1 30); do
  if g status | grep -q 'HEALTHY NODES'; then break; fi
  sleep 1
  [ "$i" = 30 ] && { echo "Garage is not answering: systemctl --user status garage" >&2; exit 1; }
done

# The row after the header, not the header itself. A length test rather than /^[0-9a-f]{16}$/:
# mawk silently treats {16} as a literal string, so the regex matches nothing and the script
# claims the node cannot be found while `garage status` plainly shows it.
node="$(g status | awk '/HEALTHY NODES/{found=1; next}
                        found && length($1) == 16 && $1 ~ /^[0-9a-f]+$/ {print $1; exit}')"
[ -n "$node" ] || { echo "could not find the node id in 'garage status'" >&2; exit 1; }

if g status | grep -q 'NO ROLE ASSIGNED'; then
  say "assigning $CAPACITY in zone $ZONE to node $node"
  g layout assign -z "$ZONE" -c "$CAPACITY" "$node"
  g layout apply --version 1
else
  say "node $node already has a role"
fi

if g key info "$KEY_NAME" >/dev/null 2>&1; then
  say "key '$KEY_NAME' exists; reading it back"
  info="$(g key info "$KEY_NAME" --show-secret 2>/dev/null || true)"
  key_id="$(printf '%s' "$info" | awk -F'"' '/"AccessKeyId"/{print $4; exit}')"
  key_secret="$(printf '%s' "$info" | awk -F'"' '/"SecretAccessKey"/{print $4; exit}')"
else
  say "creating key '$KEY_NAME'"
  created="$(g key create "$KEY_NAME")"
  key_id="$(printf '%s' "$created" | awk '/Key ID/{print $3; exit}')"
  key_secret="$(printf '%s' "$created" | awk '/Secret key/{print $3; exit}')"
fi

if g bucket info "$BUCKET" >/dev/null 2>&1; then
  say "bucket '$BUCKET' already exists"
else
  say "creating bucket '$BUCKET'"
  g bucket create "$BUCKET"
fi
say "allowing '$KEY_NAME' to read and write '$BUCKET'"
g bucket allow --read --write --key "$KEY_NAME" "$BUCKET" >/dev/null

host="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

==> ready

  endpoint  http://${host:-<this host>}:3900      (path-style, region "garage")
  bucket    $BUCKET
  key id    $key_id
  secret    $key_secret

On the machine that analyses music:

  SERVICE_ARCHIVE_URL=http://${host:-<this host>}:3900
  SERVICE_ARCHIVE_BUCKET=$BUCKET
  SERVICE_ARCHIVE_KEY=$key_id
  SERVICE_ARCHIVE_SECRET=$key_secret

Then archive what already exists, and refill a serving deployment from it:

  python -m service.app.manage --archive-all
  python -m service.app.manage --restore-from $BUCKET

The secret is shown once; read it again with:
  $BIN -c $CONFIG key info $KEY_NAME --show-secret
EOF
