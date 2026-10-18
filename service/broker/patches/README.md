# Patches and operations for the broker: five patches, two procedures

The job dashboard shows an artifact as `kind · size MB` and always downloads it. Two
things are worth changing there for a service whose result is *somewhere to go*:

* **(a) an artifact can be a link, not only a download.** A `text/uri-list` artifact (the
  standard mime for a list of URIs; the extension that produces it is `.uri`) is fetched
  read-only and rendered as a link, because downloading a text file to read a URL out of
  it is a poor way to offer one.
* **(b) the progress log is rendered.** `JobEvent`'s own docstring calls it *"append-only
  progress log, which is what drives the dashboard timeline"* — the endpoint and the
  generated client call were both there and no page used them. A worker's progress
  messages are usually where the interesting sentences are ("20 bars had to be clamped",
  "the page is at http://…"), so URLs in them become links.

Neither needs a change to the broker, to the database, or to any worker: (a) works on
artifacts that are already uploaded, and (b) reads an endpoint that already exists.

## Apply

From the reference checkout (`…/selfCommentary/full-stack-fastapi-template`):

```bash
cp /home/wei/music_gen/note-seq/service/broker/patches/jobs.tsx \
   frontend/src/routes/_layout/jobs.tsx

# and, separately, a pre-existing type error that stops `tsc` (see below):
cp /home/wei/music_gen/note-seq/service/broker/patches/ServiceForm.tsx \
   frontend/src/components/Jobs/ServiceForm.tsx
```

`jobs.tsx.patch` is the same first change as a unified diff, for review before applying.
`verify/` is a copy of their frontend with the patch applied, used to typecheck it against
their real types and to run it; it is scratch, not something to deploy.

A running Vite dev server picks both changes up on reload — no restart, no rebuild.

## (a) How a link is recognised

```tsx
const isUriList =
  artifact.mime_type === "text/uri-list" ||
  (typeof artifact.kind === "string" && artifact.kind.endsWith("_url"))
```

The mime is the honest signal: it is a property of the artifact, so the rule needs no
knowledge of which service produced it, and any future service gets the same treatment by
writing a `.uri` file. The kind check is the fallback for a worker whose `mimetypes`
database does not know the extension (an older Python would send
`application/octet-stream`).

The fetch goes through the same authenticated client as the download — a bare `<a href>`
to the artifact endpoint returns `{"detail":"Not authenticated"}`, which is why the
existing download is a fetch-and-blob rather than a link. Here the same fetch is read as
text, and the first non-comment line is taken as the URI. If anything about that fails,
the row falls back to the download button it had before, so a bad artifact is still a
file.

## (b) The timeline

`readJobEvents` is fetched per row **only when the row is expanded** — polling the queue
must not multiply by the number of rows. Messages are rendered with their URLs
linkified, since that is where a worker says where the result went.

## The pre-existing type error

`frontend/src/components/Jobs/ServiceForm.tsx:142` reads `prop.accept`, but the `Prop`
type above it does not declare `accept`. With their own tsconfig:

```
$ npx tsc -p tsconfig.build.json --noEmit
src/components/Jobs/ServiceForm.tsx(142,24): error TS2339: Property 'accept' does not exist on type 'Prop'.
```

That is with their untouched file too, so it predates this patch — but their build script
is `tsc -p tsconfig.build.json && vite build`, so `bun run build` fails on it. The `accept`
key is real (the broker's schema validator supports `accept` as a rendering hint, and
`note_seq_align` uses it to offer `.musicxml,.xml,.mxl` in the file picker), so the fix is
to declare it:

```ts
type Prop = {
  …
  format?: string
  accept?: string
}
```

`ServiceForm.tsx` in this directory is their file with that one line added.

## What was verified

* **Typecheck.** `tsc -p tsconfig.build.json --noEmit` over their whole tree with both
  patches: **exit 0, clean**. With their original files it fails on the `accept` error
  above, so the second patch is what makes their build pass again.
