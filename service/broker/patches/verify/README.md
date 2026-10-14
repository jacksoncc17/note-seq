# The harness the patches were verified with

`check_dashboard.mjs` drives the real dashboard in Chrome against the real broker and
asserts the two things the patches are for: an artifact rendered as a link (and the link
answers), and the progress timeline behind the row's chevron with its URLs linkified.

Reproducing it means running their frontend with the patch applied, which is why this is
a script and not a fixture — the pieces it needs are their repository, their dependencies
and their broker:

```bash
T=/home/wei/playground/goGames/selfCommentary/full-stack-fastapi-template
H=/home/wei/music_gen/note-seq/service/broker/patches

mkdir -p $H/verify/frontend && cd $H/verify/frontend
cp -r $T/frontend/{src,public} $T/frontend/package.json $T/frontend/tsconfig*.json \
      $T/frontend/vite.config.ts $T/frontend/index.html $T/frontend/components.json .
cp $H/jobs.tsx src/routes/_layout/jobs.tsx
cp $H/ServiceForm.tsx src/components/Jobs/ServiceForm.tsx
printf 'VITE_API_URL=\\n' > .env                 # relative URLs: the dev proxy below
npm install                                      # their deps, ~170 packages
npm install --save-dev @vitejs/plugin-react      # see the note below
```

Then two edits to *this copy's* `vite.config.ts` (never theirs), both of which the patch
under test is indifferent to:

* `react()` from `@vitejs/plugin-react` instead of `@vitejs/plugin-react-swc` — the SWC
  native binding refuses to run in this sandbox at all ("cache root has a parent writable
  by another user"), since `/` and `/home` are owned by `nobody` here;
* a `server` block: `port: 5199`, and `proxy: { "/api": { target: "http://127.0.0.1:8000" } }`.
  `changeOrigin` must stay **false**: their list route answers `307` to the trailing slash,
  and a rewritten Host makes that `Location` absolute, which turns it into a cross-origin
  redirect — and a cross-origin redirect drops the `Authorization` header, so the page
  silently gets nothing.

```bash
node_modules/.bin/vite &                          # http://127.0.0.1:5199

export FIRST_SUPERUSER=$(grep -E '^FIRST_SUPERUSER=' $T/.env | cut -d= -f2-)
export FIRST_SUPERUSER_PASSWORD=$(grep -E '^FIRST_SUPERUSER_PASSWORD=' $T/.env | cut -d= -f2-)
cd $H/verify && node check_dashboard.mjs
```

What it printed when both patches were applied:

```
the broker has 20 job(s)
  job 640e100d: page_url(text/plain), range_audio(audio/x-wav)
the page rendered 27 row(s)
(a) artifact links on the page: 1
    page_url · open -> http://localhost:8100/pieces/serenade3
    the linked page answers: 200 text/html; charset=utf-8
(b) rows with a progress-log toggle: 27
    the timeline has 87 line(s); first few:
      10:33:46 PM   job queued   0%
      10:33:47 PM   claimed by lan-note-seq (attempt 1)   0%
    links inside the timeline: 1
      http://localhost:8100/pieces/serenade3
```

Note that job's artifact is the *old* `open-the-page.txt` with `text/plain`: the rule
matches it on the `_url` kind as well as on the mime, which is why the link appears for
artifacts uploaded before the `.uri` rename as well as after it.
