# Garage, in Docker, for another machine

An S3-compatible store to keep finished pieces in, so the machine that *serves* them can be
wiped and refilled. Garage is a single ~20 MB binary with one config file; the container adds
nothing but a pinned version.

**In this repository it is only deployed here for testing.** The point of it is to run on a
home machine that is not the droplet and not the laptop that analyses music.

## Run it

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