* **Both features in a browser, against their live broker.** A copy of their frontend with
  the patches applied, served by Vite, driven by Chrome with a superuser token:
  `page_url · open -> http://localhost:8100/pieces/serenade3`, and that link answered
  `200 text/html` when followed; all 27 job rows had a progress-log toggle, and the one
  opened showed 87 timeline lines ending in a linkified `http://localhost:8100/pieces/…`.
  The exact output, and how to reproduce the harness, is in `verify/README.md`.
* **The mime convention.** `mimetypes.guess_type('open-the-page.uri')` is `text/uri-list`,
  which is what their worker sends (it guesses the mime from the filename), and the
  service's tests assert the file is written in uri-list form — the URI first, comments
  after it.

One thing that came out of the browser check and changed the patch: the *first* version
took the first non-comment line as the URI, which found nothing in the artifacts already
uploaded — the older ones are a plain `.txt` that says a sentence before the URL. It now
takes the first URL anywhere in the text, so old and new artifacts both render as links.
(`verify/README.md` records the job where that showed up: `page_url(text/plain)`.)

Two wrinkles worth knowing if you reproduce the harness, both in that README: this
sandbox cannot run `@vitejs/plugin-react-swc` at all (its native binding refuses every
cache root), and their list route's `307` to a trailing slash turns into a cross-origin
redirect — and loses the `Authorization` header — if a dev proxy rewrites the Host.


---

# (c) Whose job is it? — `owner-through-the-worker.patch`

A third patch, and the only one that touches the backend. Everything a service publishes
for a *person* needs to know which person asked, and at the moment nothing downstream of
the broker does:

```
broker job row   owner_id        <- it knows, and its own job list is scoped by it
JobClaimed       (nothing)       <- the worker is never told
plugin params    (nothing)       <- so the plugin cannot tell the service
piece meta.json  (nothing)       <- so a page shows every piece to everybody
```

The fix is three lines in three files, all in the direction the data already flows:

* `backend/app/models_jobs.py` — `JobClaimed` gains `owner_id`;
* `backend/app/api/routes/worker.py` — `_claimed()` fills it from `job.owner_id`;
* `worker.py` — the params handed to a plugin gain `owner_id`, next to the `_extra_args`
  key that is already injected there.

The plugin on this side reads `params["owner_id"]`, writes it into `meta.json`, and the
piece carries its owner from then on — into the package that is uploaded to the serving
deployment, and into every read that decides what to show. Nothing else in the broker
changes: no migration, no route, no model. A worker that has not been restarted after the
patch simply keeps sending pieces without an owner, which the page then shows to operators
only (`SERVICE_ALLOWED_SUBJECTS`), so the two halves can be rolled out in either order.

## Apply

```bash
cd ~/full-stack-fastapi-template
git apply --check /path/to/note-seq/service/broker/patches/owner-through-the-worker.patch
git apply         /path/to/note-seq/service/broker/patches/owner-through-the-worker.patch
# then restart the backend and the worker: the plugin's params come from the worker, and
# the worker's owner comes from the backend's claim response
```

Then, on the serving deployment, list yourself as an operator so the pieces that predate
this are still visible to you:

```
SERVICE_ALLOWED_SUBJECTS=<your user id from GET /api/v1/users/me>
```

and claim the ones already published:

```bash
python -m service.app.manage --adopt-unowned --owner <your user id>
```

Verified here: the patch applies cleanly to the current checkout (`git apply --check`),
the three patched files compile, and the half that lives on this side is covered by
`test_the_piece_belongs_to_whoever_asked_for_the_job`, which runs a real broker job through
the real plugin and then asks the page's API as two different accounts.


---

# (d) Rebranding the site: `rebrand-museservices.patch`

Not a functional patch — it renames the front end from the template's `FastAPI` to
`MuseServices`, and how much of it needs a rebuild depends on which layer you touch.

**What the patch changes** (12 files, applies cleanly to the current checkout):

* `frontend/index.html` — the tab title, which is what a browser shows before the app boots;
* `frontend/src/routes/**` — the eight per-route titles (`"Dashboard - FastAPI Template"` →
  `"Dashboard - MuseServices"`);
