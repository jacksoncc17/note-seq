import { Link, Outlet, createRootRoute, createRoute, createRouter } from '@tanstack/react-router'
import { useEffect } from 'react'

import { JobList, PieceList, SubmitForm } from '../components/Analyse'
import { Card, CardBody } from '../components/ui/card'
import { ScoreScreen } from '../components/ScoreScreen'
import { goToSignIn, rememberSignIn, token } from '../lib/api'
import { useStatus } from '../lib/hooks'

/** The shell: a title, a way home, and what this service can do on this machine. */
function Layout() {
  const status = useStatus()
  const capabilities = status.data?.capabilities
  const auth = status.data?.auth

  // Under the broker's origin the page is meant to be reached with the broker's token
  // already in hand. If there is none, the useful thing is to go and sign in rather than
  // to show three panels full of failed requests.
  useEffect(() => {
    if (!status.data) return
    rememberSignIn(auth?.login_url)
    if (auth?.mode === 'token' && !token()) goToSignIn()
  }, [status.data, auth?.mode, auth?.login_url])
  // A serving-only deployment has neither the analyzer nor ffmpeg nor yt-dlp and is not
  // missing anything by that: it reads pieces the worker on the LAN analysed. Warning
  // about them there would be a warning about the deployment being what it is.
  const readOnly = Boolean(status.data?.read_only)
  const missing =
    capabilities && !readOnly
      ? Object.entries(capabilities)
          .filter(([, okay]) => !okay)
          .map(([name]) => name)
      : []

  return (
    <div className="min-h-full">
      <header className="border-b border-stone-200 bg-white">
        <div className="mx-auto flex max-w-[1500px] items-center gap-4 px-4 py-3 lg:px-6">
          <Link to="/" className="flex items-baseline gap-2">
            <span className="text-sm font-semibold tracking-tight text-stone-900">note-seq</span>
            <span className="text-xs text-stone-500">score and recording, in the browser</span>
          </Link>
          <span className="flex-1" />
          {status.data && (
            <span className="text-xs text-stone-400" title={status.data.data_dir}>
              {status.data.pieces} piece{status.data.pieces === 1 ? '' : 's'} ·{' '}
              {status.data.jobs.done} analysed
              {/* A piece that has not reached the page serving it. Shown only when there
                  is something waiting, because on a serving deployment it never is. */}
              {Boolean(status.data.outbox) && (
                <span className="text-amber-700">
                  {' '}
                  · {status.data.outbox} waiting to publish
                </span>
              )}
            </span>
          )}
        </div>
      </header>

      {missing.length > 0 && (
        <div className="bg-amber-50 px-4 py-2 text-xs text-amber-900 lg:px-6">
          Missing on this machine: {missing.join(', ')} — part of the pipeline is unavailable.
          See service/README.md.
        </div>
      )}

      <main>
        <Outlet />
      </main>
    </div>
  )
}

function Home() {
  const status = useStatus()
  const readOnly = Boolean(status.data?.read_only)

  // A serving-only deployment has no worker attached: a form here would queue an
  // analysis nothing ever runs, and the queue it fills belongs to the broker. So it
  // shows the pieces and says where analyses are submitted.
  if (readOnly) {
    return (
      <div className="mx-auto max-w-[1500px] space-y-6 p-4 lg:p-6">
        <Card>
          <CardBody className="space-y-1 text-sm text-stone-600">
            <p className="font-medium text-stone-900">Scores ready to read</p>
            <p>
              This deployment serves finished pieces only. Analyses are submitted on the
              broker — every job that finishes there is published here and appears below.
            </p>
          </CardBody>
        </Card>
        <PieceList />
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-[1500px] space-y-6 p-4 lg:p-6">
      <SubmitForm capabilities={status.data?.capabilities} />
      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-stone-900">Queue</h2>
        <JobList />
      </section>
      <PieceList />
    </div>
  )
}

const rootRoute = createRootRoute({ component: Layout })

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  component: Home,
})

// Code-based rather than file-based: two routes do not need a code generator in the
// build, and this way `tsc` alone is enough to know the routes are typed correctly.
const pieceRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/pieces/$slug',
  component: function PieceRoute() {
    const { slug } = pieceRoute.useParams()
    return <ScoreScreen slug={slug} />
  },
})

const routeTree = rootRoute.addChildren([indexRoute, pieceRoute])

// `BASE_URL` is '/' at the root and '/note-seq/' under a path prefix; the router wants
// it without the trailing slash.
const basepath = import.meta.env.BASE_URL.replace(/\/$/, '') || '/'

export const router = createRouter({
  routeTree,
  basepath,
  defaultPreload: 'intent',
})

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router
  }
}
