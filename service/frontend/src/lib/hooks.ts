import { useQuery } from '@tanstack/react-query'

import { getJob, getPiece, getStatus, listJobs, listPieces, pageUrl } from './api'

/** Poll the job list while anything is queued or running, and stop when it is not.
 *  A list that keeps polling every second forever is a list that keeps a laptop's fan on. */
export function useJobs(limit = 30) {
  return useQuery({
    queryKey: ['jobs', limit],
    queryFn: () => listJobs(limit),
    refetchInterval: (query) => {
      const jobs = query.state.data?.jobs ?? []
      return jobs.some((job) => job.status === 'queued' || job.status === 'running') ? 1200 : false
    },
  })
}

export function useJob(id: string | null) {
  return useQuery({
    queryKey: ['job', id],
    queryFn: () => getJob(id as string),
    enabled: Boolean(id),
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'queued' || status === 'running' ? 1000 : false
    },
  })
}

export function usePieces() {
  return useQuery({ queryKey: ['pieces'], queryFn: listPieces })
}

export function usePiece(slug: string) {
  return useQuery({ queryKey: ['piece', slug], queryFn: () => getPiece(slug) })
}

/** One page of the engraving, as SVG text. Cached for as long as the piece is. */
export function usePage(slug: string, number: number | null) {
  return useQuery({
    queryKey: ['page', slug, number],
    enabled: Boolean(number),
    staleTime: Infinity,
    queryFn: async () => {
      const response = await fetch(pageUrl(slug, number as number))
      if (!response.ok) throw new Error(`page ${number} could not be loaded`)
      return response.text()
    },
  })
}

export function useStatus() {
  return useQuery({ queryKey: ['status'], queryFn: getStatus, staleTime: 10_000 })
}
