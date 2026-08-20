/**
 * Window translucency — the one place the main process and the renderer agree
 * on what the setting means.
 *
 * One lever, 0–100 (0 = off, the default). Two modes decide HOW the desktop
 * shows through:
 *
 * - 'clear' — the main process maps the lever to native window opacity
 *   (`setOpacity`), so the whole window fades, text included. macOS + Windows;
 *   `setOpacity` is a no-op on Linux.
 * - 'glass' — macOS only. The window stays fully opaque at the native level and
 *   the renderer thins its page surfaces instead, letting the vibrancy material
 *   every chat window already carries read as a matte blur while text keeps
 *   full contrast.
 *
 * The renderer owns the value and mirrors it to main over IPC; main persists it
 * so a cold launch can apply it at window creation, before the renderer reports
 * anything.
 */

export type TranslucencyMode = 'clear' | 'glass'

/**
 * macOS vibrancy materials offered as glass "frost" levels, ordered sheer →
 * heavy. macOS exposes no blur-radius knob (VibrancyOptions is only an
 * animation duration), so the material IS the frost control: each maps to a
 * different NSVisualEffectView material with its own luminance lift.
 *
 * Curated by pixel census on macOS 26 (one window, visualEffectState pinned
 * to 'active', cycling all 14 materials over the same wallpaper): the 14
 * collapse to 9 distinct looks (sidebar≡hud, window≡fullscreen-ui,
 * tooltip≡content≡under-window≡under-page). These four are the ladder with
 * the widest separations that stay distinct in BOTH appearances — dark lum
 * 26/63/84/127, light lum 217/233/254/242. sidebar/hud sit 9 lum from
 * under-window when focused and collapse INTO it when unfocused, which
 * shipped as two indistinguishable picker options once — don't re-add them.
 */
export const GLASS_MATERIALS = ['under-window', 'popover', 'titlebar', 'header'] as const

export type GlassMaterial = (typeof GLASS_MATERIALS)[number]

export const DEFAULT_GLASS_MATERIAL: GlassMaterial = 'under-window'

/**
 * Where the glass field lives. 'window' thins every field surface; 'sidebar'
 * is the Finder shape — glass rail, opaque content column. The scope is a
 * renderer concern (which surfaces thin); the main process only persists and
 * echoes it.
 */
export const GLASS_SCOPES = ['window', 'sidebar'] as const

export type GlassScope = (typeof GLASS_SCOPES)[number]

export const DEFAULT_GLASS_SCOPE: GlassScope = 'window'

export interface TranslucencyState {
  intensity: number
  mode: TranslucencyMode
  material: GlassMaterial
  scope: GlassScope
}

/**
 * The half of the state that is scoped to the light/dark appearance.
 *
 * A tint that reads as a whisper over a dark palette is a milky sheet over a
 * light one, so one shared number cannot serve both — the same setting has to
 * mean a different amount in each appearance. `mode` stays global: clear vs
 * glass is a choice about the window, not about the palette.
 */
export type TranslucencyValues = Omit<TranslucencyState, 'mode'>

export type Appearance = 'light' | 'dark'

/**
 * Per-appearance defaults, per platform family. Glass ships ON: it is the
 * better-looking half of the feature, and a lever that starts at zero is a
 * feature nobody finds.
 *
 * The two platforms need different numbers because the lever means different
 * things behind them. `intensity` is how much of the theme tint the renderer
 * REMOVES (see `glassSurfaceKeep`), and what shows through underneath is a
 * native material with its own weight:
 *
 * - macOS vibrancy is genuinely sheer, so the tint has to come most of the way
 *   off before the desktop reads at all. Light leans heavy — a bright desktop
 *   behind a bright window needs real thinning before the field separates —
 *   with a single point of fade so the window edge reads as glass rather than
 *   as paint. Dark takes far less: a dark field already separates, and the
 *   tint that flatters light would smother it.
 * - Windows acrylic composites its OWN tint in DWM before the page is drawn,
 *   so the renderer's tint stacks on top of a backdrop that is already doing
 *   the work. The same numbers that read as frost on a Mac read as a washed
 *   sheet here; these stay low and let DWM carry it. Fade stays at zero —
 *   `setOpacity` over a system backdrop dims the composited result rather than
 *   deepening it.
 *
 * Both sit on the frost each platform renders best: 'header' and 'titlebar'
 * are macOS-only rungs (on Windows they collapse onto mica — see
 * `glassMaterialsFor`), while 'under-window' is the acrylic rung, the live
 * blur closest to what macOS calls under-window.
 */
const DEFAULT_VALUES: Record<'mac' | 'windows', Record<Appearance, TranslucencyValues>> = {
  mac: {
    light: { intensity: 66, fade: 1, material: 'header', scope: 'window' },
    dark: { intensity: 22, fade: 0, material: 'titlebar', scope: 'window' }
  },
  windows: {
    light: { intensity: 20, fade: 0, material: 'under-window', scope: 'window' },
    dark: { intensity: 5, fade: 0, material: 'under-window', scope: 'window' }
  }
}

/**
 * The untouched values for an appearance on this platform. Linux never reaches
 * here — translucency is unsupported there, so nothing resolves.
 */
export function defaultTranslucencyValues(appearance: Appearance, isWindows: boolean): TranslucencyValues {
  return DEFAULT_VALUES[isWindows ? 'windows' : 'mac'][appearance]
}

