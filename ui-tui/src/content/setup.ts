import { messages } from '../i18n/runtime.js'
import type { PanelSection } from '../types.js'

export const setupRequiredTitle = (): string => messages().content.setup.title

<<<<<<< HEAD
export const buildSetupRequiredSections = (): PanelSection[] => [
  {
    text: 'OpenComputer needs a model provider before the TUI can start a session.'
  },
  {
    rows: [
      ['/model', 'configure provider + model in-place'],
      ['/setup', 'run full first-time setup wizard in-place'],
      ['Ctrl+C', 'exit and run `hermes setup` manually']
    ],
    title: 'Actions'
  }
]
=======
export const buildSetupRequiredSections = (): PanelSection[] => {
  const s = messages().content.setup

  return [
    {
      text: s.intro
    },
    {
      rows: [
        ['/setup', s.setupRow],
        ['/model', s.modelRow],
        ['Ctrl+C', s.exitRow]
      ],
      title: s.actions
    },
    {
      text: s.footer
    }
  ]
}
>>>>>>> upstream/main
