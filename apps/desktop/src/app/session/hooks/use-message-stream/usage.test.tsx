import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $currentUsage } from '@/store/session'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const SID = 'session-1'
// $currentUsage mirrors the primary session; ClientSessionState.usage drives
// the same status bar when a secondary tile is focused.
const BASELINE = { calls: 2, compressions: 1, input: 500, output: 40, total: 540 }

let handleEvent: ((event: RpcEvent) => void) | null = null
let sessionStates = new Map<string, ClientSessionState>()

function Harness() {
  const activeSessionIdRef = useRef<string | null>(SID)
  const sessionStateByRuntimeIdRef = useRef(sessionStates)
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

describe('useMessageStream status-bar usage scoping', () => {
  beforeEach(() => {
    handleEvent = null
    sessionStates = new Map([[SID, { ...createClientSessionState(), usage: { ...BASELINE } }]])
    $currentUsage.set({ ...BASELINE })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('merges a live session.usage tick from the focused session', async () => {
    await mountStream()

    act(() =>
<<<<<<< HEAD
      handleEvent!({
        payload: { usage: { context_percent: 42, input: 1200, total: 1280 } },
=======
      stream.handleEvent({
        payload: { usage: { compressions: 2, context_percent: 42, input: 1200, total: 1280 } },
>>>>>>> upstream/main
        session_id: SID,
        type: 'session.usage'
      })
    )

    // Merge, not replace: fields absent from the tick keep their prior values.
    expect($currentUsage.get()).toEqual({ ...BASELINE, compressions: 2, context_percent: 42, input: 1200, total: 1280 })
    expect(sessionStates.get(SID)?.usage).toEqual({
      ...BASELINE,
      compressions: 2,
      context_percent: 42,
      input: 1200,
      total: 1280
    })
  })

<<<<<<< HEAD
  it('caches a background session.usage tick without overwriting the primary status bar', async () => {
    await mountStream()

    act(() =>
      handleEvent!({
        payload: { usage: { input: 9999, total: 9999 } },
=======
  it('normalizes a canonical top-level session.usage tick for the status bar', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: {
          api_calls: 3,
          cache_read_tokens: 800,
          input_tokens: 1_200,
          output_tokens: 80,
          prompt_tokens: 1_000,
          tokens_per_s: 41.64,
          total_tokens: 1_280
        },
        session_id: SID,
        type: 'session.usage'
      })
    )

    expect($currentUsage.get()).toEqual({
      ...BASELINE,
      avg_tps: 41.6,
      cache_hit_pct: 80,
      calls: 3,
      input: 1_200,
      output: 80,
      prompt: 1_000,
      total: 1_280
    })
    expect(sessionStates.get(SID)?.usage).toEqual($currentUsage.get())
  })

  it('maps latency and active subagents from a canonical tick the same way Ink does', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { active_subagents: 2, api_calls: 1, avg_latency_s: 0.84, total_tokens: 10 },
        session_id: SID,
        type: 'session.usage'
      })
    )

    expect($currentUsage.get()).toMatchObject({ active_subagents: 2, avg_latency_s: 0.8, calls: 1, total: 10 })
  })

  it('merges a partial canonical tick without zeroing counters it does not carry', () => {
    mountStream()

    act(() => stream.handleEvent({ payload: { api_calls: 3 }, session_id: SID, type: 'session.usage' }))
    act(() =>
      stream.handleEvent({
        payload: { context_length: 10_000, last_prompt_tokens: 4_200 },
        session_id: SID,
        type: 'session.usage'
      })
    )

    expect($currentUsage.get()).toMatchObject({ ...BASELINE, calls: 3, context_percent: 42, context_used: 4_200 })
  })

  it('falls back to prompt_tokens and completion_tokens when input_tokens and output_tokens are zero', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: {
          api_calls: 3,
          completion_tokens: 80,
          input_tokens: 0,
          output_tokens: 0,
          prompt_tokens: 1_000,
          total_tokens: 1_080
        },
        session_id: SID,
        type: 'session.usage'
      })
    )

    expect($currentUsage.get()).toEqual({
      ...BASELINE,
      calls: 3,
      completion: 80,
      input: 1_000,
      output: 80,
      prompt: 1_000,
      total: 1_080
    })
  })

  it('caches a background session.usage tick without overwriting the primary status bar', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { usage: { compressions: 9, input: 9999, total: 9999 } },
>>>>>>> upstream/main
        session_id: 'background-session',
        type: 'session.usage'
      })
    )

    expect($currentUsage.get()).toEqual(BASELINE)
    expect(sessionStates.get('background-session')?.usage).toEqual({
      calls: 0,
      compressions: 9,
      input: 9999,
      output: 0,
      total: 9999
    })
  })

<<<<<<< HEAD
  it('applies message.complete usage from the focused session', async () => {
    await mountStream()

    act(() =>
      handleEvent!({
        payload: { text: 'done', usage: { calls: 3, input: 1500, output: 90, total: 1590 } },
=======
  it('caches authoritative session.info usage for a background tile', () => {
    sessionStates.set('background-session', { ...createClientSessionState(), usage: null })
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { usage: { calls: 3, compressions: 4, input: 700, output: 60, total: 760 } },
        session_id: 'background-session',
        type: 'session.info'
      })
    )

    expect($currentUsage.get()).toEqual(BASELINE)
    expect(sessionStates.get('background-session')?.usage).toEqual({
      calls: 3,
      compressions: 4,
      input: 700,
      output: 60,
      total: 760
    })
  })

  it('clears a stale background compression count when session.info omits it', () => {
    sessionStates.set('background-session', {
      ...createClientSessionState(),
      usage: { ...BASELINE, compressions: 4, context_percent: 55 }
    })
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { usage: { calls: 3, input: 700, output: 60, total: 760 } },
        session_id: 'background-session',
        type: 'session.info'
      })
    )

    const usage = sessionStates.get('background-session')?.usage
    expect($currentUsage.get()).toEqual(BASELINE)
    expect(usage?.compressions).toBeUndefined()
    expect(usage?.context_percent).toBe(55)
    expect(usage?.calls).toBe(3)
  })

  it('applies message.complete usage from the focused session', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { text: 'done', usage: { calls: 3, compressions: 3, input: 1500, output: 90, total: 1590 } },
>>>>>>> upstream/main
        session_id: SID,
        type: 'message.complete'
      })
    )

    expect($currentUsage.get()).toEqual({ calls: 3, compressions: 3, input: 1500, output: 90, total: 1590 })
  })

  it('ignores message.complete usage from a background session', async () => {
    await mountStream()

    act(() =>
<<<<<<< HEAD
      handleEvent!({
        payload: { text: 'done', usage: { calls: 9, input: 9999, output: 999, total: 9999 } },
=======
      stream.handleEvent({
        payload: { text: 'done', usage: { calls: 9, compressions: 9, input: 9999, output: 999, total: 9999 } },
>>>>>>> upstream/main
        session_id: 'background-session',
        type: 'message.complete'
      })
    )

    expect($currentUsage.get()).toEqual(BASELINE)
  })
})
