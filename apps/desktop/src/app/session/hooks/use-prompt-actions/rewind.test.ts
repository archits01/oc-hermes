import { describe, expect, it } from 'vitest'

import { type ChatMessage, chatMessageText, textPart, toChatMessages } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'

import {
  appendMidTurnUserMessage,
  applyReloadOptimistic,
  applyRewindOptimistic,
  finalizeInterruptedMessages,
  isSyntheticRendererId,
  planEdit,
  planReload,
  planRestore,
  rebindSurvivorRowIds,
  resolveDurableRowId,
  runRewindSubmit,
  survivorRowIdsFrom,
  truncateSubmitParams
} from './rewind'
import { isFailedUserTurn, visibleUserOrdinal } from './utils'

const row = (id: string, role: ChatMessage['role'], text: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  id,
  role,
  parts: [textPart(text)],
  ...extra
})

type MidTurnState = { interimBoundaryPending: boolean; messages: ChatMessage[]; streamId: null | string }

describe('appendMidTurnUserMessage', () => {
  // #73793: a message typed while a turn streams must land AFTER the assistant
  // output the user had already watched arrive — never spliced above it.
  it('appends the mid-turn message after the live assistant output, sealed in place', () => {
    const state: MidTurnState = {
      interimBoundaryPending: false,
      streamId: 'assistant-stream-1',
      messages: [
        row('user-1', 'user', 'long task'),
        row('assistant-stream-1', 'assistant', 'two screens of output', { pending: true })
      ]
    }

    const next = appendMidTurnUserMessage(state, row('user-2', 'user', 'urgently'))

    expect(next.messages.map(message => message.id)).toEqual(['user-1', 'assistant-stream-1', 'user-2'])
    // The sealed bubble stops streaming; the turn's next delta seeds a fresh
    // bubble BELOW the correction instead of mutating the one above it.
    expect(next.messages[1]).toMatchObject({ pending: false, interim: true })
    expect(next.streamId).toBeNull()
    expect(next.interimBoundaryPending).toBe(true)
  })

  // #83151: the retired insert-before splice fell back to the LAST assistant
  // row anywhere in the transcript when the stream id was stale, landing the
  // new prompt mid-thread. A stale/missing stream id must append at the tail.
  it('appends at the live tail when the stream id is stale or missing', () => {
    const state: MidTurnState = {
      interimBoundaryPending: false,
      streamId: 'assistant-stream-stale',
      messages: [
        row('user-1', 'user', 'old prompt'),
        row('assistant-1', 'assistant', 'old committed reply'),
        row('user-2', 'user', 'newer prompt'),
        row('assistant-2', 'assistant', 'newer committed reply')
      ]
    }

    const next = appendMidTurnUserMessage(state, row('user-3', 'user', 'mid-turn note'))

    expect(next.messages.map(message => message.id)).toEqual([
      'user-1',
      'assistant-1',
      'user-2',
      'assistant-2',
      'user-3'
    ])
    expect(next.messages.at(-1)?.id).toBe('user-3')
    expect(next.interimBoundaryPending).toBe(false)
  })

  it('drops an empty pending stream placeholder instead of sealing it', () => {
    const state: MidTurnState = {
      interimBoundaryPending: false,
      streamId: 'assistant-stream-1',
      messages: [
        row('user-1', 'user', 'task'),
        { id: 'assistant-stream-1', role: 'assistant', parts: [], pending: true }
      ]
    }

    const next = appendMidTurnUserMessage(state, row('user-2', 'user', 'correction'))

    expect(next.messages.map(message => message.id)).toEqual(['user-1', 'user-2'])
    expect(next.streamId).toBeNull()
    expect(next.interimBoundaryPending).toBe(false)
  })
})

