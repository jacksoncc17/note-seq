#!/usr/bin/env bash
#
# One-time Garage setup: give the node a role, then create the key and bucket the archive
# uses. Safe to re-run -- it skips whatever already exists and prints the credentials at the
# end, which is the only time the secret key is shown in full.
#
#   ./bootstrap.sh                      # bucket and key both called "note-seq"
#   BUCKET=pieces KEY_NAME=work ./bootstrap.sh
#
# Run it on the machine that hosts Garage, from this directory, after `docker compose up -d`.
#
# The layout step is the one people forget: a node with no role assigned accepts no writes
# at all ("NO ROLE ASSIGNED" in `garage status`), and S3 clients see a 503 rather than
# anything that explains itself.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BUCKET="${BUCKET:-note-seq}"
KEY_NAME="${KEY_NAME:-note-seq}"
CAPACITY="${CAPACITY:-10GB}"
ZONE="${ZONE:-dc1}"

g() { docker compose exec -T garage /garage "$@" 2>&1 | sed 's/\x1b\[[0-9;]*m//g'; }

say() { printf '==> %s\n' "$*"; }

say "waiting for Garage"
for i in $(seq 1 30); do
  if g status | grep -q 'HEALTHY NODES'; then break; fi
  sleep 1
  [ "$i" = 30 ] && { echo "Garage did not come up: docker compose logs garage" >&2; exit 1; }
done

# The row after the header, not the header itself: `garage status` prints
# "ID  Hostname  Address ..." first, and taking $1 of that gives you the literal "ID", which
# then fails three lines later with "0 nodes match 'ID'".
#
# A length test rather than /^[0-9a-f]{16}$/: this machine's awk is mawk, which silently
# treats {16} as a literal string -- no error, no match, and the script says the node cannot
# be found while `garage status` plainly shows it.
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

# `key create` prints the secret once. If the key exists already, ask for it explicitly --
# `key info --show-secret` is the only other way to read it back.
if g key info "$KEY_NAME" >/dev/null 2>&1; then
  say "key '$KEY_NAME' exists; reading its id and secret back"
  key_json="$(g key info "$KEY_NAME" --show-secret 2>/dev/null || true)"
  key_id="$(printf '%s' "$key_json" | awk -F'"' '/"AccessKeyId"/{print $4; exit}')"
  key_secret="$(printf '%s' "$key_json" | awk -F'"' '/"SecretAccessKey"/{print $4; exit}')"
else
  say "creating key '$KEY_NAME'"
  created="$(g key create "$KEY_NAME")"
  key_id="$(printf '%s' "$created" | awk '/Key ID/{print $3; exit}')"
  key_secret="$(printf '%s' "$created" | awk '/Secret key/{print $3; exit}')"
fi

if ! g bucket info "$BUCKET" >/dev/null 2>&1; then
  say "creating bucket '$BUCKET'"
  g bucket create "$BUCKET"
else
  say "bucket '$BUCKET' already exists"
fi

say "allowing '$KEY_NAME' to read and write '$BUCKET'"
g bucket allow --read --write --key "$KEY_NAME" "$BUCKET" >/dev/null

endpoint="${ENDPOINT:-http://127.0.0.1:3900}"
cat <<EOF

==> ready

  endpoint  $endpoint      (path-style, region "garage")
  bucket    $BUCKET
  key id    $key_id
  secret    $key_secret

Configure the machine that analyses music with:

  SERVICE_ARCHIVE_URL=$endpoint
  SERVICE_ARCHIVE_BUCKET=$BUCKET
  SERVICE_ARCHIVE_KEY=$key_id
  SERVICE_ARCHIVE_SECRET=$key_secret

The secret is shown here once. It can be read again with:
  docker compose exec garage /garage key info $KEY_NAME --show-secret

If the analysing machine is elsewhere on the network, replace 127.0.0.1 with this host's
address, and make sure port 3900 is reachable (and only 3900: the admin API on 3903 can
create keys and is deliberately not published).
EOF
