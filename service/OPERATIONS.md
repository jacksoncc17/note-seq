# Operations: the commands that keep this running

Every command below was either run on the machine it names or taken from the code and
configuration it talks to; the few that were not exercised from this environment are marked
*(not run here)*. Where a number was measured it says so, because "should be fine" is how an
operations note ages badly.

Three machines, three jobs:

| machine | what it is | what runs on it |
|---|---|---|
| the droplet (`www.jinshiming.net`) | public host | Traefik, the broker (FastAPI + Postgres in docker compose), the note-seq page served at `/note-seq`, the `note-seq` container |
| `192.168.1.11` (`vm2`) | the archive VM, Ubuntu 22.04, one vCPU | Garage on `:3900` (S3) with the web UI on `:3909`, and the worker `vm-note-seq` as a **systemd user unit** |
| this box | development and the LAN service | two checkouts, the LAN service on `:8100`, the worker `lan-note-seq` under `worker-ctl.sh` |

Data lives in: the droplet's `note-seq-data` volume (pieces, inside the container), the VM's
checkout (`~/note-seq-desktop/service/data`) plus Garage's blocks on `/dev/sdb` at
`/srv/garage/data`, and this box's `service/data`.

---

## 1. Secrets: where they are, and the two commands that mint new ones

| secret | lives in | for |
|---|---|---|
| broker `SECRET_KEY` | droplet `~/full-stack-fastapi-template/.env.secret` | signing the tokens the page trusts |
| `FIRST_SUPERUSER` / `_PASSWORD` | same | the admin account used by the curls below |
| `NOTE_SEQ_UPLOAD_TOKEN` | droplet `.env` | the page's own upload token = `SERVICE_PUBLISH_TOKEN` on workers |
| `NOTE_SEQ_OPERATOR_SUBJECTS` | droplet `.env` | whose broker user id sees *all* pieces (the operator) |
| `WORKER_KEY` | each worker's `service/broker/worker.env` (0600, gitignored) | claiming jobs |
| `SERVICE_ARCHIVE_KEY` / `_SECRET` | same `worker.env` | writing the archive |
| Garage RPC/admin tokens | VM `~/.config/garage/garage.toml` (0600, gitignored) | the store and its admin API |
| web UI login | VM `~/.config/garage-webui/env` (0600) | `:3909` |

Rules that were learned the hard way: read passwords with `read -rsp` so they never reach
`argv` or history; put secrets in these files rather than command lines; check with
`git check-ignore -v <file>` before committing anything that looks like one.

### A broker token (superuser, valid 8 days)

```bash
read -rsp 'Broker admin email: ' EMAIL; echo
read -rsp 'Broker admin password: ' PW; echo

TOKEN=$(curl -sS -X POST https://www.jinshiming.net/api/v1/login/access-token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "username=$EMAIL" \
  --data-urlencode "password=$PW" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

unset PW
curl -s https://www.jinshiming.net/api/v1/users/me -H "Authorization: Bearer $TOKEN" \
  | python3 -c 'import json,sys; u=json.load(sys.stdin); print(u["email"], "superuser:", u["is_superuser"])'
```

The form field is `username` and it carries the **email** — that is `OAuth2PasswordRequestForm`'s
quirk, not a mistake in the command. Keep the token in a 0600 file if you want to reuse it
(`umask 077; printf '%s\n' "$TOKEN" > ~/.broker-token`).

### A worker key (returned exactly once)

```bash
curl -s -X POST https://www.jinshiming.net/api/v1/admin/workers/ \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "vm-note-seq"}' | tee worker-registration.json
python3 -c 'import json; print(json.load(open("worker-registration.json"))["api_key"])'
```

The broker stores a hash; there is no "show me the key again". **Re-registering the same name
rotates it**, which is both the recovery path and the way to break a worker that is still using
the old one. Give each machine its own name (`vm-note-seq`, `lan-note-seq`) so rotations don't
collide.

### Who is a worker, and what it is doing