/**
 * The renderer's book of translucency settings.
 *
 * `base` is the shared rung: a value the user set before appearances were
 * split (a migrated v1 state), or one they have never touched. An appearance
 * slot only carries the keys edited WHILE that appearance was painted, so
 * changing the tint in light mode leaves dark's alone and an untouched dark
 * still inherits whatever base says. That is the ladder — appearance over base
 * over default, per key, so \"unset\" keeps carrying over.
 *
 * The book is renderer-owned. The main process is handed the RESOLVED state
 * (see `resolveTranslucency`) because a window's backing, vibrancy and opacity
 * only ever concern the appearance actually on screen.
 */
export interface TranslucencyBook {
  mode: TranslucencyMode
  base: Partial<TranslucencyValues>
  light: Partial<TranslucencyValues>
  dark: Partial<TranslucencyValues>
}

export const TRANSLUCENCY_MIN = 0
export const TRANSLUCENCY_MAX = 100

/** Renderer slider granularity. Main accepts any integer in range. */
export const TRANSLUCENCY_STEP = 1

/** Most see-through clear setting — floored so it stays usable, not invisible. */
export const TRANSLUCENCY_OPACITY_FLOOR = 0.3

/**
 * Exponent for the clear intensity → opacity ramp. 1 is a linear ramp, which
 * spends the whole readable band (opacity ≳ 0.95) in the first few percent of
 * the lever. 2 holds that band across roughly the first third while leaving
 * both endpoints bit-identical to the linear ramp.
 */
export const TRANSLUCENCY_CURVE = 2

export function clampIntensity(value: unknown): number {
  const n = Math.round(Number(value))

  return Number.isFinite(n) ? Math.min(TRANSLUCENCY_MAX, Math.max(TRANSLUCENCY_MIN, n)) : TRANSLUCENCY_MIN
}

/**
 * Glass rides on the macOS vibrancy material, so it is macOS-only and 'clear'
 * is the fallback everywhere else.
 *
 * With no mode recorded, macOS gets glass — it is the better-looking half of
 * the feature and the one worth finding, and pre-selecting it costs a fresh
 * profile nothing because the intensity still starts at 0 (the whole feature
 * is off until the user raises the lever). `legacyIntensity` is the escape
 * hatch: a profile that already carries a NON-ZERO intensity but no mode
 * predates this setting and has been rendering as clear all along, so it keeps
 * rendering as clear. Flipping a window someone already tuned is the one thing
 * a default must not do.
 */
export function normalizeMode(value: unknown, isMac: boolean, legacyIntensity = 0): TranslucencyMode {
  if (!isMac) {
    return 'clear'
  }

  if (value === 'glass' || value === 'clear') {
    return value
  }

  return legacyIntensity > 0 ? 'clear' : 'glass'
}

/** Unknown or unsupported values fall back to the default material. */
export function normalizeMaterial(value: unknown): GlassMaterial {
  return GLASS_MATERIALS.includes(value as GlassMaterial) ? (value as GlassMaterial) : DEFAULT_GLASS_MATERIAL
}

/** Unknown or unsupported values fall back to whole-window glass. */
export function normalizeScope(value: unknown): GlassScope {
  return GLASS_SCOPES.includes(value as GlassScope) ? (value as GlassScope) : DEFAULT_GLASS_SCOPE
}

/** Parse a persisted translucency.json / IPC payload into a safe state. */
export function normalizeState(payload: unknown, isMac: boolean): TranslucencyState {
  const record = payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {}
  const intensity = clampIntensity(record.intensity)

  return {
    intensity,
    mode: normalizeMode(record.mode, isMac, intensity),
    material: normalizeMaterial(record.material),
    scope: normalizeScope(record.scope)
  }
}

/**
 * Native window opacity for a state. Glass never fades the native window — its
 * see-through effect is painted by the renderer over the vibrancy material.
 */
export function windowOpacityFor({ intensity, mode }: TranslucencyState): number {
  if (mode === 'glass') {
    return 1
  }

  const ratio = clampIntensity(intensity) / TRANSLUCENCY_MAX

  return 1 - (1 - TRANSLUCENCY_OPACITY_FLOOR) * Math.pow(ratio, TRANSLUCENCY_CURVE)
}

/**
 * Whether glass is visually active. Both processes branch on this: main to
 * decide a window's backing, the renderer to decide whether to thin surfaces.
 */
export function glassActive({ intensity, mode }: TranslucencyState): boolean {
  return mode === 'glass' && intensity > 0
}

/**
 * Percent of the surface tint the renderer KEEPS at a given intensity. Linear
 * to zero: at 100 the tint is fully gone — bare vibrancy glass — so the slider
 * spans the whole range from opaque theme to untinted blur. Text and cards
 * keep their own opaque tokens for contrast; only the field surfaces thin.
 */
export function glassSurfaceKeep(intensity: number): number {
  return TRANSLUCENCY_MAX - clampIntensity(intensity)
}

/**
 * The vibrancy material a chat window should carry. 'sidebar' is the
 * long-standing default the titlebar band was designed against; glass mode
 * swaps the whole window onto the user's chosen material (setVibrancy is
 * cheap and animatable at runtime, unlike the backing).
 */
export function vibrancyFor(state: TranslucencyState): GlassMaterial | 'sidebar' {
  return glassActive(state) ? state.material : 'sidebar'
}
