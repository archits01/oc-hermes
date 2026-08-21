import assert from 'node:assert/strict'

import { EventEmitter } from 'node:events'

import { describe, expect, test, vi } from 'vitest'

import {
  configurePackagedAppUpdater,
  PACKAGED_UPDATE_REPOSITORY,
  shouldEnablePackagedAppUpdater,
  type PackagedAppUpdaterDriver
} from './packaged-app-updater'

class FakeUpdater extends EventEmitter implements PackagedAppUpdaterDriver {
  allowDowngrade = true
  allowPrerelease = true
  autoDownload = false
  autoInstallOnAppQuit = true
  checks = 0
  quitAndInstall = vi.fn()

  async checkForUpdates() {
    this.checks += 1
  }
}

async function settle() {
  await Promise.resolve()
  await Promise.resolve()
}

describe('packaged OpenComputer updater', () => {
  test('only enables for packaged macOS applications', () => {
    assert.equal(shouldEnablePackagedAppUpdater(false, 'darwin'), false)
    assert.equal(shouldEnablePackagedAppUpdater(true, 'linux'), false)
    assert.equal(shouldEnablePackagedAppUpdater(true, 'win32'), false)
    assert.equal(shouldEnablePackagedAppUpdater(true, 'darwin'), true)
    assert.equal(PACKAGED_UPDATE_REPOSITORY, 'https://github.com/archits01/oc-hermes')
  })

  test('does not touch an updater on unsupported installs', async () => {
    const updater = new FakeUpdater()
    const log = vi.fn()
    const dialog = { showMessageBox: vi.fn() }
    const controller = configurePackagedAppUpdater({ dialog, isPackaged: false, log, platform: 'darwin', updater })

    assert.equal(controller.enabled, false)
    assert.equal(await controller.checkForUpdates({ interactive: true }), false)
    assert.equal(updater.checks, 0)
    expect(log).toHaveBeenCalledWith(expect.stringContaining('disabled'))
  })

  test('uses background download but requires a visible restart confirmation', async () => {
    const updater = new FakeUpdater()
    const log = vi.fn()
    const dialog = { showMessageBox: vi.fn().mockResolvedValue({ response: 0 }) }
    const controller = configurePackagedAppUpdater({ dialog, isPackaged: true, log, platform: 'darwin', updater })

    assert.equal(controller.enabled, true)
    assert.equal(updater.autoDownload, true)
    assert.equal(updater.autoInstallOnAppQuit, false)
    assert.equal(updater.allowPrerelease, false)
    assert.equal(updater.allowDowngrade, false)

    updater.emit('update-available', { version: '0.17.1' })
    updater.emit('update-downloaded', { version: '0.17.1' })
    await settle()

    expect(dialog.showMessageBox).toHaveBeenCalledWith(
      expect.objectContaining({ buttons: ['Restart and install', 'Later'], message: 'OpenComputer 0.17.1 is ready' })
    )
    expect(updater.quitAndInstall).toHaveBeenCalledTimes(1)
    expect(log).toHaveBeenCalledWith(expect.stringContaining('awaiting restart confirmation'))
  })

  test('manual check reports current state and coalesces concurrent checks', async () => {
    const updater = new FakeUpdater()
    const log = vi.fn()
    const dialog = { showMessageBox: vi.fn().mockResolvedValue({ response: 0 }) }
    const controller = configurePackagedAppUpdater({ dialog, isPackaged: true, log, platform: 'darwin', updater })

    const first = controller.checkForUpdates({ interactive: true })
    const second = controller.checkForUpdates({ interactive: true })
    assert.equal(first, second)
    await first
    assert.equal(updater.checks, 1)

    updater.emit('update-not-available', { version: '0.17.0' })
    await settle()
    expect(dialog.showMessageBox).toHaveBeenCalledWith(expect.objectContaining({ message: 'OpenComputer is up to date' }))
  })
})