```bash
curl -s https://www.jinshiming.net/api/v1/admin/workers/list \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("offline after", d["offline_after_minutes"], "min")
for w in d["data"]:
    print(f"{w[\"name\"]}  online={w[\"online\"]}  last beat={w[\"seconds_since_heartbeat\"]}s")
    print("   capabilities:", w["capabilities"])
    print("   status      :", w["status"])'
```

`capabilities` is what the worker *can* do (declared at registration); `status` is what it last
reported *doing*. The broker never routes on either — routing is the service the worker bound to
at startup — so both are informational.

---

## 2. Broker and database (on the droplet)

```bash
cd ~/full-stack-fastapi-template

# (not run here: the droplet is theirs. These are the stack's own conventions.)
docker compose ps                              # what is up
docker compose logs -f --tail=100 backend      # the API, including startup migrations
docker compose exec backend alembic current     # the schema revision it is on
docker compose exec backend alembic upgrade head        # entrypoint does this on every start
docker compose exec backend alembic downgrade d9f3b6c2a4e8   # drop the worker.status column only

# the database, in the stack's own container (user postgres, database app)
docker compose exec -T db psql -U postgres -d app -c '\dt'
docker compose exec -T db psql -U postgres -d app -c 'select name, is_active, last_heartbeat_at from worker;'

./deploy/deploy.sh          # rebuild the image and restart -- NOT --no-build, which reuses the old one
docker compose restart backend   # dev stack only: code is synced from ./backend, migrations run on start
```

**One-off: clear heartbeat reports left in `capabilities`** (written by a broker built before
the capabilities/status split; nothing overwrites them now):

```bash
docker compose exec -T db psql -U postgres -d app -c "
  UPDATE worker SET capabilities = NULL
   WHERE capabilities->>'processed' IS NOT NULL
      OR capabilities->>'toolkit'   IS NOT NULL
      OR capabilities->>'fake'      IS NOT NULL;"
```

The predicate matches a heartbeat report by the keys it carries, so a real declaration (for
example `{"soundfont": "Sonatina_Symphonic_Orchestra.sf2"}`) survives. `capabilities ? 'x'` does
not work — that operator is `jsonb`-only and the column is `json`; `->>` is defined for both.
Verified on Postgres 18.

**Reclaiming a wedged queue.** A job whose worker died stays claimed until its lease expires
(default 3600 s). Rather than waiting, restart the API and let `reap_expired_leases` run, or
reap explicitly:

```bash
curl -s -X POST https://www.jinshiming.net/api/v1/jobs/admin/reap -H "Authorization: Bearer $TOKEN"
```

*(not run here — it needs the superuser token.)*

---

## 3. The note-seq page and its service

```bash
# health, from anywhere: outbox depth, archive settings, how many pieces, whether it accepts uploads
curl -s https://www.jinshiming.net/note-seq/api/v1/status | python3 -m json.tool

docker compose restart note-seq     # after changing .env (not after changing code -- that needs a build)
```

Fields worth knowing in that status: `outbox` (pieces the page refused, waiting to be resent),
`archive` (whether an archive is configured), `jobs`/`pieces` counts, `read_only` and
`accept_pieces` (the two independent kill switches).

The overlay that adds the page to the broker's stack (only needed on a fresh droplet):

```bash
docker compose -f compose.yml -f compose.deploy.yml \
  -f ../note-seq/service/deploy/compose.note-seq.yml up -d --wait --build
```

---

## 4. Workers

```bash
# this box -- pid file and log under service/data/
./service/broker/worker-ctl.sh start | stop | restart | status | log | services | show

# the VM -- systemd user unit, installed by service/deploy/worker/install.sh
systemctl --user restart note-seq-worker
systemctl --user status note-seq-worker
journalctl --user -u note-seq-worker -f

# either machine, in the foreground: prints its settings, claims one job, exits
./service/broker/run_worker.sh --once
./service/broker/run_worker.sh --list-services     # what it would load, no broker call
```

Things that bite:

