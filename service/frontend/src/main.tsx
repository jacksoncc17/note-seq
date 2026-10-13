import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'

import { router } from './routes/__root'
import './index.css'

// Jobs are polled while they run, so the cache is short-lived on purpose: a page that
// showed a job stuck at 40% for thirty seconds after it finished would be worse than no
// cache at all.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 1_000, refetchOnWindowFocus: true, retry: 1 },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
