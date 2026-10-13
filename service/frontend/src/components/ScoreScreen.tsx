import { ChevronLeft, ChevronRight, Info, TriangleAlert, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  clipUrlForBars,
  type MeasureTime,
  type PageGeometry,
  type Piece,
} from '../lib/api'
import { usePage, usePiece } from '../lib/hooks'
import { cn, clock } from '../lib/utils'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import { Card, CardBody } from './ui/card'

/** The clip the page is playing: where it came from, and where it starts in the map. */
type Clip = {
  url: string
  start: number
  end: number
  first: number
  last: number
}

/** The score page: the engraving, the bars you can pick, and the music of those bars. */
export function ScoreScreen({ slug }: { slug: string }) {
  const piece = usePiece(slug)
  const [page, setPage] = useState<number | null>(null)
  const [first, setFirst] = useState<number | null>(null)
  const [last, setLast] = useState<number | null>(null)
  const [awaiting, setAwaiting] = useState<'start' | 'end'>('start')
  const [clip, setClip] = useState<Clip | null>(null)
  const [loading, setLoading] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const [position, setPosition] = useState(0)
  const audio = useRef<HTMLAudioElement | null>(null)

  const data = piece.data
  const pages = data?.geometry.pages ?? []
  const current = page ?? pages[0]?.number ?? 1
  const svg = usePage(slug, data ? current : null)

  useEffect(() => {
    if (data && page === null && pages.length) setPage(pages[0].number)
  }, [data, page, pages])

  // The clip for the selected bars. Fetched rather than pointed at with an <audio src>,
  // because the response says where the clip starts in the recording (X-Clip-Start) and
  // that number is what puts a bar under the playhead -- reading it beats recomputing it
  // from the range and hoping the two agree.
  useEffect(() => {
    if (first === null || last === null) {
      setClip(null)
      return
    }
    const controller = new AbortController()
    setLoading(true)
    setProblem(null)
    let objectUrl: string | null = null
    fetch(clipUrlForBars(slug, first, last), { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => null)
          throw new Error(body?.detail ?? `the clip could not be cut (${response.status})`)
        }
        const blob = await response.blob()
        objectUrl = URL.createObjectURL(blob)
        setClip({
          url: objectUrl,
          start: Number(response.headers.get('X-Clip-Start') ?? 0),
          end: Number(response.headers.get('X-Clip-End') ?? 0),
          first,
          last,
        })
      })
      .catch((error: Error) => {
        if (error.name !== 'AbortError') setProblem(error.message)
      })
      .finally(() => setLoading(false))
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [slug, first, last])

  const measures = useMemo(() => data?.map.measures ?? [], [data])
  // Which bar is sounding. Only once playback has actually started: a clip sits at
  // currentTime 0 before you press play, and 0 is a real position in the recording --
  // it reported the bar before the passage for as long as nobody had played anything.
  const currentBar = useMemo(
    () => barAt(measures, clip && position > 0 ? clip.start + position : null),
    [measures, clip, position],
  )

  /**
   * A bar was clicked: set the start, then the end, then start again -- the same three
   * taps the phone client uses. `awaiting` alone decides, because after the first click
   * the range is deliberately still open at one end (`last` is null) and testing for
   * that is what made the second click replace the start instead of ending the passage.
   */
  const pick = useCallback(
    (bar: number) => {
      if (awaiting === 'start') {
        setFirst(bar)
        setLast(null)
        setAwaiting('end')
        return
      }
      if (first === null) return
      setFirst(Math.min(first, bar))
      setLast(Math.max(first, bar))
      setAwaiting('start')
    },
    [awaiting, first],
  )

  const clear = () => {
    setFirst(null)
    setLast(null)
    setAwaiting('start')
    setPosition(0)
  }

  if (piece.isLoading) return <p className="p-6 text-sm text-stone-500">loading the piece…</p>
  if (piece.isError)
    return (
      <p className="p-6 text-sm text-red-700">
        This piece could not be loaded: {String(piece.error)}
      </p>
    )
  if (!data) return null

  const rangeStart = first !== null ? startOf(measures, first) : null
  const rangeEnd = last !== null ? endOf(measures, last) : null
  const selection = first !== null && last !== null ? { first, last } : null
  const pageOfCurrent =
    currentBar !== null ? pages.find((p) => p.measures.some((m) => m.number === currentBar)) : null

  return (
    <div className="mx-auto max-w-[1500px] space-y-4 p-4 lg:p-6">
      <Header piece={data} slug={slug} />

      <Card>
        <CardBody className="flex flex-wrap items-center gap-x-4 gap-y-3">
          <div className="flex items-center gap-1">
            <Button
              size="icon"
              variant="secondary"
              title="previous page"
              disabled={current <= pages[0]?.number}
              onClick={() => setPage(previousPage(pages, current))}
            >
              <ChevronLeft className="size-4" />
            </Button>
            <span className="min-w-24 text-center text-xs text-stone-600">
              page {current} / {pages.length}
            </span>
            <Button
              size="icon"
              variant="secondary"
              title="next page"
              disabled={current >= pages[pages.length - 1]?.number}
              onClick={() => setPage(nextPage(pages, current))}
            >
              <ChevronRight className="size-4" />
            </Button>
          </div>

          <div className="flex-1 text-sm text-stone-700" data-testid="range-readout">
            {selection ? (
              <>
                bars <span className="font-medium text-stone-900">{selection.first}</span>
                {selection.last !== selection.first && (
                  <>
                    –<span className="font-medium text-stone-900">{selection.last}</span>
                  </>
                )}
                {rangeStart !== null && rangeEnd !== null && (
                  <span className="text-stone-500">
                    {' '}
                    · {clock(rangeStart)}–{clock(rangeEnd)} · {duration(rangeStart, rangeEnd)} of
                    music
                  </span>
                )}
                {loading && <span className="text-stone-400"> · cutting the clip…</span>}
                {currentBar !== null && (
                  <span className="text-stone-500"> · now bar {currentBar}</span>
                )}
                {pageOfCurrent && pageOfCurrent.number !== current && (
                  <button
                    className="ml-2 text-xs text-stone-500 underline decoration-dotted"
                    onClick={() => setPage(pageOfCurrent.number)}
                  >
                    bar {currentBar} is on page {pageOfCurrent.number}
                  </button>
                )}
              </>
            ) : first !== null ? (
              <>
                start at bar <span className="font-medium text-stone-900">{first}</span> — click
                the last bar of the passage
              </>
            ) : (
              'Click a bar to choose the passage. Click a second bar to end it.'
            )}
          </div>

          <div className="flex items-center gap-2">
            {selection && (
              <Button size="sm" variant="ghost" onClick={clear}>
                <X className="size-3.5" /> Clear
              </Button>
            )}
          </div>
        </CardBody>

        {problem && (
          <div className="border-t border-amber-100 bg-amber-50 px-4 py-2 text-sm text-amber-900">
            {problem}
          </div>
        )}

        <div className="border-t border-stone-100 px-4 py-3">
          {clip ? (
            <div className="flex flex-wrap items-center gap-3">
              <audio
                ref={audio}
                src={clip.url}
                controls
                autoPlay={false}
                className="h-9 w-full max-w-xl"
                data-testid="clip-audio"
                onTimeUpdate={(event) => setPosition(event.currentTarget.currentTime)}
                onEnded={() => setPosition(0)}
              />
              <span className="text-xs text-stone-500" data-testid="clip-info">
                clip {clock(clip.start)}–{clock(clip.end)} of the recording
              </span>
            </div>
          ) : (
            <p className="text-xs text-stone-400">
              The audio for the selected bars appears here.
            </p>
          )}
        </div>
      </Card>

      <div className="score-sheet p-2 sm:p-4">
        {svg.data ? (
          <Page
            svg={svg.data}
            geometry={pages.find((p) => p.number === current)}
            selection={selection}
            currentBar={currentBar}
            onPick={pick}
          />
        ) : (
          <p className="p-6 text-sm text-stone-500">engraving page {current}…</p>
        )}
      </div>

      <Warnings piece={data} />
    </div>
  )
}