describe('truncateSubmitParams', () => {
  it('omits truncation fields when no ordinal is set', () => {
    expect(truncateSubmitParams(undefined)).toEqual({})
  })

  it('confirms intent for every ordinal, and the empty edge only for ordinal 0', () => {
    expect(truncateSubmitParams(0)).toEqual({
      confirm_truncate: true,
      truncate_before_user_ordinal: 0,
      confirm_empty_truncate: true
    })
    expect(truncateSubmitParams(1)).toEqual({
      confirm_truncate: true,
      truncate_before_user_ordinal: 1
    })
  })

  it('never sends an ordinal without the intent flag the gateway requires', () => {
    // The gateway drops history only for a submit that declares itself a
    // rewind. An ordinal built here without confirm_truncate would be refused
    // (and, on an older gateway, would truncate silently).
    for (const ordinal of [0, 1, 2, 7]) {
      const params = truncateSubmitParams(ordinal)

      expect(params.truncate_before_user_ordinal).toBe(ordinal)
      expect(params.confirm_truncate).toBe(true)
    }
  })

  it('includes truncate_before_row_id when passed', () => {
    expect(truncateSubmitParams(1, 'msg-123', 456)).toEqual({
      confirm_truncate: true,
      truncate_before_user_ordinal: 1,
      truncate_before_message_id: 'msg-123',
      truncate_before_row_id: 456
    })
    expect(truncateSubmitParams(undefined, undefined, 456)).toEqual({
      confirm_truncate: true,
      truncate_before_row_id: 456
    })
  })

  it('drops renderer-synthetic message ids but keeps durable row ids', () => {
    // chat-messages.ts: `${timestamp}-${index}-${role}`
    expect(truncateSubmitParams(1, '1723456789-0-user', 456)).toEqual({
      confirm_truncate: true,
      truncate_before_user_ordinal: 1,
      truncate_before_row_id: 456
    })
    expect(truncateSubmitParams(0, 'user-1723456789-0', undefined)).toEqual({
      confirm_truncate: true,
      truncate_before_user_ordinal: 0,
      confirm_empty_truncate: true
    })
  })
})

describe('survivorRowIdsFrom', () => {
  it('returns undefined when the field is absent or not an array (older gateway)', () => {
    expect(survivorRowIdsFrom(undefined)).toBeUndefined()
    expect(survivorRowIdsFrom({ status: 'streaming' })).toBeUndefined()
    expect(survivorRowIdsFrom({ survivor_user_row_ids: 'nope' })).toBeUndefined()
  })

  it('keeps integer ids and nulls anything else', () => {
    expect(survivorRowIdsFrom({ survivor_user_row_ids: [7, null, 9.5, '11', 12] })).toEqual([7, null, null, null, 12])
  })
})

describe('rebindSurvivorRowIds', () => {
  const user = (id: string, rowId?: number, hidden?: boolean): ChatMessage => ({
    id,
    role: 'user',
    parts: [textPart(`text ${id}`)],
    ...(rowId !== undefined ? { rowId } : {}),
    ...(hidden ? { hidden } : {})
  })

  const assistant = (id: string, rowId?: number): ChatMessage => ({
    id,
    role: 'assistant',
    parts: [textPart(`reply ${id}`)],
    ...(rowId !== undefined ? { rowId } : {})
  })

  it('rebinds surviving visible user turns positionally and clears the resubmitted turn', () => {
    // Post-rewind state: two survivors + the resubmitted turn (stale rowId 5).
    const messages = [user('u0', 1), assistant('a0', 2), user('u1', 3), assistant('a1', 4), user('u2', 5)]
    const rebound = rebindSurvivorRowIds(messages, [7, 9])

    expect(rebound[0].rowId).toBe(7)
    expect(rebound[2].rowId).toBe(9)
    // Resubmitted turn is past the survivor list — its durable id doesn't
    // exist yet, and keeping the stale one would 4018 the next rewind.
    expect(rebound[4].rowId).toBeUndefined()
    // Assistant rows are untouched (only user turns are rewind targets).
    expect(rebound[1].rowId).toBe(2)
  })

  it('clears the cached id for null entries instead of keeping a stale one', () => {
    const messages = [user('u0', 1), user('u1', 3)]
    const rebound = rebindSurvivorRowIds(messages, [null, 9])

    expect(rebound[0].rowId).toBeUndefined()
    expect(rebound[1].rowId).toBe(9)
  })

  it('skips hidden user turns — same visible-user filter as the ordinal math', () => {
    const messages = [user('u0', 1), user('hidden', 2, true), user('u1', 3)]
    const rebound = rebindSurvivorRowIds(messages, [7, 9])

    expect(rebound[0].rowId).toBe(7)
    expect(rebound[1].rowId).toBe(2) // hidden: untouched
    expect(rebound[2].rowId).toBe(9)
  })

  it('preserves object identity when nothing changes', () => {
    const messages = [user('u0', 7)]

    expect(rebindSurvivorRowIds(messages, [7])[0]).toBe(messages[0])
  })
})

