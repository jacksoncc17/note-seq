import type { InputHTMLAttributes } from 'react'

import { cn } from '../../lib/utils'

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cn(
        'h-9 w-full rounded-md border border-stone-300 bg-white px-3 text-sm text-stone-900 ' +
          'placeholder:text-stone-400 focus:border-stone-500 focus:outline-none ' +
          'file:mr-2 file:h-6 file:rounded file:border-0 file:bg-stone-100 file:px-2 file:text-xs ' +
          'file:font-medium file:text-stone-700',
        className,
      )}
      {...props}
    />
  )
}
