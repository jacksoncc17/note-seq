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
  /** Pieces analysed here that the page serving them has not accepted yet. Zero on a
   *  serving deployment; on the machine that analyses, this is the number to watch. */
  outbox?: number
  data_dir: string
  database: string
  frontend_built: boolean
  capabilities: Record<string, boolean>
  /** How this deployment is reached: `open`, or `token` when it shares the broker's
   *  login. `login_url` is where to send a browser that has no token. `owner_filter`
   *  means the pieces you see are the ones you own; `operators` counts the accounts that
   *  see every piece, including ones with no owner. */
  auth?: {
    mode: 'open' | 'token'
    login_url: string | null
    owner_filter?: boolean
    operators?: number
  }
  /** True when this deployment only serves finished pieces: no form, no queue. */
  read_only?: boolean
}

// `/api/v1` when this service is the whole origin; under a path prefix it is that
// prefix plus the same path, because the proxy strips the prefix on the way through.
const API = (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api/v1'

/**
 * The broker's access token, read from the key its own frontend uses
 * (`hooks/useAuth.ts`: `localStorage.access_token`).
 *
 * This only works because the page is served on the *broker's* origin: localStorage is
 * per-origin, so a page on a subdomain sees nothing. Serving here under a path prefix is
 * what makes "sign in to the broker" also mean "signed in to the scores", with no second
 * password anywhere.
 */
export const TOKEN_KEY = 'access_token'

export function token(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

let signInUrl: string | null = null

/** Told by the status call: where to send a browser whose token is missing or stale. */
export function rememberSignIn(url: string | null | undefined) {
  signInUrl = url ?? null
}

export function goToSignIn(): boolean {
  if (!signInUrl) return false
  window.location.assign(signInUrl)
  return true
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const value = token()
  return value ? { ...extra, Authorization: `Bearer ${value}` } : extra
}

class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(API + path, {
    ...init,
    headers: authHeaders((init?.headers as Record<string, string>) ?? {}),
  })
  if (response.status === 401 || response.status === 403) {
    // the token is missing, expired (the broker's last 8 days) or belongs to an account
    // this service does not allow -- signing in again is the only useful next step
    if (goToSignIn()) throw new ApiError(response.status, 'signing in')
  }
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

/**
 * A fetch that carries the broker's token, for the calls that are not JSON.
 *
 * The page's SVG and the audio clip are fetched directly rather than through `request`,
 * so they need this: without it they were the only two requests on the page that went out
 * unauthenticated, and under a deployment that shares the broker's login they came back
 * 401 while everything else worked.
 */
export async function authorisedFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(url, {
    ...init,
    headers: authHeaders((init.headers as Record<string, string>) ?? {}),
  })
  if (response.status === 401 || response.status === 403) {
    if (goToSignIn()) throw new ApiError(response.status, 'signing in')
  }
  return response
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