describe('finalizeInterruptedMessages', () => {
  it('records when a stopped assistant turn and its active part ended', () => {
    const [message] = finalizeInterruptedMessages(
      [
        {
          id: 'assistant-live',
          role: 'assistant',
          parts: [{ type: 'text', text: 'partial answer', timestamp: 10 }],
          pending: true,
          timestamp: 10
        }
      ],
      'assistant-live',
      11.25
    )

    expect(message.completedAt).toBe(11.25)
    expect(message.parts[0].completedAt).toBe(11.25)
    expect(message.pending).toBe(false)
  })

  it('keeps and closes a tool-only assistant turn when it is stopped', () => {
    const [message] = finalizeInterruptedMessages(
      [
        {
          id: 'assistant-tool',
          role: 'assistant',
          parts: [
            {
              args: {} as never,
              argsText: '{}',
              timestamp: 10,
              toolCallId: 'call-1',
              toolName: 'terminal',
              type: 'tool-call'
            }
          ],
          pending: true,
          timestamp: 10
        }
      ],
      'assistant-tool',
      11.25
    )

    expect(message.parts).toHaveLength(1)
    expect(message.parts[0].completedAt).toBe(11.25)
    expect(message.completedAt).toBe(11.25)
  })
})

describe('failed-turn-aware ordinal space', () => {
  const user = (id: string, rowId?: number): ChatMessage => ({
    id,
    role: 'user',
    parts: [textPart(`text ${id}`)],
    ...(rowId !== undefined ? { rowId } : {})
  })

  const assistant = (id: string, rowId?: number): ChatMessage => ({
    id,
    role: 'assistant',
    parts: [textPart(`reply ${id}`)],
    ...(rowId !== undefined ? { rowId } : {})
  })

  const failedAssistant = (id: string): ChatMessage => ({
    id,
    role: 'assistant',
    parts: [textPart(`err ${id}`)],
    error: 'provider down'
  })

  it('planEdit/planReload skip failed turns when counting ordinals (#41275)', () => {
    // u0 failed (never persisted), u1/u2 succeeded. Backend counts u1 before
    // u2, so editing u2 must aim at ordinal 1, not 2.
    const messages: ChatMessage[] = [
      user('u0', undefined),
      failedAssistant('a0'),
      user('u1', 11),
      assistant('a1', 12),
      user('u2', 13),
      assistant('a2', 14)
    ]

    const reload = planReload(messages, 'a2')

    expect(reload?.truncateOrdinal).toBe(1)
    expect(reload?.truncateRowId).toBe(13)
  })

  it('planEdit still flags failed turns via the shared helper', () => {
    const messages: ChatMessage[] = [user('u0', 11), assistant('a0', 12), user('u1', undefined), failedAssistant('a1')]

    const plan = planEdit(messages, {
      role: 'user',
      sourceId: 'u1',
      parentId: null,
      content: [{ type: 'text', text: 'better text' }]
    } as never)

    expect(plan?.isFailedTurn).toBe(true)
    expect(plan?.truncateOrdinal).toBeUndefined()
    expect(plan?.sourceText).toBe('text u1')
  })

  it('planReload degrades a failed turn to a plain resubmit (#86623)', () => {
    const messages: ChatMessage[] = [user('u0', 11), assistant('a0', 12), user('u1', undefined), failedAssistant('a1')]

    const reload = planReload(messages, 'a1')

    expect(reload?.text).toBe('text u1')
    expect(reload?.truncateOrdinal).toBeUndefined()
    expect(reload?.truncateRowId).toBeUndefined()
    expect(reload?.truncateMessageId).toBeUndefined()
  })

  it('planRestore degrades a failed turn to a plain resubmit', () => {
    const messages: ChatMessage[] = [user('u0', 11), assistant('a0', 12), user('u1', undefined), failedAssistant('a1')]

    const plan = planRestore(messages, 'u1')

    expect(plan.truncateOrdinal).toBeUndefined()
    expect(plan.truncateRowId).toBeUndefined()
    expect(plan.truncateMessageId).toBeUndefined()
  })

  it('rebindSurvivorRowIds skips failed turns — they hold no survivor slot', () => {
    const messages: ChatMessage[] = [user('u0', 1), failedAssistant('a0'), user('u1', 3), assistant('a1', 4)]
    const rebound = rebindSurvivorRowIds(messages, [9])

    // u0 failed: untouched. u1 is survivor ordinal 0.
    expect(rebound[0].rowId).toBe(1)
    expect(rebound[2].rowId).toBe(9)
  })
})