- **A plugin or schema change needs a worker restart**; the contract is published at startup and
  the broker's submit form is built from what was published then.
- **A worker that runs a copy of `worker.py`** (the VM does, at `~/note-seq-worker/worker.py`)
  does not change when the checkout does. Patch the checkout, then copy and restart:
  `scp worker.py wei@192.168.1.11:~/note-seq-worker/worker.py && ssh wei@192.168.1.11 systemctl --user restart note-seq-worker`.
- **Two workers claim from one queue**. `claim_job` uses `FOR UPDATE SKIP LOCKED`, so a job goes
  to whoever polls first and never to both. Stop one with `worker-ctl.sh stop` if you want all
  work on the archive VM.
- **The page marks an idle worker offline after 3 minutes** (`OFFLINE_AFTER_MINUTES`), because the
  worker only heartbeats at startup and after a job. Expect `online=false` and a stale `status`
  between jobs; a restart refreshes both.
- **A worker key that was rotated** shows up as `403 Invalid or inactive worker key` in the
  worker's log, and the worker keeps retrying — paste the new key into `worker.env` and restart.

---

## 5. Pieces: inspect, evict, archive, restore

All of these are `manage.py`, run **where the store and the archive are both reachable** — in
practice a checkout on the LAN (its `service/data` plus Garage on `:3900`). The droplet cannot
reach the archive at all, so `--evict` and `--restore-from` are not things it can do today; its
own volume is pruned by neither. Nothing is removed unless `--delete` is given.

```bash
python -m service.app.manage                       # summary: what is on disk, what is stale
python -m service.app.manage --evict               # dry run: what would go, and why it declines
python -m service.app.manage --evict --delete      # actually reclaim
```

Eviction has three preconditions per piece, all of which must pass:

1. `pieces/<slug>.zip` in the archive exists **and its size matches a freshly packed package**
   (a stale or half-written archive entry fails, and the piece stays);
2. the serving disk is at least `--max-used-percent` full (default **80**; `0` ignores free space);
3. the piece has not been accessed for `--older-than` days (default **15**), except the
   `--keep-used N` most recently used ones.

What stays: `meta.json`, `map.json`, `geometry.json` — the piece still lists and describes
itself, with `meta.evicted = {at, bytes, archive}`. What goes: recording, pages, score files.
The page then shows a notice, and audio/image answer 404 until it is restored.

```bash
python -m service.app.manage --archive-all               # pieces published before an archive existed
python -m service.app.manage --restore-from note-seq     # bring archived pieces (or one evicted one) back
python -m service.app.manage --restore-from note-seq --overwrite   # replace what is here
python -m service.app.manage --outbox                    # pieces the page has not taken
python -m service.app.manage --scratch                   # half-built pieces, orphans, job dirs
python -m service.app.manage --renders                   # data/output/* from the analyzer
python -m service.app.manage --tools                     # ffmpeg / fluidsynth / yt-dlp versions
python -m service.app.manage --adopt-unowned --owner <user-id>   # pieces from before ownership

# the demo piece: one recording every signed-in account can open
python -m service.app.manage --public <slug>        # writes "public": true into meta.json
python -m service.app.manage --unpublic <slug>      # and back behind its owner
```

There is **no automatic rehydration yet**: the droplet cannot reach the archive, so a page that
needs an evicted piece back is a `--restore-from` on the VM, by hand, or the broker-mediated
"rehydrate" job that is still unwritten.

The other half of the same story, when a piece never reached the page:

```bash
python -m service.app.publish --status            # what is waiting in data/outbox
python -m service.app.publish --watch             # keep pushing, every --interval seconds
python -m service.app.publish --queue-all         # re-queue everything on disk
python -m service.app.publish --queue <slug>      # one piece
```

A 401 from the page means `SERVICE_PUBLISH_TOKEN` is wrong or missing; the piece is safe in the
outbox, and `/api/v1/status` shows `outbox: N`.

