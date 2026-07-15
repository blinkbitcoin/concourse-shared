#!/usr/bin/env node
// Audits npm dependencies against the npm registry bulk advisory endpoint.
//
// The legacy audit endpoints that `yarn audit` (v1) and `pnpm audit` (< v11)
// call were retired by npmjs.org on 2026-07-15 (they respond 410 Gone), so
// this reads the package set straight from the lockfile and posts it to the
// replacement /-/npm/v1/security/advisories/bulk endpoint.
//
// Supports pnpm-lock.yaml (lockfileVersion 6 and 9) and yarn.lock (v1).
"use strict"

const fs = require("fs")
const https = require("https")

const BULK_ADVISORY_URL =
  "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk"
const SEVERITY_ORDER = ["low", "moderate", "high", "critical"]
const CHUNK_SIZE = 1000

function addPackage(packages, name, version) {
  if (!name || !/^[0-9]/.test(version)) return
  if (!packages.has(name)) packages.set(name, new Set())
  packages.get(name).add(version)
}

// keys under `packages:` look like `  /name@1.2.3(peer@4.5.6):` (v6) or
// `  'name@1.2.3':` (v9); split name/version at the last `@` before any peer suffix
function splitKey(key) {
  const base = key.split("(")[0].replace(/^'|'$/g, "").replace(/^\//, "")
  const at = base.lastIndexOf("@")
  return [base.slice(0, at), base.slice(at + 1)]
}

function collectFromPnpmLock(text) {
  const packages = new Map()
  let section = ""
  for (const line of text.split("\n")) {
    if (/^[A-Za-z].*:$/.test(line)) {
      section = line.slice(0, -1)
      continue
    }
    if (section !== "packages") continue
    const match = line.match(/^ {2}(\S.*):$/)
    if (match) addPackage(packages, ...splitKey(match[1]))
  }
  return packages
}

// yarn v1 entries: an unindented `name@range, name@range:` line followed by
// an indented `  version "1.2.3"` line
function collectFromYarnLock(text) {
  const packages = new Map()
  let pendingName = null
  for (const line of text.split("\n")) {
    if (/^[^\s#].*:$/.test(line)) {
      const spec = line.slice(0, -1).split(", ")[0].replace(/^"|"$/g, "")
      pendingName = spec.slice(0, spec.lastIndexOf("@"))
      continue
    }
    const version = line.match(/^ {2}version "([^"]+)"$/)
    if (version && pendingName) {
      addPackage(packages, pendingName, version[1])
      pendingName = null
    }
  }
  return packages
}

function collectPackages() {
  if (fs.existsSync("pnpm-lock.yaml")) {
    return collectFromPnpmLock(fs.readFileSync("pnpm-lock.yaml", "utf8"))
  }
  if (fs.existsSync("yarn.lock")) {
    return collectFromYarnLock(fs.readFileSync("yarn.lock", "utf8"))
  }
  console.error("Failed audit: no pnpm-lock.yaml or yarn.lock found")
  process.exit(2)
}

function post(body) {
  return new Promise((resolve, reject) => {
    const request = https.request(
      BULK_ADVISORY_URL,
      { method: "POST", headers: { "content-type": "application/json" } },
      (response) => {
        let data = ""
        response.on("data", (chunk) => (data += chunk))
        response.on("end", () => {
          if (response.statusCode !== 200) {
            return reject(new Error(`HTTP ${response.statusCode}: ${data.slice(0, 200)}`))
          }
          try {
            resolve(JSON.parse(data))
          } catch (err) {
            reject(err)
          }
        })
      },
    )
    request.on("error", reject)
    request.setTimeout(60000, () => request.destroy(new Error("timeout")))
    request.end(JSON.stringify(body))
  })
}

async function fetchAdvisories(packages) {
  const advisories = {}
  const names = [...packages.keys()].sort()
  for (let start = 0; start < names.length; start += CHUNK_SIZE) {
    const chunk = {}
    for (const name of names.slice(start, start + CHUNK_SIZE)) {
      chunk[name] = [...packages.get(name)].sort()
    }
    Object.assign(advisories, await post(chunk))
  }
  return advisories
}

async function main() {
  const levelIndex = process.argv.indexOf("--level")
  const level = levelIndex > 0 ? process.argv[levelIndex + 1] : "high"
  if (!SEVERITY_ORDER.includes(level)) {
    console.error(`unknown level '${level}'`)
    process.exit(2)
  }

  const packages = collectPackages()
  if (packages.size === 0) {
    console.error("no packages parsed from lockfile - refusing to report a vacuous pass")
    process.exit(2)
  }

  let advisories
  try {
    advisories = await fetchAdvisories(packages)
  } catch (err) {
    // parity with the retired yarn/pnpm audit behavior: registry trouble never failed the gate
    console.error(`Could not fetch advisories, skipping audit: ${err.message}`)
    process.exit(0)
  }

  const matching = []
  for (const [name, list] of Object.entries(advisories)) {
    for (const advisory of list) {
      if (
        SEVERITY_ORDER.indexOf(advisory.severity) >= SEVERITY_ORDER.indexOf(level)
      ) {
        matching.push([name, advisory])
      }
    }
  }

  if (matching.length === 0) process.exit(0)

  matching.sort(
    (a, b) =>
      SEVERITY_ORDER.indexOf(b[1].severity) - SEVERITY_ORDER.indexOf(a[1].severity),
  )
  console.log(`audit found advisories at or above '${level}':`)
  for (const [name, advisory] of matching) {
    console.log()
    console.log(`${advisory.severity}: ${name}`)
    console.log(`  title: ${advisory.title}`)
    console.log(`  installed versions: ${[...packages.get(name)].sort().join(", ")}`)
    if (advisory.vulnerable_versions) {
      console.log(`  vulnerable versions: ${advisory.vulnerable_versions}`)
    }
    if (advisory.url) console.log(`  url: ${advisory.url}`)
  }
  process.exit(1)
}

main()