* `frontend/src/components/Common/Footer.tsx` — the footer line;
* `frontend/src/components/Common/Logo.tsx` — **the one real change**: the wordmark is text
  now, not the drawn `fastapi-logo*.svg`. A name that is a picture has to be redrawn for
  every size and both themes; as text it is one string, it inherits the theme colour and the
  page's type size, and a screen reader reads it correctly. The *icon* is still an asset —
  replace `frontend/public/assets/images/fastapi-icon.svg` (and `-light`) with your own mark
  and no code changes;
* `deploy/env.production.example` — `PROJECT_NAME="MuseServices"`, which is what the backend
  uses for its OpenAPI title, `/docs`, and outbound email subjects.

**The name also lives in `.env`, which the patch deliberately does not touch.** That file is
tracked in git *and* edited on the server, so patching it would make the droplet's
`git pull --ff-only` fail — the exact failure `deploy.sh` refuses to deploy through. Set it
on the droplet instead:

```bash
# on the droplet, in the broker repo
sed -i 's/^PROJECT_NAME=.*/PROJECT_NAME="MuseServices"/' .env
```

**What needs which:**

| change | how it takes effect |
| --- | --- |
| `PROJECT_NAME` in `.env` | restart the backend (no rebuild) |
| anything under `frontend/src`, `index.html` | **rebuild** — the page is baked into the backend image |

So the practical order is: apply the patch → push → `./deploy/deploy.sh` (with the build) →
edit `.env` on the droplet → `./deploy/deploy.sh --no-build`. Or edit `.env` first and let
one deploy do both.

---

# Unregistering a service from the site (e.g. `sgf_video`)

There is no unregister endpoint, and it is worth knowing why: a service is a **contract a
worker publishes about itself** at startup (`POST /worker/services` → `upsert_service`), the
`service` table is that contract, and `GET /api/v1/jobs/services` — the list the submit form
renders — returns every row in it. Nothing removes a row when a worker stops, on purpose: the
broker must be able to validate a job that references a service whose worker is asleep.

So removing one is three steps, in this order:

```bash
# 1. stop the worker that runs it, or it re-publishes on its next start
#    (it publishes at startup only, so an already-running idle worker will not re-add it)

# 2. drop the worker's binding, then the contract. The order matters: workerservice.service
#    is a foreign key to service.name with no cascade, so the row cannot go first.
docker compose -f compose.yml -f compose.deploy.yml exec -T db \
  psql -U postgres -d app -c "DELETE FROM workerservice WHERE service = 'sgf_video'; \
                              DELETE FROM service WHERE name = 'sgf_video';"

# 3. check it is gone
curl -s https://${DOMAIN}/api/v1/jobs/services -H "Authorization: Bearer $TOKEN" \
  | python3 -c 'import json,sys; print([s["name"] for s in json.load(sys.stdin)["data"]])'
```

What survives, and should: **job history**. `job.service` is a plain string with no foreign
key, so yesterday's jobs stay listed with their artifacts downloadable — unregistering a
contract does not erase what it produced. What does *not* survive: the submit form no longer
offers it, and a new submission naming it is refused, because the contract it would be
validated against is gone.

**If you only want it to stop working**, deactivate the worker instead — that is reversible
and leaves the contract in place:

```bash
curl -s -X POST "https://${DOMAIN}/api/v1/admin/workers/${WORKER_ID}/active?active=false" \
  -H "Authorization: Bearer $TOKEN"
```

It stops claiming new jobs (the job it holds, if any, still has to finish: the lease is what
prevents a second worker racing it) but the service stays on the form as a service nothing
will run. Removing the row is the way to take it off the form.


---

# (e) A hint on hover, in the service picker: `service-description-on-hover.patch`

One attribute, because the interesting part is that **the data was already flowing** and only
one place failed to show it:

```
plugin docstring, first line   (services/*/service.py, worker_plugins/*.py)
  → worker.py: description=_first_line(plugin.__doc__)          at startup
  → POST /worker/services → upsert_service(description=…)
  → GET  /jobs/services  → ServicePublic.description            (already in the client type)
  → submit.tsx: shown under the picker for the *selected* service
  → <-- and nowhere on the items, which is what this patches
```

