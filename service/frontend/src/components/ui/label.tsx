import type { LabelHTMLAttributes } from 'react'

import { cn } from '../../lib/utils'

export function Label({ className, ...props }: LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label
      className={cn('block text-xs font-medium uppercase tracking-wide text-stone-500', className)}
      {...props}
    />
  )
}
