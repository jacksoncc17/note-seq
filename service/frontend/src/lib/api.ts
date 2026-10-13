/** The API, typed. One place that knows the service's URLs and its JSON shapes. */

export type JobStatus = 'queued' | 'running' | 'done' | 'failed'

export type Job = {
  id: string
  kind: string
  status: JobStatus
  progress: number
  message: string
  title: string
  slug: string | null
  error: string | null
  attempts: number
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  seconds: number | null
  params: Record<string, unknown>
  log?: string
}

export type MeasureTime = {
  measure: number
  play_index: number
  start_s: number
  end_s: number
  confidence: number | null
}

export type MeasureBox = {
  number: number
  x0: number
  y0: number
  x1: number
  y1: number
  fx0?: number
  fy0?: number
  fx1?: number
  fy1?: number
}

export type PageGeometry = {
  number: number
  width: number
  height: number
  measures: MeasureBox[]
}

export type PieceMeta = {
  slug: string
  title: string
  composer: string
  bars: number
  duration_s: number
  method: string
  seconds_per_measure: number
  pages: number
  created_at: number
  score: { name?: string; title?: string; composer?: string; sha256?: string }
  audio: { name?: string; source?: string; url?: string; duration_s?: number }
  quality: Record<string, unknown>
  warnings: string[]
}

export type PieceMap = {
  version: number
  method: string
  seconds_per_measure: number
  audio_duration_s: number
  offset_s: number
  measures: MeasureTime[]
  quality: Record<string, unknown>
  warnings: string[]
}

export type Piece = {
  meta: PieceMeta
  map: PieceMap
  geometry: { pages: PageGeometry[]; warnings: string[]; slug: string }
  audio: { url: string; duration_s: number }
}

export type PieceSummary = {
  slug: string
  title: string
  composer: string
  bars: number
  duration_s: number
  created_at: number
  pages: number
  warnings: number
}

export type ServiceStatus = {
  service: string
  version: string
  jobs: Record<JobStatus, number>
  pieces: number
  data_dir: string
  database: string
  frontend_built: boolean
  capabilities: Record<string, boolean>
}

const API = '/api/v1'

class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(API + path, init)
  if (!response.ok) {
    // FastAPI puts the reason in `detail`; anything else is the status line.
    let detail = response.statusText
    try {
      const body = await response.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* not JSON: the status line is all there is */
    }
    throw new ApiError(response.status, detail)
  }
  return (await response.json()) as T
}

export const getStatus = () => request<ServiceStatus>('/status')

export const listJobs = (limit = 30) =>
  request<{ jobs: Job[]; stats: Record<JobStatus, number> }>(`/jobs?limit=${limit}`)

export const getJob = (id: string) => request<Job>(`/jobs/${id}`)

export const retryJob = (id: string) => request<Job>(`/jobs/${id}/retry`, { method: 'POST' })

export const listPieces = () => request<{ pieces: PieceSummary[] }>('/pieces')

export const getPiece = (slug: string) => request<Piece>(`/pieces/${slug}`)

export type Upload = {
  score: File
  audio: File | null
  youtubeUrl: string
  title: string
}

/** Upload a score and a recording (or a URL) as multipart, and queue the analysis. */
export function createJob(upload: Upload): Promise<Job> {
  const form = new FormData()
  form.append('score', upload.score)
  if (upload.audio) form.append('audio', upload.audio)
  form.append('youtube_url', upload.youtubeUrl)
  form.append('title', upload.title)
  return request<Job>('/jobs', { method: 'POST', body: form })
}

export const pageUrl = (slug: string, number: number) =>
  `${API}/pieces/${slug}/pages/${number}.svg`

/** The clip for a range of bars: the URL is the request, and it is cacheable. */
export const clipUrlForBars = (slug: string, first: number, last: number) =>
  `${API}/pieces/${slug}/audio?first_bar=${first}&last_bar=${last}`

export const audioUrl = (slug: string) => `${API}/pieces/${slug}/audio`

export { ApiError }
