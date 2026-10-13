import type { HTMLAttributes } from 'react'

import { cn } from '../../lib/utils'

const tones = {
  queued: 'bg-stone-100 text-stone-600 ring-stone-200',
  running: 'bg-amber-50 text-amber-800 ring-amber-200',
  done: 'bg-emerald-50 text-emerald-800 ring-emerald-200',
  failed: 'bg-red-50 text-red-800 ring-red-200',
  plain: 'bg-stone-100 text-stone-700 ring-stone-200',
} as const

export function Badge({
  tone = 'plain',
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement> & { tone?: keyof typeof tones }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset',
        tones[tone],
        className,
      )}
      {...props}
    />
  )
}
