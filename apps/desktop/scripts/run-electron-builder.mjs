// Resolve electronDist at runtime (#38673, #47917): electron-builder 26.8.x can
// re-unpack a broken Electron.app; reusing the installed dist dodges that.
// npm workspace hoisting is non-deterministic — require.resolve finds electron
// wherever it landed. Dist present → -c.electronDist=<abs>/dist; absent → let
// electron-builder fetch via @electron/get (electronVersion + ELECTRON_MIRROR).

import fs from "node:fs"
import path from "node:path"
import { spawnSync } from "node:child_process"
import { createRequire } from "node:module"

const require = createRequire(import.meta.url)

function electronDistDir() {
  try {
    return path.join(path.dirname(require.resolve("electron/package.json")), "dist")
  } catch {
    return null
  }
}

function distBinary(dist) {
  if (process.platform === "darwin") {
    return path.join(dist, "Electron.app", "Contents", "MacOS", "Electron")
  }
  if (process.platform === "win32") {
    return path.join(dist, "electron.exe")
  }
  return path.join(dist, "electron")
}

function electronBuilderCli() {
  const pkgJson = require.resolve("electron-builder/package.json")
  const bin = require(pkgJson).bin
  const rel = typeof bin === "string" ? bin : bin["electron-builder"]
  return path.join(path.dirname(pkgJson), rel)
}

function targetIsCrossPlatform(argv) {
  const wantsMac = argv.includes("--mac")
  const wantsWin = argv.includes("--win")
  const wantsLinux = argv.includes("--linux")
  if (wantsMac && process.platform !== "darwin") return true
  if (wantsWin && process.platform !== "win32") return true
  if (wantsLinux && process.platform !== "linux") return true
  return false
}

const dist = electronDistDir()
const args = []
const argv = process.argv.slice(2)
if (dist && fs.existsSync(distBinary(dist)) && !targetIsCrossPlatform(argv)) {
  args.push(`-c.electronDist=${dist}`)
} else {
  console.warn(
    "[run-electron-builder] no same-platform electron dist; electron-builder will fetch " +
      "via @electron/get (electronVersion + ELECTRON_MIRROR)."
  )
}
args.push(...argv)

const result = spawnSync(process.execPath, [electronBuilderCli(), ...args], {
  stdio: "inherit",
})
if (result.error) {
  console.error(`[run-electron-builder] spawn failed: ${result.error.message}`)
  process.exit(1)
}
process.exit(result.status == null ? 1 : result.status)
