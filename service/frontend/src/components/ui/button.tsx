import { cva, type VariantProps } from 'class-variance-authority'
import type { ButtonHTMLAttributes } from 'react'

import { cn } from '../../lib/utils'

// The button set this app actually uses: a primary action, a quiet one, and a plain
// one for the small square controls around the score.
export const button = cva(
  'inline-flex items-center justify-center gap-1.5 rounded-md text-sm font-medium transition-colors ' +
    'disabled:pointer-events-none disabled:opacity-45 focus-visible:outline-2 focus-visible:outline-offset-2 ' +
    'focus-visible:outline-stone-400 whitespace-nowrap',
  {
    variants: {
      variant: {
        primary: 'bg-stone-900 text-white hover:bg-stone-700',
        secondary: 'bg-white text-stone-800 ring-1 ring-stone-300 hover:bg-stone-100',
        ghost: 'text-stone-600 hover:bg-stone-200/70 hover:text-stone-900',
        danger: 'bg-red-600 text-white hover:bg-red-500',
      },
      size: {
        sm: 'h-8 px-3',
        md: 'h-9 px-4',
        icon: 'h-8 w-8',
      },
    },
    defaultVariants: { variant: 'secondary', size: 'md' },
  },
)

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> &
  VariantProps<typeof button>

export function Button({ className, variant, size, ...props }: ButtonProps) {
  return <button className={cn(button({ variant, size }), className)} {...props} />
}
