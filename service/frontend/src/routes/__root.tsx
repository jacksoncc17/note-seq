import { Link, Outlet, createRootRoute, createRoute, createRouter } from '@tanstack/react-router'

import { JobList, PieceList, SubmitForm } from '../components/Analyse'
import { ScoreScreen } from '../components/ScoreScreen'
import { useStatus } from '../lib/hooks'

/** The shell: a title, a way home, and what this service can do on this machine. */
function Layout() {
  const status = useStatus()
  const capabilities = status.data?.capabilities
  const missing = capabilities
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

export const router = createRouter({ routeTree, defaultPreload: 'intent' })

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router
  }
}
