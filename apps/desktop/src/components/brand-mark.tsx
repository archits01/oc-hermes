import { cn } from '@/lib/utils'

const assetPath = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`

// Brand badge: OpenComputer flower mark on a white tile, identical in light/dark.
// Never use the nous-girl / Hermes girl here. Flower only.
// Fills the tile (softly rounded); size via className (default size-14).
export function BrandMark({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span
      className={cn(
        'inline-flex size-14 shrink-0 items-center justify-center overflow-hidden rounded-md bg-white',
        className
      )}
      {...props}
    >
      <img alt="" className="size-full object-contain" src={assetPath('flower.png')} />
    </span>
  )
}