So no backend change, no schema change, no new field: `submit.tsx` puts the description on
each `<SelectItem>` as a native `title`, which is a hover hint in every browser with no
component and no JS. It goes on the item rather than in its text because Radix mirrors an
item's text into the closed trigger — a second line would then also appear in the box after
you pick the service.

**Where the text comes from is worth knowing when writing a plugin:** the description is the
*first line of the module docstring*, so that line is user-facing copy, not a note to a
maintainer. Two existing services currently publish a line that is a wrapped paragraph's
first line and therefore ends mid-sentence:

* `services/sgf_video/service.py` → *"The SGF video render service -- the pipeline this
  system was built for, expressed as"*
* `services/intonationCheck/service.py` → *"intonationCheck -- compare two recordings of the
  same part against the score."* (this one is fine)

Rewriting the first line of such a docstring is the whole fix — no code change, and it takes
effect the next time that worker starts, because that is when it publishes.

`note_seq_align` was rewritten the same way for the same reason: it used to describe the
plugin's construction ("The note-seq broker plugin: one job, a few artifacts, …") and now
says what choosing it does — *"Align a score with a recording of it: bar timings, a clip of
any passage, a clickable page."*


---

# (f) An optional field left empty is a 422: `optional-fields-may-be-empty.patch`

Reported as "why do I have to fill in first bar and last bar when the page lets me skip the
range?" — and the fault is in the form, not in the service contract:

```
ServiceForm.tsx  initial(): no default → ""          (for every non-checkbox, non-file control)
ServiceForm.tsx  submit():  { ...current }           → sends first_bar: ""
broker           param_schema._type_ok(): "" is not an int
                 → 422 "first_bar: expected integer"
```

Nothing about the service requires them: `param_schema` lists only `score` in `required`, and
`note_seq_align._range()` already treats an absent or empty pair as "no passage" — *"give
both first_bar and last_bar, or neither"*. The defect is that an untouched field is sent as an
empty string, and only numerics notice: an empty string is a valid `string`, so optional text
fields never showed the problem.

The patch drops empty values for fields that are not required, just before submitting. That
is deliberately general: it fixes every optional numeric field of every service, rather than
one schema learning to spell "I am an integer, unless I am empty". Required fields keep their
empty value, so the broker still names the one that is missing.

Until it is deployed, the workaround on the form is to fill both numbers (which produces a
clip — there is no way to say "no passage" from the form yet), or to omit them entirely:

```bash
curl -s -X POST https://${DOMAIN}/api/v1/jobs/generic \
  -H "Authorization: Bearer $TOKEN" -F service=note_seq_align \
  -F 'params={"title":"from curl"}' -F file_field=score -F files=@score.mxl
```

Verified: applies cleanly to the current checkout, and both the original and patched files
parse as TSX (0 syntax errors) with the TypeScript compiler from the note-seq frontend, since
their checkout has no `tsc` on disk to typecheck with. `required` is declared at line 234,
`submit` at 245, so the reference resolves.


---

# Checking whether a frontend change is deployed

Told the hard way: after a redeploy a fix appeared to have had no effect, and the first check
agreed with that — because it grepped the entry bundle, and the router **code-splits routes**,
so the form's code lives in a per-route chunk (`submit-<hash>.js`) that the page loads on
navigation. The rebrand next to it was clearly live; the fix was simply in a file nobody had
looked at.

```bash
./verify/check_deployed_bundle.sh https://www.jinshiming.net 'title:e.description'
#   chunks 44 mentioned by the entry bundle
#     submit-B1X313cX.js   6886 bytes  title:e.description:1
#   1 chunk(s) matched: the change is deployed
```

Look for **minified** text, not source text: comments are stripped, names shortened, and
whitespace removed, so `title={s.description ?? undefined}` is `title:e.description??void 0`
in the bundle. The chunk's hash changes on every build, which is why the script discovers the
names from the entry bundle instead of taking one as an argument.

Worked example, from the deployment this was written for: the rebrand (`<title>MuseServices`,
and `MuseServices` in the entry bundle) and the hover hint (`title:e.description`) were
deployed; `optional-fields-may-be-empty.patch` was not — the submit chunk contained **one**
`delete` (the file-field one) rather than the two the fix adds, which is exactly why leaving
first/last bar empty still answered with a 422.
