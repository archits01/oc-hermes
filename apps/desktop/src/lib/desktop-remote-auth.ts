import type { DesktopAuthProvider } from '@/global'

export interface RemoteAuthProviderShape {
  isPassword: boolean
  providerLabel: string
}

function providerDisplayName(provider: DesktopAuthProvider): string {
  const raw = provider.displayName || provider.name

  return /\b(?:nous|hermes(?:\s+agent)?)\b/i.test(raw) ? 'Open Computer Account' : raw
}

export function deriveRemoteAuthProviderShape(
  providers: DesktopAuthProvider[] | null | undefined,
  fallback = 'your identity provider'
): RemoteAuthProviderShape {
  const list = providers ?? []

  if (list.length === 0) {
    return { isPassword: false, providerLabel: fallback }
  }

  return {
    isPassword: list.every(provider => Boolean(provider.supportsPassword)),
    providerLabel: list.map(providerDisplayName).join(' / ')
  }
}
