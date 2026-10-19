# The worker, on a machine that is not the laptop

`install.sh` turns a bare Linux machine into a note-seq worker: the interpreter, the system
tools the analysis shells out to, the broker's `worker.py`, this machine's `worker.env`, and a
systemd user unit that starts it at boot and restarts it when it dies.

```
./service/deploy/worker/install.sh --check     # what is present, what is not; changes nothing
./service/deploy/worker/install.sh             # install whatever is missing
./service/deploy/worker/install.sh --help      # every flag
```

It is deliberately not a package installer: the worker runs `service/broker/run_worker.sh` from
a **clone** of this repository, because the plugin it loads calls the analyzer in that tree. Run
it from the checkout it is going to run — `git clone --branch desktop <url>` first, then this.

## The deployment this was written for

The archive VM (`vm2`, 192.168.1.11): Ubuntu 22.04, **one vCPU**, 15 GB RAM, Garage on the same
machine holding the archive. That last part is the interesting one — the archive is now a
loopback hop from the worker rather than a LAN round trip, so `SERVICE_ARCHIVE_URL` is
`http://127.0.0.1:3900`.

One vCPU is not a compromise, it is the whole point of the measurement: the analysis is
single-threaded end to end, so the same 204-bar piece takes **6.4 s pinned to one core and 6.7 s
on sixteen** of a Ryzen 7 3700X. On this VM's Xeon E3-1225 v3 it takes **10.0 s** steady state
(the first job after a fresh venv takes ~20 s, because numba compiles its kernels once per venv).
Throughput comes from more workers, not from more cores.

## What each step costs

| step | what | cost |
|---|---|---|
| 1 | `ffmpeg`, `fluidsynth` + soundfont, `libcairo2`, `python3-venv` | seconds, needs sudo |
| 2 | Python 3.12 via `uv`, then `requirements.txt` | ~650 MB on disk, a few minutes, **no root** |
| 3 | broker's `worker.py` + `worker_plugins/` | one copy |
| 4 | `service/broker/worker.env` | secrets, `0600`, never committed |
| 5 | `~/.config/systemd/user/note-seq-worker.service` | enabled and started only if the key is real |

**Why Python 3.12 and `uv`:** Ubuntu 22.04's system Python is 3.10, where pip stops at librosa
0.11.0. The alignment's numbers were measured with librosa 1.0.0, and a different librosa is a
different map. `uv` fetches a standalone 3.12 without root, so the system interpreter is left
alone. `requirements.txt` beside this README pins the whole set, including the packages that are
only there because `import note_seq` pulls in its notebook helpers (`bokeh`, `IPython`, `pandas`).

**Why the key is not a CLI argument:** everything secret lives in `worker.env`, which
`run_worker.sh` sources itself. It never appears in the unit, in `argv`, in `ps`, or in shell
history. The installer writes the file only when it is absent — rewriting it silently would
rotate a working key.

## The two things the installer cannot do for you

1. **Register the worker.** That is a curl against the broker's admin API and it returns the key
   exactly once (`POST /api/v1/admin/workers/ {"name": "..."}` with a superuser token). Give each
   machine its own name; re-registering an existing name rotates that machine's key.
2. **Give the archive a bucket.** `garage/native/bootstrap.sh` does it, on the archive machine.

## After it runs

```bash
systemctl --user status note-seq-worker            # active, and the banner shows the settings
journalctl --user -u note-seq-worker -f            # job progress
./service/broker/run_worker.sh --once              # claim one job and exit -- the smoke test
```

`--once` prints the same banner as the unit and then either runs the job in front of it or says
`queue empty`. That banner is the honest check: `key set`, `token set`, and the `publishing`
line naming the page the piece will be sent to. A wrong key stops at registration with a 401
instead, and the unit would be restart-looping rather than idling.

## The soundfont, which is not a detail

The alignment renders the reference MIDI with FluidSynth and compares it to the recording, so
the soundfont is part of the measurement. `analyzer/analyzer/align.py` prefers
`~/soundfonts/Sonatina_Symphonic_Orchestra.sf2` and falls back to the system `FluidR3_GM.sf2`.

Measured on the Chopin waltz, same audio, same score, same machine: Sonatina gives bars 1–18 as
**30.53 s**, FluidR3_GM gives **30.02 s**, and the raw bar-length range moves from 0.09–7.24 s to
0.28–7.06 s. Both maps are plausible; they are simply not the same map. So if maps need to agree
between machines, the same `.sf2` has to be at the same path on both — copy the file, the
installer only reports which one it found.

## On the machine that already runs the archive

```
sudo apt-get install -y python3-venv ffmpeg fluidsynth fluid-soundfont-gm libcairo2
git clone --branch desktop ssh://git@<gitlab>/wei/note-seq-desktop.git ~/note-seq-desktop
cd ~/note-seq-desktop && ./service/deploy/worker/install.sh
```

and then the two steps above: the curl for the key, and `garage/native/bootstrap.sh` for the
bucket if this is the first worker that archives. Running `./service/deploy/worker/install.sh`
again after any of that is a no-op — every step is skipped by what it finds.
