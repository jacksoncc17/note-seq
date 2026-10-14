# Two small patches for the broker's jobs dashboard

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
