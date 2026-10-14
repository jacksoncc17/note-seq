# The note-seq page, as a container.
#
# Build context is this directory (`service/`), not the repository: the serving half needs
# fastapi, uvicorn, python-multipart, soundfile, numpy and pyjwt and nothing else -- no
# analyzer, no Verovio, no librosa, no FluidSynth, no soundfont, no ffmpeg, and therefore
# none of the repository's other directories (including the 100 MB `cyolo_score_following`
# clone and the 236 MB analyzer cache) are ever uploaded to the builder. That was measured
# rather than assumed: `create_app()` imports none of that, and
# `test_serving_a_piece_needs_no_analyzer` makes an import of `analyzer` fail and then asks
# the API for a piece, a page and a clip.
#
#   docker build -t note-seq:latest service/
#   docker run --rm -p 8100:8000 -v note-seq-data:/data note-seq:latest
#
# For a deployment under the broker's own origin (a path prefix on the broker's host),
# build with the prefix baked in:
#
#   docker build -t note-seq:latest \
#     --build-arg VITE_BASE=/note-seq/ \
#     --build-arg VITE_API_BASE=/note-seq/api/v1 \
#     service/

# --------------------------------------------------------------------------- the page
FROM node:22-alpine AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# Where the page will be served from. The default is the root of its own origin; a
# deployment behind the broker's login passes `/note-seq/` and `/note-seq/api/v1`, which
# is what lets it share that origin's token instead of needing a password of its own.
ARG VITE_BASE=/
ARG VITE_API_BASE=/api/v1
ENV VITE_BASE=${VITE_BASE} VITE_API_BASE=${VITE_API_BASE}
RUN npm run build

# --------------------------------------------------------------------------- the server
FROM python:3.12-slim
# libsndfile is what soundfile reads the recordings with. It is the only system library
# the serving half needs -- the audio is sliced from the piece's own WAV, so no ffmpeg.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

# `python -m service.app` wants `service` importable, so the layout matches the repository:
# /srv/service/app, run from /srv.
WORKDIR /srv
COPY requirements.txt ./service/requirements.txt
RUN pip install --no-cache-dir -r ./service/requirements.txt
COPY app/ ./service/app/
COPY --from=frontend /app/dist ./service/frontend/dist

# Serving only: the analysis runs on the worker, on the LAN, with the analyzer and its
# soundfont. A worker in here would claim jobs and fail every one of them.
ENV SERVICE_EMBEDDED_WORKER=0 \
    SERVICE_DATA=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000

# python rather than curl: the slim image has no curl, and this keeps it that way.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/status', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "service.app", "--host", "0.0.0.0", "--port", "8000"]
