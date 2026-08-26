/**
 * Remote thin clients do not own a local always-listening wake-word surface.
 * The backend runs on another machine, so exposing or auto-arming "Hey Hermes"
 * in the local composer is misleading and can open the Mac microphone without
 * a useful local detector. Local Desktop installations keep the feature.
 */
export function wakeWordAllowedForConnection(mode: string | null | undefined): boolean {
  return mode !== 'remote'
}
