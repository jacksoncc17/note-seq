# Garage, in Docker, for another machine

An S3-compatible store to keep finished pieces in, so the machine that *serves* them can be
wiped and refilled. Garage is a single ~20 MB binary with one config file; the container adds
nothing but a pinned version.

**In this repository it is only deployed here for testing.** The point of it is to run on a
home machine that is not the droplet and not the laptop that analyses music.

## The deployment this was written for

A VM (`vm2`, Ubuntu 22.04, 16 GB RAM) with two disks: `sda` (200 G, the system) and `sdb`
(1000 G, ext4, labelled `data_vol`). The blocks live on `sdb`, the metadata stays with the
system's SSD — LMDB wants the fast device, the blocks are sequential reads and want the room.

```
install.sh                     → v2.1.0 in ~/.local/bin, user unit, user unit + linger/cron
setup-data-disk.sh /dev/sdb /srv/garage/data wei   (sudo, once)
rsync -a ~/garage/data/ /srv/garage/data/          # existing blocks
data_dir = "/srv/garage/data"                      # one line in ~/.config/garage/garage.toml
garage layout assign -z dc1 -c 950GB <node>; garage layout apply --version 2
```

Measured after the move: 15 objects / 145.5 MiB in the bucket, `112 MB` on `sdb`, layout
capacity 950 GB (it was 100 GB before the capacity was re-assigned — Garage's placement maths
uses that number, so it is worth fixing once). A fresh archive write landed on the disk, and
`--restore-from` brought all fifteen back with the audio hashing identically.

## Two ways to run it

`compose.yml` here runs it in Docker, which is the quickest way to try it on a machine that
already has Docker. On a **dedicated Linux machine** the container adds nothing: Garage is one
static binary, one config file and two directories. `native/` is that install, and it is what
the deployment described below actually uses:

```
native/install.sh            # binary + config + systemd unit (system-wide, or for your user
                             #   when sudo needs a password)
native/setup-data-disk.sh    # mount a dedicated disk for the blocks (needs sudo, once)
native/bootstrap.sh          # layout, key, bucket
```

Two things about the native path worth knowing before reading the scripts:

* **metadata and data belong on different disks.** The blocks (`data_dir`) are what grows, so
  they go on the big disk; LMDB (`metadata_dir`) wants the SSD the system is on. The blocks are
  immutable files named by their hash, so moving them is a copy; only the metadata needs the
  snapshot discipline.
* **`nofail` in fstab is a quiet danger.** A dedicated disk that fails to mount leaves Garage
  writing to the root filesystem, and nobody notices until it fills. `install.sh` prints a
  warning when `OBJECTS_DIR` is not a mountpoint, and `setup-data-disk.sh` prints the check to
  run afterwards (`findmnt --target`).

## Run it in Docker

```bash
cp garage.toml.example garage.toml     # then replace the two generated secrets in it
chmod 600 garage.toml                  # Garage refuses world-readable secrets
docker compose up -d
./bootstrap.sh                         # gives the node a role, creates the key and bucket
```

`bootstrap.sh` prints the endpoint, bucket, key id and secret at the end. That is the only time
the secret is shown — `garage key info <name> --show-secret` reads it again if it is lost.

Then, on the machine that analyses music, set:

```bash
SERVICE_ARCHIVE_URL=http://<this-host>:3900
SERVICE_ARCHIVE_BUCKET=note-seq
SERVICE_ARCHIVE_KEY=<key id>
SERVICE_ARCHIVE_SECRET=<secret>
```

Every finished piece is then written to the bucket as `pieces/<slug>.zip` — the same package
the page's own upload endpoint accepts.

## What is published, and what is not

Only port **3900** (the S3 API). The admin API on 3903 is unauthenticated apart from its own
token and can create keys, and the web listing UI on 3902 shows your bucket contents; both
stay on the container's network. If you need to reach them, use `docker compose exec` or an
SSH tunnel, not a published port.

There is no TLS here: Garage speaks plain HTTP on 3900. Inside the LAN that is fine; across
the internet, put it behind a tunnel or a TLS-terminating proxy rather than opening the port.

