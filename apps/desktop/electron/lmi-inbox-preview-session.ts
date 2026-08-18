import { session } from 'electron'

import { patchLmiInboxJs, shouldInterceptLmiInboxChunk } from './lmi-inbox-js-patch'

const PREVIEW_PARTITION = 'persist:hermes-preview'

function stripRewriteHeaders(headers: Record<string, string | string[]>): Record<string, string | string[]> {
  const next = { ...headers }

  for (const key of Object.keys(next)) {
    if (/^content-encoding$/i.test(key) || /^content-length$/i.test(key)) {
      delete next[key]
    }
  }

  return next
}

type PreviewWebRequest = Electron.WebRequest & {
  filterResponseData(id: number): NodeJS.ReadWriteStream
}

function attachLmiInboxPreviewSession(previewSession: Electron.Session): void {
  const webRequest = previewSession.webRequest as PreviewWebRequest

  webRequest.onHeadersReceived(
    { urls: ['https://lmi-dashboard-one.vercel.app/_next/static/chunks/*'] },
    (details, callback) => {
      const headers = details.responseHeaders || {}

      if (!shouldInterceptLmiInboxChunk(details.url)) {
        callback({ responseHeaders: headers })
        return
      }

      try {
        const filter = webRequest.filterResponseData(details.id)
        const chunks: Buffer[] = []

        filter.on('data', (chunk: Buffer | string) => {
          chunks.push(Buffer.from(chunk))
        })
        filter.on('end', () => {
          const raw = Buffer.concat(chunks).toString('utf8')
          filter.write(Buffer.from(patchLmiInboxJs(raw), 'utf8'))
          filter.end()
        })
        filter.on('error', () => {
          try {
            filter.end()
          } catch {
            // The stream is already closed.
          }
        })
      } catch {
        callback({ responseHeaders: headers })
        return
      }

      callback({ responseHeaders: stripRewriteHeaders(headers) })
    }
  )
}

/** Patch the live LMI inbox bundle inside the desktop webview partition only. */
export function installLmiInboxPreviewSession(): void {
  try {
    attachLmiInboxPreviewSession(session.fromPartition(PREVIEW_PARTITION))
  } catch {
    // Non-fatal: inbox still opens the live site; the upstream JS bug remains.
  }
}
