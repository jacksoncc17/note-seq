import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { ChevronDown, ChevronRight, FileMusic, RotateCw, Upload } from 'lucide-react'
import { useState } from 'react'

import { createJob, type Job } from '../lib/api'
import { useJob, useJobs, usePieces } from '../lib/hooks'
import { cn, clock } from '../lib/utils'
import { Badge } from './ui/badge'
import { Button, button } from './ui/button'
import { Card, CardBody, CardHeader, CardTitle } from './ui/card'
import { Input } from './ui/input'
import { Label } from './ui/label'
import { Progress } from './ui/progress'

/** Upload a score and a recording, and put the analysis in the queue. */
export function SubmitForm({ capabilities }: { capabilities?: Record<string, boolean> }) {
  const client = useQueryClient()
  const [score, setScore] = useState<File | null>(null)
  const [audio, setAudio] = useState<File | null>(null)
  const [youtube, setYoutube] = useState('')
  const [title, setTitle] = useState('')
  const [problem, setProblem] = useState<string | null>(null)

  const submit = useMutation({
    mutationFn: () => createJob({ score: score as File, audio, youtubeUrl: youtube, title }),
    onSuccess: () => {
      setProblem(null)
      setScore(null)
      setAudio(null)
      setYoutube('')
      setTitle('')
      void client.invalidateQueries({ queryKey: ['jobs'] })
    },
    onError: (error: Error) => setProblem(error.message),
  })

  const ready = Boolean(score) && (Boolean(audio) || youtube.trim().length > 0)
  const canFetchYoutube = capabilities?.yt_dlp !== false
  const canReadAudio = capabilities?.ffmpeg !== false

  return (
    <Card>
      <CardHeader>
        <CardTitle>Analyse a piece</CardTitle>
      </CardHeader>
      <CardBody className="space-y-4">
        <p className="text-sm text-stone-600">
          Give the score and a recording of it. The service lines the two up — every bar gets
          a time in the recording — and then serves the engraved score where you can pick any
          range of bars and listen to exactly those.
        </p>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="score">Score (MusicXML)</Label>
            <Input
              id="score"
              type="file"
              accept=".musicxml,.xml,.mxl"
              onChange={(event) => setScore(event.target.files?.[0] ?? null)}
            />
            <p className="text-xs text-stone-500">.musicxml, .xml or .mxl</p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="audio">Recording</Label>
            <Input
              id="audio"
              type="file"
              accept=".mp3,.wav,.flac,.m4a,.aac,.ogg,.opus,.wma,.webm,audio/*"
              disabled={!canReadAudio}
              onChange={(event) => setAudio(event.target.files?.[0] ?? null)}
            />
            <p className="text-xs text-stone-500">
              {canReadAudio ? 'mp3, wav, flac, m4a, ogg' : 'ffmpeg is missing, so audio cannot be read'}
            </p>
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="youtube">…or a YouTube URL</Label>
            <Input
              id="youtube"
              placeholder="https://www.youtube.com/watch?v=…"
              value={youtube}
              disabled={!canFetchYoutube}
              onChange={(event) => setYoutube(event.target.value)}
            />
            <p className="text-xs text-stone-500">
              {canFetchYoutube ? 'the best audio track is downloaded and converted' : 'yt-dlp is not installed'}
            </p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="title">Title (optional)</Label>
            <Input
              id="title"
              placeholder="defaults to the score's own title"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
            />
          </div>
        </div>

        {problem && (
          <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-red-200">
            {problem}
          </p>
        )}

        <div className="flex items-center gap-3">
          <Button
            variant="primary"
            disabled={!ready || submit.isPending}
            onClick={() => submit.mutate()}
          >
            <Upload className="size-4" />
            {submit.isPending ? 'Uploading…' : 'Analyse'}
          </Button>
          {!ready && (
            <span className="text-xs text-stone-500">
              a score plus either a recording or a URL
            </span>
          )}
        </div>
      </CardBody>
    </Card>
  )
}

const statusTone: Record<Job['status'], 'queued' | 'running' | 'done' | 'failed'> = {
  queued: 'queued',
  running: 'running',
  done: 'done',
  failed: 'failed',
}

/** The queue: what has been asked for, and where each request has got to. */
export function JobList() {
  const jobs = useJobs()

  if (jobs.isLoading) return <p className="text-sm text-stone-500">loading the queue…</p>
  if (jobs.isError)
    return <p className="text-sm text-red-700">the queue could not be read: {String(jobs.error)}</p>

  const rows = jobs.data?.jobs ?? []
  if (!rows.length) {
    return (
      <p className="rounded-md border border-dashed border-stone-300 px-4 py-6 text-center text-sm text-stone-500">
        Nothing analysed yet. Upload a score and a recording above.
      </p>
    )
  }

  return (
    <ul className="space-y-2" data-testid="job-list">
      {rows.map((job) => (
        <JobRow key={job.id} job={job} />
      ))}
    </ul>
  )
}