## A web UI, if you want one

Garage itself ships **no admin UI**: a CLI, an admin HTTP API (JSON, for automation and
monitoring), Prometheus metrics, and — separate thing — the `[s3_web]` endpoint, which serves
bucket *contents* as static websites. That last one is easy to mistake for a browser: per the
[cookbook](https://garagehq.deuxfleurs.fr/documentation/cookbook/exposing-websites/) it does
**not** do directory listings, it supports only an index and an error document, and it is
**publicly** exposed with no authentication — so it is how you *publish* a bucket, not how you
administer one.

Two community UIs exist, and one of them is wired in here:

```bash
GARAGE_ADMIN_TOKEN=$(grep -oP '(?<=^admin_token = ")[^"]+' garage.toml) \
  docker compose --profile ui up -d       # then http://<this host>:3909
docker compose --profile ui down          # and it is gone
```

* **[khairul169/garage-webui](https://github.com/khairul169/garage-webui)** — what is configured
  in `compose.yml`: cluster health and layout, buckets with an object browser, access keys. It
  reads the admin token from the mounted `garage.toml`, and reaches the admin API as
  `http://garage:3903` **inside the compose network**, so 3903 stays unpublished. Verified
  here: the dashboard reports the live cluster (`Healthy`, 1 node, 256 partitions) and the
  bucket's contents (`9.6 MB` — the one archived piece). It also takes an optional
  `AUTH_USER_PASS=user:bcrypt-hash` login (`htpasswd -nbBC 10`), which is worth setting.
* **[stouder-io/mechanic](https://github.com/stouder-io/mechanic)** — the other one, also
  Garage-native, with website config and admin-token management. Its README's image reference
  (`ghcr.io/stouderio/mechanic:latest`) returned *denied* when tried from here, so it is not
  wired in; if you want it, build it from source or use a JAR release.

If a UI is more than you want, the [browsing tools page](https://garagehq.deuxfleurs.fr/documentation/connect/cli/)
lists what Garage's own authors recommend: **`mc`** (MinIO client, "recommended" and a single
static binary), `aws s3`, `rclone` (whose `rclone ncdu` is a terminal browser), `s5cmd`,
Cyberduck/`duck` (which ships a Garage connection profile), and `sftpgo` (which has a web UI and
an SFTP front end over an S3 backend).

**Treat any UI here as an admin console.** It holds the admin token, it can create and delete
keys, and it can delete the archive. Keep it on the LAN — if you want it from elsewhere, put it
behind Tailscale rather than publishing 3909.

## Filling the layout is the step people miss

A node with no role assigned accepts **no writes at all** — `garage status` says
`NO ROLE ASSIGNED`, and S3 clients get a 503 that explains nothing. `bootstrap.sh` does it
(`layout assign` then `layout apply --version 1`); if you do it by hand, both commands are
needed, and the version number must be exactly one more than the current one.

## Backups

The bucket *is* the backup of the pieces, so back up the bucket's metadata in turn:

```bash
docker compose exec garage /garage meta snapshot   # cheap, and the recovery path for LMDB
docker run --rm -v garage_garage-data:/data -v "$PWD":/out alpine \
  tar czf /out/garage-data-$(date +%F).tar.gz -C /data .
```

`metadata_auto_snapshot_interval = "6h"` in `garage.toml` is already doing the first part on a
schedule. LMDB (the default engine) is the one that can corrupt on an unclean shutdown, which
is what the snapshots are for.

## Why the version is pinned

`dxflrs/garage:v2.1.0` and not `latest`: Garage's on-disk format has changed between minor
versions, and an archive that a newer container cannot read is worse than a missing update.
Bump it deliberately, and note that v2 **dropped** `replication_mode` in favour of
`replication_factor` + `consistency_mode` — the config in this directory is the v2 spelling,
and an old `replication_mode = "single"` line fails at startup with
*"The legacy replication_mode is no longer supported."*

## One node, one copy

`replication_factor = 1`: there is no redundancy here, and there cannot be with one machine.
That is why the archive is a *second* copy of something the droplet also has, not the only
copy of anything.
