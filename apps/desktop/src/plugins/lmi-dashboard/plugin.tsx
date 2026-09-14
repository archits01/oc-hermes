/**
 * LMI operations dashboard — bundled so a packaged DMG has the sidebar entry.
 * The page is the production Vercel dashboard embedded in a constrained iframe;
 * the VM remains the data/API authority behind Vercel and is not a second UI.
 */

import {
  type HermesPlugin,
  host,
  PALETTE_AREA,
  type PaletteContribution,
  type RouteContribution,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  type SidebarNavContribution
} from '@hermes/plugin-sdk'
import { useState } from 'react'

const DASHBOARD_URL = 'https://lmi-dashboard-one.vercel.app/dashboard'

function DashboardPage() {
  const [loaded, setLoaded] = useState(false)
  const [failed, setFailed] = useState(false)

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-background">
      <div className="relative min-h-0 min-w-0 flex-1 overflow-hidden">
        {!loaded && !failed ? (
          <div
            aria-live="polite"
            className="absolute inset-0 z-10 flex items-center justify-center bg-background text-sm text-muted-foreground"
          >
            Loading LMI Dashboard…
          </div>
        ) : null}
        {failed ? (
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 bg-background p-6 text-center">
            <p className="text-sm text-muted-foreground">The Vercel dashboard could not be loaded.</p>
            <a
              className="text-sm font-medium text-primary underline underline-offset-4"
              href={DASHBOARD_URL}
              rel="noreferrer"
              target="_blank"
            >
              Open LMI Dashboard in a browser
            </a>
          </div>
        ) : null}
        <iframe
          allow="clipboard-read; clipboard-write"
          className="block h-full min-h-0 w-full border-0 bg-white"
          loading="eager"
          onError={() => {
            setFailed(true)
            setLoaded(false)
          }}
          onLoad={() => {
            setFailed(false)
            setLoaded(true)
          }}
          referrerPolicy="no-referrer"
          sandbox="allow-forms allow-modals allow-popups allow-same-origin allow-scripts"
          src={DASHBOARD_URL}
          title="Laser Magic India Dashboard"
        />
      </div>
    </div>
  )
}

const plugin: HermesPlugin = {
  id: 'lmi-dashboard',
  name: 'LMI Dashboard',
  description: 'Laser Magic India operations dashboard in the sidebar.',
  defaultEnabled: true,
  register(ctx) {
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/lmi-dashboard' } satisfies RouteContribution,
        render: () => <DashboardPage />
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 40,
        data: {
          path: '/lmi-dashboard',
          label: 'LMI Dashboard',
          codicon: 'dashboard'
        } satisfies SidebarNavContribution
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'lmiDashboard.open',
          label: 'LMI Dashboard',
          keywords: ['lmi', 'dashboard', 'operations', 'leads'],
          run: () => host.navigate('/lmi-dashboard')
        } satisfies PaletteContribution
      }
    ])
  }
}

export default plugin
