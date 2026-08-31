/**
 * Release-only updater for the packaged OpenComputer desktop application.
 *
 * This intentionally has no connection to the source/runtime `hermes update`
 * flow. A DMG install cannot safely replace its own Electron bundle by pulling
 * source. electron-updater consumes the signed release artifacts and metadata
 * produced by electron-builder instead.
 *
 * The provider is deliberately baked into package.json's build.publish block,
 * not taken from an environment variable or a user-controlled endpoint. On
 * macOS electron-updater validates the code signature of downloaded apps; a
 * signed, notarized release remains a deployment prerequisite.
 */

export const PACKAGED_UPDATE_REPOSITORY = 'https://github.com/archits01/oc-hermes'

type UpdateEvent = 'error' | 'update-available' | 'update-downloaded' | 'update-not-available'

export interface PackagedAppUpdaterDriver {
  allowDowngrade: boolean
  allowPrerelease: boolean
  autoDownload: boolean
  autoInstallOnAppQuit: boolean
  checkForUpdates(): Promise<unknown>
  on(event: UpdateEvent, listener: (payload?: { version?: string } | Error) => void): unknown
  quitAndInstall(): void
}

export interface PackagedUpdateDialog {
  showMessageBox(options: {
    buttons: string[]
    cancelId: number
    defaultId: number
    detail: string
    message: string
    type: 'error' | 'info'
  }): Promise<{ response: number }>
}

export interface PackagedAppUpdaterOptions {
  dialog: PackagedUpdateDialog
  isPackaged: boolean
  log: (message: string) => void
  platform: NodeJS.Platform
  updater: PackagedAppUpdaterDriver
}

export interface PackagedAppUpdaterController {
  enabled: boolean
  checkForUpdates(options?: { interactive?: boolean }): Promise<boolean>
}

/** Only release DMGs are in scope for the first hosted updater rollout. */
export function shouldEnablePackagedAppUpdater(isPackaged: boolean, platform: NodeJS.Platform): boolean {
  return isPackaged && platform === 'darwin'
}

function describeError(error: unknown): string {
  if (error instanceof Error && error.message) {
    return error.message
  }

  return String(error || 'unknown error')
}

/**
 * Attach the package-updater lifecycle. It checks at launch and downloads a
 * verified update in the background, but never restarts or replaces the app
 * without a visible user confirmation. A manual check gets a truthful
 * "current" / "could not check" dialog; background checks only log those
 * outcomes so normal launches are quiet.
 */
export function configurePackagedAppUpdater(options: PackagedAppUpdaterOptions): PackagedAppUpdaterController {
  const disabled: PackagedAppUpdaterController = {
    enabled: false,
    checkForUpdates: async () => false
  }

  if (!shouldEnablePackagedAppUpdater(options.isPackaged, options.platform)) {
    options.log('[packaged-updater] disabled (only packaged macOS releases are supported)')

    return disabled
  }

  const { dialog, log, updater } = options
  let checkInFlight: Promise<boolean> | null = null
  let interactiveCheckPending = false
  let restartPromptOpen = false

  // Downloading a newer signed application is safe to do in the background.
  // Installing/relaunching must stay user-mediated so it cannot interrupt an
  // active conversation, transfer, or backend teardown.
  updater.autoDownload = true
  updater.autoInstallOnAppQuit = false
  updater.allowPrerelease = false
  updater.allowDowngrade = false

  updater.on('update-available', payload => {
    const version = typeof payload === 'object' && payload && 'version' in payload ? payload.version : undefined
    log(`[packaged-updater] signed OpenComputer update available${version ? `: ${version}` : ''}; downloading`)
  })

  updater.on('update-not-available', payload => {
    const version = typeof payload === 'object' && payload && 'version' in payload ? payload.version : undefined
    log(`[packaged-updater] desktop app is current${version ? ` (${version})` : ''}`)

    if (interactiveCheckPending) {
      interactiveCheckPending = false
      void dialog.showMessageBox({
        buttons: ['OK'],
        cancelId: 0,
        defaultId: 0,
        detail: 'This OpenComputer desktop app is already on the latest hosted release.',
        message: 'OpenComputer is up to date',
        type: 'info'
      })
    }
  })

  updater.on('error', error => {
    const detail = describeError(error)
    log(`[packaged-updater] update check/download failed: ${detail}`)

    if (interactiveCheckPending) {
      interactiveCheckPending = false
      void dialog.showMessageBox({
        buttons: ['OK'],
        cancelId: 0,
        defaultId: 0,
        detail: 'No desktop update was installed. Please try again later or download a signed release manually.',
        message: 'OpenComputer could not check for updates',
        type: 'error'
      })
    }
  })

  updater.on('update-downloaded', payload => {
    const version = typeof payload === 'object' && payload && 'version' in payload ? payload.version : undefined
    log(`[packaged-updater] update downloaded${version ? `: ${version}` : ''}; awaiting restart confirmation`)

    if (restartPromptOpen) {
      return
    }

    restartPromptOpen = true
    void dialog
      .showMessageBox({
        buttons: ['Restart and install', 'Later'],
        cancelId: 1,
        defaultId: 0,
        detail:
          'The signed desktop update has downloaded. Restarting closes OpenComputer; it will install the update and reopen.',
        message: version ? `OpenComputer ${version} is ready` : 'An OpenComputer update is ready',
        type: 'info'
      })
      .then(({ response }) => {
        if (response === 0) {
          // electron-updater performs the platform-specific hand-off. Existing
          // before-quit guards still protect active work and backend cleanup.
          updater.quitAndInstall()
        }
      })
      .catch(error => log(`[packaged-updater] restart prompt failed: ${describeError(error)}`))
      .finally(() => {
        restartPromptOpen = false
      })
  })

  return {
    enabled: true,
    checkForUpdates: ({ interactive = false } = {}) => {
      interactiveCheckPending ||= interactive

      if (checkInFlight) {
        return checkInFlight
      }

      checkInFlight = Promise.resolve(updater.checkForUpdates())
        .then(() => true)
        .catch(error => {
          // Most provider failures also emit `error`; this log is deliberately
          // retained for implementations that only reject the promise.
          log(`[packaged-updater] update check rejected: ${describeError(error)}`)

          if (interactiveCheckPending) {
            interactiveCheckPending = false
            void dialog.showMessageBox({
              buttons: ['OK'],
              cancelId: 0,
              defaultId: 0,
              detail: 'No desktop update was installed. Please try again later or download a signed release manually.',
              message: 'OpenComputer could not check for updates',
              type: 'error'
            })
          }

          return false
        })
        .finally(() => {
          checkInFlight = null
        })

      return checkInFlight
    }
  }
}
