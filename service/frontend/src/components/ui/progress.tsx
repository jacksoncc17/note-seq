import { cn } from '../../lib/utils'

/** A job's progress. `indeterminate` is for a step that reports no percentage. */
export function Progress({
  value,
  className,
  tone = 'stone',
}: {
  value: number
  className?: string
  tone?: 'stone' | 'green' | 'red'
}) {
  const colour =
    tone === 'green' ? 'bg-emerald-500' : tone === 'red' ? 'bg-red-500' : 'bg-stone-800'
  return (
    <div className={cn('h-1.5 w-full overflow-hidden rounded-full bg-stone-200', className)}>
      <div
        className={cn('h-full rounded-full transition-[width] duration-300', colour)}
        style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
      />
    </div>
  )
}