describe('resolveDurableRowId', () => {
  const gatewayWith = (messages: unknown[]) => {
    const request = (async (method: string) => {
      expect(method).toBe('session.history')

      return { messages }
    }) as <T>(method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<T>

    return request
  }

  it('resolves a unique content match to its durable row id', async () => {
    const request = gatewayWith([
      { role: 'user', text: 'first prompt', row_id: 11 },
      { role: 'assistant', text: 'reply', row_id: 12 },
      { role: 'user', text: 'typo prompt', row_id: 13 }
    ])

    expect(await resolveDurableRowId(request, 'sid', 'typo prompt', 1)).toBe(13)
  })

  it('ignores synthetic user-role injections (display_kind rows)', async () => {
    const request = gatewayWith([
      { role: 'user', text: 'real prompt', row_id: 11 },
      { role: 'user', text: 'real prompt', row_id: 12, display_kind: 'auto_continue' }
    ])

    expect(await resolveDurableRowId(request, 'sid', 'real prompt', 0)).toBe(11)
  })

  it('refuses ambiguous matches unless the target is provably the newest turn', async () => {
    const request = gatewayWith([
      { role: 'user', text: 'same text', row_id: 11 },
      { role: 'user', text: 'same text', row_id: 13 }
    ])

    // Ambiguous + target not the latest -> undefined (plain resubmit).
    expect(await resolveDurableRowId(request, 'sid', 'same text', 0)).toBeUndefined()
    // Ambiguous + target IS the latest persisted turn -> last match wins.
    expect(await resolveDurableRowId(request, 'sid', 'same text', 1)).toBe(13)
  })

  it('returns undefined on gateway failure or empty text', async () => {
    const failing = (async () => {
      throw new Error('boom')
    }) as <T>(method: string) => Promise<T>

    expect(await resolveDurableRowId(failing, 'sid', 'text', 0)).toBeUndefined()
    expect(await resolveDurableRowId(gatewayWith([]), 'sid', '   ', 0)).toBeUndefined()
  })
})

describe('runRewindSubmit durable-address discipline (#87059)', () => {
  interface Call {
    method: string
    params?: Record<string, unknown>
  }

  const historyMessages = [
    { role: 'user', text: 'first prompt', row_id: 11 },
    { role: 'assistant', text: 'ok', row_id: 12 },
    { role: 'user', text: 'typo prompt', row_id: 13 }
  ]

  const makeGateway = (calls: Call[]) =>
    (async (method: string, params?: Record<string, unknown>) => {
      calls.push({ method, params })

      if (method === 'session.history') {
        return { messages: historyMessages }
      }

      return { status: 'streaming' }
    }) as <T>(method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<T>

  it('resolves a missing rowId by content before submitting, and drops the client ordinal', async () => {
    const calls: Call[] = []

    await runRewindSubmit(
      makeGateway(calls),
      'sid',
      'fixed prompt',
      5,
      undefined,
      false,
      undefined,
      undefined,
      'typo prompt'
    )

    const submit = calls.find(call => call.method === 'prompt.submit')

    expect(submit?.params?.truncate_before_row_id).toBe(13)
    expect(submit?.params?.truncate_before_user_ordinal).toBeUndefined()
    expect(submit?.params?.confirm_truncate).toBe(true)
  })

  it('degrades to a plain resubmit when the row id cannot be resolved', async () => {
    const calls: Call[] = []

    await runRewindSubmit(
      makeGateway(calls),
      'sid',
      'fixed prompt',
      5,
      undefined,
      false,
      undefined,
      undefined,
      'unknown text'
    )

    const submit = calls.find(call => call.method === 'prompt.submit')

    expect(submit?.params?.truncate_before_row_id).toBeUndefined()
    expect(submit?.params?.truncate_before_user_ordinal).toBeUndefined()
    expect(submit?.params?.confirm_truncate).toBeUndefined()
  })

  it('leaves a bound durable rowId untouched (no extra history call) and drops the client ordinal', async () => {
    const calls: Call[] = []

    await runRewindSubmit(makeGateway(calls), 'sid', 'fixed prompt', 1, undefined, false, undefined, 13, 'typo prompt')

    expect(calls.some(call => call.method === 'session.history')).toBe(false)

    const submit = calls.find(call => call.method === 'prompt.submit')

    expect(submit?.params?.truncate_before_row_id).toBe(13)
    expect(submit?.params?.truncate_before_user_ordinal).toBeUndefined()
  })

  it('drops the client ordinal whenever a durable row id is present, including a complete live transcript (#88082, #89244)', async () => {
    const calls: Call[] = []

    await runRewindSubmit(
      makeGateway(calls),
      'sid',
      'fixed prompt',
      1, // display-lineage / window-relative — not the gateway tip
      undefined,
      false,
      undefined,
      13,
      'typo prompt'
    )

    // No content-resolution read needed — the bubble already holds a durable id.
    expect(calls.some(call => call.method === 'session.history')).toBe(false)

    const submit = calls.find(call => call.method === 'prompt.submit')

    expect(submit?.params?.confirm_truncate).toBe(true)
    expect(submit?.params?.truncate_before_row_id).toBe(13)
    expect(submit?.params?.truncate_before_user_ordinal).toBeUndefined()
  })

  it('drops the client ordinal when the cut is addressed by durable message id alone', async () => {
    const calls: Call[] = []

    await runRewindSubmit(
      makeGateway(calls),
      'sid',
      'fixed prompt',
      1,
      'durable-platform-msg-1',
      false,
      undefined,
      undefined,
      'typo prompt'
    )

    const submit = calls.find(call => call.method === 'prompt.submit')

    expect(submit?.params?.confirm_truncate).toBe(true)
    expect(submit?.params?.truncate_before_message_id).toBe('durable-platform-msg-1')
    expect(submit?.params?.truncate_before_user_ordinal).toBeUndefined()
  })

  it('still confirms an empty truncate when the dropped ordinal was 0', async () => {
    const calls: Call[] = []

    await runRewindSubmit(
      makeGateway(calls),
      'sid',
      'fixed prompt',
      0,
      'durable-platform-msg-1',
      false,
      undefined,
      13,
      'typo prompt'
    )

    const submit = calls.find(call => call.method === 'prompt.submit')

    expect(submit?.params?.confirm_truncate).toBe(true)
    expect(submit?.params?.confirm_empty_truncate).toBe(true)
    expect(submit?.params?.truncate_before_row_id).toBe(13)
    expect(submit?.params?.truncate_before_user_ordinal).toBeUndefined()
  })
})

describe('optimistic rewind/reload turn-clock seeding (#86795)', () => {
  // The no-payload settle gate in gateway-event.ts holds a running=false
  // heartbeat off while an optimistically armed turn waits for the backend —
  // but only for a bounded grace window measured from turnStartedAt. These
  // transforms are the arm sites for restore/edit/regenerate, so they must
  // seed the clock (and reset turnLive): an armed state without a clock is
  // settled by the first heartbeat, and a STALE clock from a previous turn
  // would let a heartbeat settle the fresh arm immediately.
  const seeded = (extra: Partial<ReturnType<typeof createClientSessionState>>) => ({
    ...createClientSessionState(null, [row('u1', 'user', 'first'), row('a1', 'assistant', 'answer')]),
    ...extra
  })

  it('applyRewindOptimistic arms busy with a fresh turn clock', () => {
    const before = Date.now()
    const next = applyRewindOptimistic(seeded({ turnLive: true, turnStartedAt: before - 60_000 }), 0)

    expect(next.busy).toBe(true)
    expect(next.awaitingResponse).toBe(true)
    expect(next.turnLive).toBe(false)
    expect(next.turnStartedAt).toBeGreaterThanOrEqual(before)
  })

  it('applyReloadOptimistic arms busy with a fresh turn clock', () => {
    const state = seeded({ turnLive: true, turnStartedAt: Date.now() - 60_000 })
    const plan = planReload(state.messages, null)
    const before = Date.now()

    expect(plan).not.toBeNull()

    const next = applyReloadOptimistic(state, plan!)

    expect(next.busy).toBe(true)
    expect(next.turnLive).toBe(false)
    expect(next.turnStartedAt).toBeGreaterThanOrEqual(before)
  })
})

describe('isFailedUserTurn with hidden messages', () => {
  const userMsg = (id: string) => ({ id, parts: [{ text: id, type: 'text' as const }], role: 'user' as const })

  const assistant = (id: string, extra: Record<string, unknown> = {}) => ({
    id,
    parts: [{ text: id, type: 'text' as const }],
    role: 'assistant' as const,
    ...extra
  })

  it('judges the turn by the visible reply, not a hidden one', () => {
    // A regenerate rewrites history to [user, oldAssistant{hidden,error}, new].
    // The transcript shows a healthy turn, so the ordinal space must count it.
    const messages = [
      userMsg('u0'),
      assistant('a-old', { error: 'boom', hidden: true }),
      assistant('a-new')
    ] as unknown as Parameters<typeof isFailedUserTurn>[0]

    expect(isFailedUserTurn(messages, 0)).toBe(false)
    expect(visibleUserOrdinal(messages, messages.length)).toBe(1)
  })

  it('still reports a failure when the visible reply errored', () => {
    const messages = [
      userMsg('u0'),
      assistant('a-old', { hidden: true }),
      assistant('a-new', { error: 'boom' })
    ] as unknown as Parameters<typeof isFailedUserTurn>[0]

    expect(isFailedUserTurn(messages, 0)).toBe(true)
    expect(visibleUserOrdinal(messages, messages.length)).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// Durable-target resolution
// ---------------------------------------------------------------------------

describe('planRestore survives client id churn', () => {
  it('toChatMessages ids are index-derived, so a prepended older page rewrites them', () => {
    const tail = [
      { role: 'user' as const, content: 'first', timestamp: 10, row_id: 101 },
      { role: 'assistant' as const, content: 'reply', timestamp: 11, row_id: 102 }
    ]

    // What backfillOlderTranscriptPage prepends when the user scrolls up.
    const withOlderPage = [
      { role: 'user' as const, content: 'older', timestamp: 1, row_id: 1 },
      { role: 'assistant' as const, content: 'older reply', timestamp: 2, row_id: 2 },
      ...tail
    ]

    const before = toChatMessages(tail)
    const after = toChatMessages(withOlderPage)

    const beforeTarget = before.find(m => chatMessageText(m) === 'first')!
    const afterTarget = after.find(m => chatMessageText(m) === 'first')!

    // Same durable row, different client id. This is the churn that strands the
    // id captured when the restore-confirm dialog opened: `id` is
    // `${timestamp}-${index}-${role}`, and the index moved.
    expect(afterTarget.rowId).toBe(beforeTarget.rowId)
    expect(afterTarget.id).not.toBe(beforeTarget.id)
  })

  it('resolves by durable rowId when the captured id no longer exists', () => {
    const messages = [
      row('fresh-u1', 'user', 'first', { rowId: 101 }),
      row('fresh-a1', 'assistant', 'reply'),
      row('fresh-u2', 'user', 'second', { rowId: 103 }),
      row('fresh-a2', 'assistant', 'reply two')
    ]

    // The dialog captured `stale-u2` before a background poll rebuilt the
    // transcript; the ordinal is null because the helper could not find it.
    const plan = planRestore(messages, 'stale-u2', { rowId: 103, text: 'second', userOrdinal: null })

    expect(plan.sourceIndex).toBe(2)
    expect(plan.truncateRowId).toBe(103)
    expect(plan.truncateOrdinal).toBe(1)
    expect(plan.text).toBe('second')
  })

  it('prefers the durable rowId over a drifted client ordinal', () => {
    const messages = [
      row('fresh-u1', 'user', 'first', { rowId: 101 }),
      row('fresh-a1', 'assistant', 'reply'),
      row('fresh-u2', 'user', 'second', { rowId: 103 }),
      row('fresh-a2', 'assistant', 'reply two')
    ]

    // A stale ordinal would aim at turn 0 and silently destroy more history
    // than the user asked for. The durable row must win.
    const plan = planRestore(messages, 'stale-u2', { rowId: 103, text: 'second', userOrdinal: 0 })

    expect(plan.sourceIndex).toBe(2)
    expect(plan.truncateRowId).toBe(103)
    expect(plan.truncateOrdinal).toBe(1)
  })

  it('falls back to the id when the rowId is not in this transcript', () => {
    const messages = [
      row('u1', 'user', 'first', { rowId: 101 }),
      row('a1', 'assistant', 'reply'),
      row('u2', 'user', 'second', { rowId: 103 })
    ]

    const plan = planRestore(messages, 'u1', { rowId: 999, text: 'first', userOrdinal: 0 })

    expect(plan.sourceIndex).toBe(0)
    expect(plan.truncateRowId).toBe(101)
  })

  it('still fails loud when nothing resolves', () => {
    const messages = [row('u1', 'user', 'first', { rowId: 101 }), row('a1', 'assistant', 'reply')]

    // No durable anchor, dead id, no ordinal: refuse rather than guess. A
    // wrong guess truncates history the user did not ask to lose.
    expect(() => planRestore(messages, 'gone', { text: 'first', userOrdinal: null })).toThrow(
      'Could not find the message to restore.'
    )
  })
})

describe('planRestore ordinal under a partial transcript window', () => {
  const partial = [
    row('u1', 'user', 'first', { rowId: 501 }),
    row('a1', 'assistant', 'reply'),
    row('u2', 'user', 'second', { rowId: 503 }),
    row('a2', 'assistant', 'reply two')
  ]

  it('omits the ordinal when older pages are unloaded and a row anchor exists', () => {
    // Tail hydration loads only the newest page, so this ordinal counts the
    // loaded turns and undercounts the gateway's by every unloaded older one.
    // The gateway refuses that mismatch (4030) under the same "Restore failed"
    // toast, and its contract is that a null ordinal aims purely at the
    // durable target.
    const plan = planRestore(partial, 'u2', { rowId: 503, text: 'second', userOrdinal: 1 }, {
      transcriptPossiblyTruncated: true
    })

    expect(plan.sourceIndex).toBe(2)
    expect(plan.truncateRowId).toBe(503)
    expect(plan.truncateOrdinal).toBeUndefined()
  })

  it('keeps the ordinal cross-check when the whole transcript is loaded', () => {
    const plan = planRestore(partial, 'u2', { rowId: 503, text: 'second', userOrdinal: 1 }, {
      transcriptPossiblyTruncated: false
    })

    expect(plan.truncateOrdinal).toBe(1)
    expect(plan.truncateRowId).toBe(503)
  })

  it('defaults to sending the ordinal when the caller says nothing', () => {
    const plan = planRestore(partial, 'u2', { rowId: 503, text: 'second', userOrdinal: 1 })

    expect(plan.truncateOrdinal).toBe(1)
  })

  it('still sends the ordinal on a partial window with no row anchor', () => {
    // Nothing durable to aim with. The gateway fails closed on ordinal-only
    // truncation of a durable session (4004), so leaving it is the safe half.
    const noRows = [row('u1', 'user', 'first'), row('a1', 'assistant', 'reply'), row('u2', 'user', 'second')]

    const plan = planRestore(noRows, 'u2', { text: 'second', userOrdinal: 1 }, { transcriptPossiblyTruncated: true })

    expect(plan.truncateOrdinal).toBe(1)
    expect(plan.truncateRowId).toBeUndefined()
  })
})

describe('rebindSurvivorRowIds on a partial transcript window', () => {
  const survivors = [901, 902, 903]

  it('binds positionally when the whole transcript is loaded', () => {
    const messages = [
      row('u1', 'user', 'one', { rowId: 1 }),
      row('a1', 'assistant', 'r1'),
      row('u2', 'user', 'two', { rowId: 2 }),
      row('a2', 'assistant', 'r2'),
      row('u3', 'user', 'three', { rowId: 3 })
    ]

    const out = rebindSurvivorRowIds(messages, survivors, true)

    expect(out.filter(m => m.role === 'user').map(m => m.rowId)).toEqual([901, 902, 903])
  })

  it('clears row ids instead of mis-binding when older pages are unloaded', () => {
    // The gateway builds survivor_user_row_ids from ITS ordinal 0 over the
    // whole tip; the client counts from the start of the loaded window. When
    // the window is a suffix, position i on the client is gateway turn i+P, so
    // binding positionally stamps every turn with an id belonging to a turn P
    // positions earlier — a wrong-but-EXISTING row id, which the gateway will
    // happily truncate at. Clearing is self-healing: the next hydrate restores
    // real ids, and meanwhile planRestore keeps sending the ordinal so the
    // gateway's cross-check still guards the cut.
    const messages = [
      row('u1', 'user', 'one', { rowId: 11 }),
      row('a1', 'assistant', 'r1'),
      row('u2', 'user', 'two', { rowId: 12 })
    ]

    const out = rebindSurvivorRowIds(messages, survivors, false)

    expect(out.filter(m => m.role === 'user').map(m => m.rowId)).toEqual([undefined, undefined])
  })

  it('defaults to the complete-window behaviour when the caller says nothing', () => {
    const messages = [row('u1', 'user', 'one', { rowId: 1 }), row('a1', 'assistant', 'r1')]

    expect(rebindSurvivorRowIds(messages, [901]).find(m => m.role === 'user')?.rowId).toBe(901)
  })
})

describe('planEdit and planReload respect the transcript window', () => {
  const messages = [
    row('u1', 'user', 'first', { rowId: 701 }),
    row('a1', 'assistant', 'reply'),
    row('u2', 'user', 'second', { rowId: 703 }),
    row('a2', 'assistant', 'reply two')
  ]

  it('planReload omits the ordinal on a partial window', () => {
    const paged = planReload(messages, 'a2', { transcriptPossiblyTruncated: true })
    const whole = planReload(messages, 'a2', { transcriptPossiblyTruncated: false })

    expect(paged?.truncateOrdinal).toBeUndefined()
    expect(paged?.truncateRowId).toBe(703)
    expect(whole?.truncateOrdinal).toBe(1)
  })

  it('planEdit omits the ordinal on a partial window', () => {
    const edited = {
      role: 'user' as const,
      sourceId: 'u2',
      content: [{ type: 'text' as const, text: 'second edited' }]
    }

    const paged = planEdit(messages, edited as never, { transcriptPossiblyTruncated: true })
    const whole = planEdit(messages, edited as never, { transcriptPossiblyTruncated: false })

    expect(paged?.truncateOrdinal).toBeUndefined()
    expect(paged?.truncateRowId).toBe(703)
    expect(whole?.truncateOrdinal).toBe(1)
  })
})

describe('isSyntheticRendererId covers real hydrated id shapes', () => {
  it('detects fractional-timestamp ids, which is what production actually mints', () => {
    // hermes_state stamps message_timestamp = time.time() and nudges by 1e-6,
    // and the gateway ships it as float(ts) — so live rows carry ids like
    // "1787205245.3426-5-user". An anchored ^\d+- pattern stops dead at the
    // decimal point, so the guard silently missed EVERY hydrated message and
    // only ever caught the integer Date.now() fallback (rows with no
    // timestamp). That let a client-synthesized id reach the gateway, which
    // fails closed on it (4018 "no longer in session history").
    expect(isSyntheticRendererId('1787205245.3426-5-user')).toBe(true)
    expect(isSyntheticRendererId('1787205235.78412-0-assistant')).toBe(true)
    expect(isSyntheticRendererId('1787205235.4622-12-tools')).toBe(true)
  })

  it('still detects the integer shapes it already handled', () => {
    expect(isSyntheticRendererId('1723456789-0-user')).toBe(true)
    expect(isSyntheticRendererId('user-1723456789-0')).toBe(true)
    expect(isSyntheticRendererId('assistant-abc')).toBe(true)
    expect(isSyntheticRendererId('x-synthetic-1')).toBe(true)
  })

  it('does not claim durable or unrelated ids', () => {
    expect(isSyntheticRendererId('456')).toBe(false)
    expect(isSyntheticRendererId('msg_01ABCdef')).toBe(false)
    expect(isSyntheticRendererId(undefined)).toBe(false)
    // A platform id that merely starts with digits must not be swallowed.
    expect(isSyntheticRendererId('1787205245-notarole')).toBe(false)
  })
})

describe('confirm_empty_truncate survives a withheld ordinal', () => {
  it('sets the flag from the first-turn fact when no ordinal is sent', () => {
    // Restoring to the very first user turn leaves the transcript empty, which
    // the gateway refuses (4028) unless confirm_empty_truncate rides along.
    // That flag used to be derived from `truncateOrdinal === 0` — unreachable
    // once the ordinal is withheld (paged window, or the content-rescue path
    // which clears the ordinal unconditionally), so a legitimate restore to
    // turn 0 got refused. The fact travels explicitly now.
    expect(truncateSubmitParams(undefined, undefined, 456, true)).toEqual({
      confirm_truncate: true,
      confirm_empty_truncate: true,
      truncate_before_row_id: 456
    })
  })

  it('does not set it for a non-first turn on the same path', () => {
    expect(truncateSubmitParams(undefined, undefined, 456, false)).toEqual({
      confirm_truncate: true,
      truncate_before_row_id: 456
    })
  })

  it('still sets it from ordinal 0 when the ordinal is sent', () => {
    expect(truncateSubmitParams(0, undefined, 456)).toEqual({
      confirm_truncate: true,
      confirm_empty_truncate: true,
      truncate_before_user_ordinal: 0,
      truncate_before_row_id: 456
    })
  })

  it('never sets it with no address at all', () => {
    expect(truncateSubmitParams(undefined, undefined, undefined, true)).toEqual({})
  })
})
