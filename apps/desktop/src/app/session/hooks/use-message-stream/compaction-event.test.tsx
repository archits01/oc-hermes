<<<<<<< HEAD
import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
=======
import type { GatewayEvent } from '@hermes/shared'
import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

>>>>>>> upstream/main
import { createClientSessionState } from '@/lib/chat-runtime'
import { $compactingSessions, setSessionCompacting } from '@/store/compaction'

import { useMessageStream } from './index'

const SID = 'session-1'
const OTHER_SID = 'session-2'
let handleEvent: ((event: RpcEvent) => void) | null = null

function Harness() {
  const activeSessionIdRef = useRef<string | null>(SID)
  const sessionStateByRuntimeIdRef = useRef(new Map<string, ClientSessionState>())
  const queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (sessionId, updater) => {
      const current = sessionStateByRuntimeIdRef.current.get(sessionId) ?? createClientSessionState()
      const next = updater(current)
      sessionStateByRuntimeIdRef.current.set(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    handleEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent])

  return null
}

async function mountStream() {
  render(<Harness />)
  await waitFor(() => expect(handleEvent).not.toBeNull())
}

<<<<<<< HEAD
function emit(type: RpcEvent['type'], payload: RpcEvent['payload'] = {}) {
  act(() => handleEvent!({ payload, session_id: SID, type }))
=======
function emit(type: GatewayEvent['type'], payload: GatewayEvent['payload'] = {}) {
  act(() => stream.handleEvent({ payload, session_id: SID, type }))
>>>>>>> upstream/main
}

describe('useMessageStream compaction lifecycle', () => {
  beforeEach(() => {
    handleEvent = null
    $compactingSessions.set({})
  })

  afterEach(() => {
    cleanup()
    $compactingSessions.set({})
    vi.restoreAllMocks()
  })

  it.each([
    ['message.delta', { text: 'resumed' }],
    ['reasoning.delta', { text: 'thinking again' }],
    ['tool.start', { name: 'terminal', tool_id: 'tool-1' }]
  ] as const)('clears the stale compaction phase when %s resumes the turn', async (type, payload) => {
    await mountStream()
    setSessionCompacting(OTHER_SID, true)

    emit('status.update', { kind: 'compacting' })
    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true, [SID]: true })

    emit(type, payload)

    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true })
  })

<<<<<<< HEAD
  it('clears the compaction phase on the structured completion edge', async () => {
    await mountStream()
=======
  // Manual /compress pins `compressing` (methods_session._compress_live) and
  // always clears it with `ready` from that function's `finally`. The desktop
  // matched only the auto-compaction spelling, so /compress showed no phase at
  // all — the TUI has handled both since createGatewayEventHandler.ts:904.
  it('drives the compaction phase from the manual /compress spelling', () => {
    mountStream()
    setSessionCompacting(OTHER_SID, true)

    emit('status.update', { kind: 'compressing', text: '\u280b compressing 42 messages (~120,000 tok)\u2026' })
    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true, [SID]: true })

    emit('status.update', { kind: 'ready' })

    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true })
  })

  it('clears the compaction phase on the structured completion edge', () => {
    mountStream()
>>>>>>> upstream/main
    setSessionCompacting(OTHER_SID, true)

    emit('status.update', { kind: 'compacting' })
    emit('status.update', { kind: 'compacted' })

    expect($compactingSessions.get()).toEqual({ [OTHER_SID]: true })
  })

  // #97948: a manual /compress whose RPC answered `pending` (the compute host
  // outlived the gateway's wait) has no turn-end hydrate — the `compacted`
  // edge is the only signal the transcript changed.
  it('rehydrates the idle active session on the compacted edge', () => {
    const hydrateFromStoredSession = vi.fn(async () => undefined)
    const states = new Map([[SID, { ...createClientSessionState(), storedSessionId: 'stored-1' }]])

    stream = renderMessageStream(SID, { hydrateFromStoredSession, states })

    emit('status.update', { kind: 'compacted' })

    expect(hydrateFromStoredSession).toHaveBeenCalledWith(3, 'stored-1', SID)
  })

  it('leaves the transcript to the turn settle path when compaction ends mid-turn', () => {
    const hydrateFromStoredSession = vi.fn(async () => undefined)
    const states = new Map([[SID, { ...createClientSessionState(), busy: true, storedSessionId: 'stored-1' }]])

    stream = renderMessageStream(SID, { hydrateFromStoredSession, states })

    emit('status.update', { kind: 'compacted' })

    expect(hydrateFromStoredSession).not.toHaveBeenCalled()
  })

  it('reconciles a reconnecting compaction only from trusted terminal server state', () => {
    mountStream()
    emit('status.update', { kind: 'compacting' })

    // A running heartbeat is not terminal evidence and must not hide real work.
    emit('session.info', { running: true })
    expect($compactingSessions.get()).toEqual({ [SID]: true })

    // A server-reported terminal turn is trusted reconnect evidence.
    emit('session.info', { running: false })
    expect($compactingSessions.get()).toEqual({})
  })
})