function JobRow({ job }: { job: Job }) {
  const [open, setOpen] = useState(false)
  const detail = useJob(open ? job.id : null)
  const tone = statusTone[job.status]

  return (
    <li
      className="rounded-lg border border-stone-200 bg-white px-4 py-3"
      data-testid="job"
      data-status={job.status}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            {/* A finished job IS a link to its score: that is the whole flow. */}
            {job.status === 'done' && job.slug ? (
              <Link
                to="/pieces/$slug"
                params={{ slug: job.slug }}
                className="truncate text-sm font-medium text-stone-900 underline decoration-stone-300 underline-offset-2 hover:decoration-stone-900"
                data-testid="job-link"
              >
                {job.title || job.slug}
              </Link>
            ) : (
              <span className="truncate text-sm font-medium text-stone-900">
                {job.title || job.slug || job.id}
              </span>
            )}
            <Badge tone={tone}>{job.status}</Badge>
            {job.status === 'done' && job.seconds !== null && (
              <span className="text-xs text-stone-400">in {job.seconds.toFixed(0)} s</span>
            )}
          </div>
          <p className="mt-1 truncate text-xs text-stone-500">{job.message || '—'}</p>
          {job.status === 'running' && <Progress className="mt-2" value={job.progress} />}
          {job.status === 'failed' && job.error && (
            <p className="mt-2 text-xs text-red-700">{job.error}</p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {job.status === 'done' && job.slug && (
            // A link, styled as a button: navigation must stay a link (middle click,
            // open in a new tab), so it is not wrapped in a <button>.
            <Link
              to="/pieces/$slug"
              params={{ slug: job.slug }}
              className={button({ variant: 'secondary', size: 'sm' })}
            >
              Open score
            </Link>
          )}
          {job.status === 'failed' && <RetryButton id={job.id} />}
          <Button size="icon" variant="ghost" title="log" onClick={() => setOpen((was) => !was)}>
            {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
          </Button>
        </div>
      </div>

      {open && (
        <div className="mt-3 space-y-2 border-t border-stone-100 pt-3">
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-stone-500">
            <span>id {job.id}</span>
            <span>attempt {job.attempts}</span>
            {job.started_at && <span>started {new Date(job.started_at).toLocaleTimeString()}</span>}
            {job.params.youtube_url ? <span>youtube</span> : null}
          </div>
          <pre className="max-h-64 overflow-auto rounded-md bg-stone-900/95 p-3 text-[11px] leading-relaxed text-stone-100">
            {detail.data?.log || job.log || 'no log yet'}
          </pre>
        </div>
      )}
    </li>
  )
}

function RetryButton({ id }: { id: string }) {
  const client = useQueryClient()
  const retry = useMutation({
    mutationFn: () => fetch(`/api/v1/jobs/${id}/retry`, { method: 'POST' }).then((r) => r.json()),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['jobs'] })
    },
  })
  return (
    <Button size="sm" variant="secondary" onClick={() => retry.mutate()} title="queue it again">
      <RotateCw className={cn('size-3.5', retry.isPending && 'animate-spin')} />
      Retry
    </Button>
  )
}

/** Pieces that are ready, so a finished analysis is reachable without the queue. */
export function PieceList() {
  const pieces = usePieces()
  const rows = pieces.data?.pieces ?? []
  if (!rows.length) {
    return (
      <p className="rounded-md border border-dashed border-stone-300 px-4 py-6 text-center text-sm text-stone-500">
        No pieces yet. One appears here as soon as a job finishes on the broker.
      </p>
    )
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>Scores ready to read</CardTitle>
      </CardHeader>
      <CardBody className="space-y-1">
        {rows.map((piece) => (
          <Link
            key={piece.slug}
            to="/pieces/$slug"
            params={{ slug: piece.slug }}
            className="flex items-center gap-3 rounded-md px-2 py-2 hover:bg-stone-50"
          >
            <FileMusic className="size-4 shrink-0 text-stone-400" />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium text-stone-900">
                {piece.title}
              </span>
              <span className="block truncate text-xs text-stone-500">
                {[
                  piece.composer,
                  `${piece.bars} bars`,
                  clock(piece.duration_s),
                  `${piece.pages} pages`,
                ]
                  .filter(Boolean)
                  .join(' · ')}
              </span>
            </span>
            {piece.warnings > 0 && (
              <Badge tone="running">{piece.warnings} warning{piece.warnings === 1 ? '' : 's'}</Badge>
            )}
          </Link>
        ))}
      </CardBody>
    </Card>
  )
}
