const EMAIL_VIEW_ONLY = 'emailViewOnly'
const EMAIL_VIEW_ONLY_SAFE =
  '(typeof emailViewOnly!=="undefined"?emailViewOnly:(s&&s.platform==="gmail"&&!s.memberId))'

/** Rewrite the live LMI inbox bundle so a missing emailViewOnly binding cannot crash thread open. */
export function patchLmiInboxJs(source: string): string {
  if (!source.includes(EMAIL_VIEW_ONLY) || source.includes('typeof emailViewOnly')) {
    return source
  }

  return source.replaceAll(EMAIL_VIEW_ONLY, EMAIL_VIEW_ONLY_SAFE)
}

export function shouldInterceptLmiInboxChunk(url: string): boolean {
  try {
    const parsed = new URL(url)
    return (
      parsed.hostname === 'lmi-dashboard-one.vercel.app' &&
      parsed.pathname.startsWith('/_next/static/chunks/') &&
      parsed.pathname.endsWith('.js')
    )
  } catch {
    return false
  }
}