/** One engraved page, with a click target over every bar. */
function Page({
  svg,
  geometry,
  selection,
  currentBar,
  onPick,
}: {
  svg: string
  geometry?: PageGeometry
  selection: { first: number; last: number } | null
  currentBar: number | null
  onPick: (bar: number) => void
}) {
  const [hover, setHover] = useState<number | null>(null)
  if (!geometry) return <p className="p-6 text-sm text-stone-500">no geometry for this page</p>
  const boxes = geometry.measures
  const width = geometry.width || 1
  const height = geometry.height || 1

  return (
    <div
      className="score-page"
      style={{ aspectRatio: `${width} / ${height}` }}
      data-testid="score-page"
    >
      {/* The engraving, inlined: the same document the server engraved, so a bar's box
          lands on that bar and the browser can scale it without asking for an image. */}
      <div className="absolute inset-0" dangerouslySetInnerHTML={{ __html: svg }} />

      <div className="absolute inset-0" data-testid="measure-overlay">
        {boxes.map((box) => {
          const inRange =
            selection !== null && box.number >= selection.first && box.number <= selection.last
          const state =
            currentBar === box.number
              ? 'current'
              : inRange
                ? 'selected'
                : hover === box.number
                  ? 'hover'
                  : 'idle'
          return (
            <button
              key={box.number}
              type="button"
              title={`bar ${box.number}`}
              aria-label={`bar ${box.number}`}
              data-testid={`bar-${box.number}`}
              data-state={state}
              onClick={() => onPick(box.number)}
              onMouseEnter={() => setHover(box.number)}
              onMouseLeave={() => setHover((was) => (was === box.number ? null : was))}
              className={cn(
                'absolute cursor-pointer border transition-colors',
                state === 'idle' && 'border-transparent hover:border-stone-400/60 hover:bg-stone-900/5',
                state === 'hover' && 'border-stone-400/60 bg-stone-900/5',
                state === 'selected' && 'border-sky-400/50 bg-sky-400/15',
                state === 'current' && 'border-amber-500 bg-amber-400/25',
              )}
              style={{
                left: `${(box.x0 / width) * 100}%`,
                top: `${(box.y0 / height) * 100}%`,
                width: `${((box.x1 - box.x0) / width) * 100}%`,
                height: `${((box.y1 - box.y0) / height) * 100}%`,
              }}
            />
          )
        })}
      </div>
    </div>
  )
}