**The example piece.** Marking one is a metadata write on whichever store serves it -- the
droplet's volume, so inside the stack: `docker compose exec note-seq python -m
service.app.manage --public <slug>`. It takes effect on the next request: no deploy, no page
change. Every signed-in account then lists and opens it, it keeps its owner, and `--evict`
skips it. The gate still runs first, so this is "every user who logged in", not "everybody".

---

## 6. Garage, the archive

```bash
# on the VM -- the installer puts the binary in ~/.local/bin, which a non-login shell lacks
~/.local/bin/garage status         # node, layout, and the block/version counters
~/.local/bin/garage bucket list
~/.local/bin/garage key list
~/.local/bin/garage layout show    # capacity per zone -- the number placement maths uses
systemctl --user status garage garage-webui
journalctl --user -u garage -f
```

The web UI is at `http://192.168.1.11:3909` (login in `~/.config/garage-webui/env` on the VM;
it authenticates `/api/*`, not `/`). The admin API is loopback-only on `:3903` by design; the
S3 API on `:3900` is the one thing other machines reach. A bucket that failed to mount its disk
is the failure worth checking first — `garage status` says what `data_dir` it is writing to, and
`setup-data-disk.sh` exists because writing blocks to the root filesystem fills it quietly.

---

## 7. Git: two remotes, one history, and the patch habit

| branch | remote | carries |
|---|---|---|
| `piano-music-audio-sync` | GitHub `mine` (jacksoncc17/note-seq) | `note_seq/` + `service/` — the service-only branch; `analyzer/`, `memorization/`, `tutor/` are untracked here and only on disk |
| `desktop` | LAN GitLab `192.168.1.3:wei/note-seq-desktop` | the same plus `analyzer/`, `memorization/`, `tutor/`, `grub-triage/` — what the worker machine clones |
| `main` | `origin` = magenta upstream | untouched |

Changes to `service/` land on both: commit on `desktop`, cherry-pick onto the service branch
(analyzer changes exist only on `desktop`). The mechanics used here are a temporary worktree,
because the service branch has `analyzer/` untracked and cannot check `desktop` out in place:

```bash
git worktree add --detach .scratch/wtN desktop
cp -r <changed files> .scratch/wtN/<paths>/
cd .scratch/wtN && git add -A && git commit -m "..." && git branch -f desktop HEAD
cd - && git push gitlab desktop && git worktree remove --force .scratch/wtN
git cherry-pick <sha> && git push mine piano-music-audio-sync
```

**Applying a broker patch** (the reference checkout is read-only to the agent, so changes arrive
as patches in `service/broker/patches/`):

```bash
cd ~/playground/goGames/selfCommentary/full-stack-fastapi-template
P=/home/wei/music_gen/note-seq/service/broker/patches/capabilities-vs-status.patch

git apply --check "$P" && echo applies clean   # writes nothing
git apply --index "$P" \
  && git commit -m "broker: capabilities and status are two columns, not one"
```

`--index` is the part that matters: it stages the patch's new files (migration, tests) as `A`
instead of leaving them untracked, where a `git clean -fd` would delete them. Then rebuild the
droplet (`./deploy/deploy.sh`) and restart any worker that runs a copy of `worker.py`.
`service/broker/patches/README.md` has the full procedure, what each patch changes, and what it
was verified against.

**Updating the VM's checkout:**

```bash
ssh wei@192.168.1.11
cd ~/note-seq-desktop && git pull && ./service/deploy/worker/install.sh --check
```

