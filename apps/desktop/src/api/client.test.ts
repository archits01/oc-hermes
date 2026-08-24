import { afterEach, describe, expect, test } from 'vitest'

import { HermesGateway } from './client'

const originalWebSocket = globalThis.WebSocket

class FakeWebSocket {
  static readonly CLOSED = 3
  static readonly OPEN = 1
  static instances: FakeWebSocket[] = []

  readonly listeners = new Map<string, Array<{ callback: EventListener; once: boolean }>>()
  readyState = 0

  constructor(_url: string) {
    FakeWebSocket.instances.push(this)
  }

  addEventListener(type: string, callback: EventListener, options?: AddEventListenerOptions | boolean) {
    const once = typeof options === 'object' && Boolean(options?.once)
    const listeners = this.listeners.get(type) ?? []
    listeners.push({ callback, once })
    this.listeners.set(type, listeners)
  }

  removeEventListener(type: string, callback: EventListener) {
    this.listeners.set(type, (this.listeners.get(type) ?? []).filter(listener => listener.callback !== callback))
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED
    this.emit('close')
  }

  emit(type: string) {
    const listeners = [...(this.listeners.get(type) ?? [])]

    for (const listener of listeners) {
      listener.callback(new Event(type))

      if (listener.once) {
        this.removeEventListener(type, listener.callback)
      }
    }
  }

  open() {
    this.readyState = FakeWebSocket.OPEN
    this.emit('open')
  }

  send() {}
}

function installFakeWebSocket() {
  FakeWebSocket.instances = []
  Object.defineProperty(globalThis, 'WebSocket', { configurable: true, value: FakeWebSocket })
}

afterEach(() => {
  Object.defineProperty(globalThis, 'WebSocket', { configurable: true, value: originalWebSocket })
})

describe('OpenComputer gateway error surface', () => {
  test('uses OpenComputer wording when a connection attempt errors', async () => {
    installFakeWebSocket()
    const gateway = new HermesGateway()
    const connecting = gateway.connect('ws://gateway.example.test/api/ws')

    FakeWebSocket.instances[0].emit('error')

    await expect(connecting).rejects.toThrow('Could not connect to OpenComputer gateway')
  })

  test('uses OpenComputer wording for a request without an open connection', async () => {
    const gateway = new HermesGateway()

    await expect(gateway.request('profiles.list')).rejects.toThrow('OpenComputer gateway is not connected')
  })

  test('uses OpenComputer wording when close rejects a pending request', async () => {
    installFakeWebSocket()
    const gateway = new HermesGateway()
    const connecting = gateway.connect('ws://gateway.example.test/api/ws')
    FakeWebSocket.instances[0].open()
    await connecting

    const pending = gateway.request('profiles.list')
    gateway.close()

    await expect(pending).rejects.toThrow('OpenComputer gateway connection closed')
  })
})