function Header({ piece, slug }: { piece: Piece; slug: string }) {
  const meta = piece.meta
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <h1 className="text-xl font-semibold text-stone-900">{meta.title}</h1>
        {meta.composer && <span className="text-sm text-stone-600">{meta.composer}</span>}
        <Badge tone={meta.method === 'dtw' ? 'done' : 'queued'}>{meta.method} map</Badge>
      </div>
      <p className="text-xs text-stone-500">
        {[
          `${meta.bars} bars`,
          `${meta.pages} pages`,
          `${clock(meta.duration_s)} of audio`,
          meta.seconds_per_measure ? `${meta.seconds_per_measure.toFixed(2)} s a bar` : null,
          meta.audio?.source === 'youtube' ? 'from YouTube' : meta.audio?.name,
          `slug ${slug}`,
        ]
          .filter(Boolean)
          .join(' · ')}
      </p>
    </div>
  )
}

function Warnings({ piece }: { piece: Piece }) {
  const warnings = piece.meta.warnings ?? []
  const quality = piece.map.quality ?? {}
  const clamped = Number(quality.bars_clamped ?? 0)
  if (!warnings.length && !clamped) return null
  return (
    <Card>
      <CardBody className="space-y-2 text-sm">
        <p className="flex items-center gap-2 font-medium text-stone-800">
          <TriangleAlert className="size-4 text-amber-600" />
          How well the fit went
        </p>
        {clamped > 0 && (
          <p className="text-stone-600">
            {clamped} bars had to be clamped: the alignment was erratic there, so their times are
            a plausible guess rather than a measurement. They are the bars where the playhead is
            most likely to disagree with what you hear.
          </p>
        )}
        <ul className="space-y-1 text-stone-600">
          {warnings.slice(0, 6).map((warning) => (
            <li key={warning} className="flex gap-2">
              <Info className="mt-0.5 size-3.5 shrink-0 text-stone-400" />
              <span>{warning}</span>
            </li>
          ))}
        </ul>
      </CardBody>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// the map, asked the same questions the desktop app asks it
// ---------------------------------------------------------------------------

/** The bar sounding at `seconds` in the recording, or null when nothing is playing. */
export function barAt(measures: MeasureTime[], seconds: number | null): number | null {
  if (seconds === null || !measures.length) return null
  let found: number | null = null
  for (const measure of measures) {
    if (seconds >= measure.start_s) found = measure.measure
    if (seconds < measure.start_s) break
  }
  return found
}

function startOf(measures: MeasureTime[], bar: number): number | null {
  return measures.find((m) => m.measure === bar)?.start_s ?? null
}

function endOf(measures: MeasureTime[], bar: number): number | null {
  return measures.find((m) => m.measure === bar)?.end_s ?? null
}

function duration(start: number, end: number): string {
  const seconds = Math.max(0, end - start)
  return `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, '0')}`
}

function nextPage(pages: { number: number }[], current: number): number {
  const index = pages.findIndex((p) => p.number === current)
  return pages[Math.min(pages.length - 1, index + 1)]?.number ?? current
}

function previousPage(pages: { number: number }[], current: number): number {
  const index = pages.findIndex((p) => p.number === current)
  return pages[Math.max(0, index - 1)]?.number ?? current
}