`--check` is read-only and says whether the machine is still complete (packages, venv, the
broker's `worker.py`, `worker.env`, the unit, and which soundfont will be used).

---

## 8. Tests, before believing a change

```bash
cd /home/wei/music_gen/note-seq
.venv/bin/python -m pytest analyzer/tests service/tests -q     # 174 passed, 3 skipped, ~2 min
```

The skips are the pieces that need `/home/wei/sheet_music/*` and the network; they say so.
`service/tests` is the one that matters for serving, `analyzer/tests` for the alignment. For the
broker patch, the suite is theirs: `pytest tests` inside the backend, 60 passed / 2 failed both
before and after the worker patch (SMTP and a private-route 404 in that environment).

---

## 9. Troubleshooting, by symptom

| symptom | likely cause | what to do |
|---|---|---|
| Workers page shows a path or `{"processed": N}` under **Capabilities** | a row written before the capabilities/status split, or an old `worker.py` copy | the one-off `UPDATE worker SET capabilities = NULL …`; update + restart the worker |
| `status` still shows a toolkit path | worker runs a pre-`status-toolkit-default` `worker.py` | copy the patched `worker.py`, restart; verify with the stub-broker trick below |
| worker shows **offline** while it is fine | only heartbeats at startup and after a job; offline after 3 min | restart it to refresh, or accept it between jobs |
| a job sits in `claimed` forever | worker died mid-job | wait out the lease, or `POST /jobs/admin/reap` |
| uploads vanish / `outbox: N` in status | `SERVICE_PUBLISH_TOKEN` wrong or the page is down | fix the token in `worker.env`, then `python -m service.app.publish --watch` |
| `python -m service.app.publish --status` shows `the page refused it: HTTP 400 … unexpected file in the package: 'audio.flac'` | the **page is older than the worker**: it predates the FLAC storage change and still expects `audio.wav` | redeploy note-seq on the droplet (`./deploy/deploy.sh`), then `publish --watch`; the piece is safe in the outbox meanwhile |
| `URLError ... _ssl.c:2406` on publish | **not TLS** — the page refused the request before reading the body (usually a missing/incorrect upload token) | check the token; a 1 kB upload gets a clean 401, a large one gets this |
| submit answers **422** naming a schema keyword | the broker predates that keyword (`accept`, for one) | use the intersection in the plugin's `param_schema`; there is a test pinning it |
| two machines map the same piece differently | different soundfont (`$HOME/soundfonts/…` vs `FluidR3_GM`) | put the same `.sf2` at `$HOME/soundfonts/Sonatina_Symphonic_Orchestra.sf2`, or set `NOTE_SEQ_SOUNDFONT`; the analyzer now refuses a bad override rather than falling back |
| a YouTube job fails cryptically | stale `yt-dlp` | the job log names it; `pip install -U yt-dlp` in the worker's venv, and `manage.py --tools` shows what is installed |
| `--evict` reclaims nothing | the disk is under the watermark (e.g. 77.2 % against 80) | that is policy; `--max-used-percent 0` forces it, deliberately |
| a piece 404s but is listed | it was evicted | `manage.py --restore-from note-seq` on a machine that can reach the archive |

**Reading a payload instead of guessing** — when the question is "what does the worker actually
send?", point it at a stub broker and log the body. That is how the toolkit path in the status
report was found, after two wrong readings of the code:

```bash
cat > /tmp/fakebroker.py <<'PY'
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        print(json.dumps({'path': self.path, 'body': self.rfile.read(n).decode()}), flush=True)
        out = b'null' if self.path.endswith('/worker/claim') else b'{"ok":true}'
        self.send_response(200); self.send_header('Content-Length', str(len(out)))
        self.end_headers(); self.wfile.write(out)
HTTPServer(('127.0.0.1', 8898), H).serve_forever()
PY
.venv/bin/python /tmp/fakebroker.py > /tmp/fb.log 2>&1 &
.venv/bin/python ~/note-seq-worker/worker.py --broker http://127.0.0.1:8898/api/v1 \
  --key dummy --name probe --plugin-dir ~/note-seq-desktop/service/broker \
  --service note_seq_align --once
grep heartbeat /tmp/fb.log
```

---

## 10. What this file does not cover

- **Broker-mediated rehydration** — not built. Evicted pieces come back by hand
  (`--restore-from`), and the droplet cannot reach the archive at all.
- **The desktop apps** (`analyzer/`, `memorization/`, `tutor/`) — their own READMEs.
- **A second archive node**, backup of the piece volume, and rotation of the Garage keys — none
  of it exists yet.
