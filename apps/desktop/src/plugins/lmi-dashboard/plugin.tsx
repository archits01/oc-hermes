/**
 * LMI operations dashboard — bundled so a packaged DMG has the sidebar entry.
 * Renders in-app via Electron <webview> (iframe is blocked by the site CSP).
 */

import { useEffect, useRef } from 'react'

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

const DASHBOARD_URL = 'https://lmi-dashboard-one.vercel.app/dashboard/inbox'

function DashboardPage() {
  const mountRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const mount = mountRef.current

    if (!mount) {
      return
    }

    const webview = document.createElement('webview')
    webview.className = 'h-full w-full flex-1 bg-transparent'
    webview.setAttribute('partition', 'persist:hermes-preview')
    webview.setAttribute('src', DASHBOARD_URL)
    webview.setAttribute('webpreferences', 'contextIsolation=yes,nodeIntegration=no,sandbox=yes')
    mount.replaceChildren(webview)

    return () => {
      mount.replaceChildren()
    }
  }, [])

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <div className="min-h-0 w-full flex-1" ref={mountRef} />
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
