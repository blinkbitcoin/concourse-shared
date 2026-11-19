#!/usr/bin/env node
// Audits a Node.js repo's production dependencies against the npm registry
// bulk advisory endpoint.
//
// npmjs.org retired the legacy audit endpoints that `yarn audit` (v1) and
// `pnpm audit` (< v11) call on 2026-07-15 (they respond 410 Gone). This reads
// the dependency graph straight from the lockfile and posts the package set to
// the replacement /-/npm/v1/security/advisories/bulk endpoint, the same
// request pnpm 11 sends.
//
// Scope matches the retired gate (`pnpm audit --prod`, `yarn audit --groups
// dependencies`): packages reachable from the dependencies and
// optionalDependencies of the project and its workspaces. When that graph
// cannot be resolved completely, every package in the lockfile is audited
// instead, so the scope only ever widens.
//
// Supports pnpm-lock.yaml (lockfileVersion 6 and 9) and yarn.lock (v1); yarn.lock
// wins when both exist, as before. A lockfile that cannot be read or a registry
// answer that is not a complete, well-formed result fails the audit (exit 2):
// a skipped audit must never pass.
//
// Usage: nodejs-audit-advisories.js [--level info|low|moderate|high|critical]
// Exit codes: 0 no advisory at or above the level, 1 advisories found,
// 2 the audit could not run.
"use strict"

const fs = require("fs")
const http = require("http")
const https = require("https")
const path = require("path")
const zlib = require("zlib")

const DEFAULT_URL = "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk"
// Test hook only: the suite points this at a local stub registry.
const BULK_ADVISORY_URL = process.env.AUDIT_ADVISORIES_URL || DEFAULT_URL
const SEVERITY_ORDER = ["info", "low", "moderate", "high", "critical"]
const PROD_GROUPS = ["dependencies", "optionalDependencies"]
const CHUNK_SIZE = 1000
const ATTEMPTS = 3
const RETRY_DELAY_MS = Number(process.env.AUDIT_RETRY_DELAY_MS || 2000)
const TIMEOUT_MS = 60000

class AuditError extends Error {}

function addPackage(packages, name, version) {
  if (!name || !/^[0-9]/.test(version)) return
  if (!packages.has(name)) packages.set(name, new Set())
  packages.get(name).add(version)
}

// ---------------------------------------------------------------------------
// pnpm-lock.yaml

// Parses the block-mapping subset of YAML that pnpm writes into nested
// objects. `{}` becomes an empty object, other scalars and flow collections
// stay strings, sequences and multi-line scalar continuations are skipped.
// Every `---` starts a new document (pnpm 10+ can prepend an env lockfile).
function parseLockYaml(text) {
  const documents = [{}]
  let stack = [{ indent: -1, node: documents[0] }]
  for (const raw of text.split(/\r?\n/)) {
    if (raw === "---") {
      documents.push({})
      stack = [{ indent: -1, node: documents[documents.length - 1] }]
      continue
    }
    const content = raw.trimStart()
    if (content === "" || content.startsWith("#")) continue
    const indent = raw.length - content.length
    while (indent <= stack[stack.length - 1].indent) stack.pop()
    const parent = stack[stack.length - 1].node
    if (parent === null || content === "-" || content.startsWith("- ")) {
      // inside a scalar, a sequence or a sequence item: nothing we read
      stack.push({ indent, node: null })
      continue
    }
    const entry = splitEntry(content)
    if (!entry) throw new AuditError(`cannot parse lockfile line: ${raw.trim()}`)
    const [key, value] = entry
    if (value === "" || value === "{}") {
      parent[key] = {}
      stack.push({ indent, node: parent[key] })
    } else {
      parent[key] = value
      stack.push({ indent, node: null })
    }
  }
  return documents
}

function unquote(value) {
  if (value.length >= 2 && value[0] === "'" && value.endsWith("'")) {
    return value.slice(1, -1).replace(/''/g, "'")
  }
  if (value.length >= 2 && value[0] === '"' && value.endsWith('"')) {
    return JSON.parse(value)
  }
  return value
}

function splitEntry(content) {
  let key
  let rest
  const quote = content[0]
  if (quote === "'" || quote === '"') {
    let end = 1
    while (end < content.length) {
      if (quote === "'" && content[end] === "'" && content[end + 1] === "'") end += 2
      else if (quote === '"' && content[end] === "\\") end += 2
      else if (content[end] === quote) break
      else end += 1
    }
    if (content[end + 1] !== ":") return null
    key = unquote(content.slice(0, end + 1))
    rest = content.slice(end + 2)
    if (rest !== "" && rest[0] !== " ") return null
  } else {
    const separator = content.indexOf(": ")
    if (separator >= 0) {
      key = content.slice(0, separator)
      rest = content.slice(separator + 1)
    } else if (content.endsWith(":")) {
      key = content.slice(0, -1)
      rest = ""
    } else {
      return null
    }
  }
  return [key, unquote(rest.trim())]
}

// keys look like `/name@1.2.3(peer@4.5.6)` (v6) or `name@1.2.3(peer@4.5.6)`
// (v9); split name/version at the last `@` before any peer suffix
function splitPnpmKey(key) {
  const base = key.split("(")[0].replace(/^\//, "")
  const at = base.lastIndexOf("@")
  if (at <= 0) return [null, null]
  return [base.slice(0, at), base.slice(at + 1)]
}

function asMap(value) {
  return value && typeof value === "object" ? value : {}
}

function pnpmPackages(text) {
  const all = new Map()
  const prod = new Map()
  const unresolved = []
  let nodeCount = 0
  for (const doc of parseLockYaml(text)) {
    if (Object.keys(doc).length === 0) continue
    const major = parseInt(String(doc.lockfileVersion || ""), 10)
    if (major !== 6 && major !== 9) {
      throw new AuditError(
        `unsupported pnpm lockfileVersion '${doc.lockfileVersion}' (supported: 6, 9)`,
      )
    }
    const nodes = asMap(major === 9 ? doc.snapshots : doc.packages)
    for (const key of Object.keys(nodes)) {
      nodeCount += 1
      addPackage(all, ...splitPnpmKey(key))
    }
    // a v6 single-project lockfile keeps the project's deps at the top level
    const importers = doc.importers ? asMap(doc.importers) : { ".": doc }

    const resolve = (name, ref) => {
      if (ref.startsWith("link:")) return null // workspace project, audited as an importer
      for (const key of [`${name}@${ref}`, `/${name}@${ref}`, ref, `/${ref}`]) {
        if (Object.prototype.hasOwnProperty.call(nodes, key)) return key
      }
      unresolved.push(`${name}@${ref}`)
      return null
    }

    const queue = []
    const seen = new Set()
    const visit = (deps) => {
      for (const [name, spec] of Object.entries(asMap(deps))) {
        const ref = typeof spec === "string" ? spec : asMap(spec).version
        if (typeof ref !== "string") {
          unresolved.push(name)
          continue
        }
        const key = resolve(name, ref)
        if (key && !seen.has(key)) {
          seen.add(key)
          queue.push(key)
        }
      }
    }
    for (const importer of Object.values(importers)) {
      for (const group of PROD_GROUPS) visit(asMap(importer)[group])
    }
    while (queue.length > 0) {
      const key = queue.shift()
      addPackage(prod, ...splitPnpmKey(key))
      for (const group of PROD_GROUPS) visit(asMap(nodes[key])[group])
    }
  }
  if (nodeCount === 0) {
    throw new AuditError("no packages parsed from pnpm-lock.yaml")
  }
  return { all, prod, unresolved }
}

// ---------------------------------------------------------------------------
// yarn.lock (v1)

function splitSpec(spec) {
  const at = spec.indexOf("@", 1)
  return at < 0 ? [spec, ""] : [spec.slice(0, at), spec.slice(at + 1)]
}

// `alias@npm:real@^1` installs `real`; advisories are keyed by the real name
function realName(spec) {
  const [name, range] = splitSpec(spec)
  return range.startsWith("npm:") ? splitSpec(range.slice(4))[0] : name
}

function splitYarnPair(text) {
  const match = text.match(/^("(?:[^"\\]|\\.)*"|\S+)\s+(.+)$/)
  if (!match) return null
  return [unquote(match[1]), unquote(match[2].trim())]
}

// entries: an unindented `name@range, name@range:` header followed by an
// indented `  version "1.2.3"` line and optional dependency maps
function parseYarnLock(text) {
  if (!/^# yarn lockfile v1\s*$/m.test(text)) {
    throw new AuditError("unsupported yarn.lock (only yarn classic v1 lockfiles are supported)")
  }
  const entries = new Map()
  let current = null
  let depGroup = null
  for (const raw of text.split(/\r?\n/)) {
    if (raw.trim() === "" || raw.startsWith("#")) continue
    if (!raw.startsWith(" ")) {
      if (!raw.endsWith(":")) throw new AuditError(`cannot parse yarn.lock line: ${raw}`)
      current = { name: null, version: null, dependencies: {}, optionalDependencies: {} }
      for (const spec of raw.slice(0, -1).split(/,\s*/).map(unquote)) {
        entries.set(spec, current)
        current.name = current.name || realName(spec)
      }
      depGroup = null
      continue
    }
    if (!current) throw new AuditError(`cannot parse yarn.lock line: ${raw}`)
    if (raw.startsWith("    ")) {
      if (depGroup) {
        const pair = splitYarnPair(raw.trim())
        if (!pair) throw new AuditError(`cannot parse yarn.lock line: ${raw}`)
        current[depGroup][pair[0]] = pair[1]
      }
      continue
    }
    const content = raw.trim()
    depGroup = PROD_GROUPS.includes(content.slice(0, -1)) && content.endsWith(":")
      ? content.slice(0, -1)
      : null
    const version = content.match(/^version "?([^"]+)"?$/)
    if (version) current.version = version[1]
  }
  if (entries.size === 0) throw new AuditError("no packages parsed from yarn.lock")
  return entries
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"))
  } catch (err) {
    return null
  }
}

// Returns the workspace package.json files, or null when a pattern is not a
// plain directory or a single trailing `dir/*`.
function yarnWorkspaces(root, manifest, unresolved) {
  const workspaces = manifest.workspaces
  const patterns = Array.isArray(workspaces) ? workspaces : asMap(workspaces).packages || []
  const manifests = []
  for (const pattern of patterns) {
    const dir = typeof pattern === "string" ? pattern.replace(/\/\*$/, "") : ""
    if (!dir || /[*?[\]{}!]/.test(dir)) {
      unresolved.push(`workspace pattern ${pattern}`)
      continue
    }
    const dirs = pattern.endsWith("/*")
      ? (fs.existsSync(path.join(root, dir)) ? fs.readdirSync(path.join(root, dir)) : [])
          .map((child) => path.join(dir, child))
      : [dir]
    for (const candidate of dirs) {
      const file = path.join(root, candidate, "package.json")
      if (!fs.existsSync(file)) {
        if (!pattern.endsWith("/*")) unresolved.push(`workspace ${candidate}`)
        continue
      }
      const workspace = readJson(file)
      if (!workspace) unresolved.push(`workspace ${candidate}`)
      else manifests.push(workspace)
    }
  }
  return manifests
}

function yarnPackages(root, text) {
  const entries = parseYarnLock(text)
  const all = new Map()
  for (const entry of entries.values()) addPackage(all, entry.name, entry.version)

  const prod = new Map()
  const unresolved = []
  const manifest = readJson(path.join(root, "package.json"))
  if (!manifest) {
    unresolved.push("package.json")
    return { all, prod, unresolved }
  }
  const manifests = [manifest, ...yarnWorkspaces(root, manifest, unresolved)]
  const workspaceNames = new Set(manifests.map((m) => m.name).filter(Boolean))

  const queue = []
  const seen = new Set()
  const visit = (deps, fromWorkspace) => {
    for (const [name, range] of Object.entries(asMap(deps))) {
      const entry = entries.get(`${name}@${range}`)
      if (!entry) {
        if (!(fromWorkspace && workspaceNames.has(name))) unresolved.push(`${name}@${range}`)
        continue
      }
      if (!seen.has(entry)) {
        seen.add(entry)
        queue.push(entry)
      }
    }
  }
  for (const m of manifests) for (const group of PROD_GROUPS) visit(m[group], true)
  while (queue.length > 0) {
    const entry = queue.shift()
    addPackage(prod, entry.name, entry.version)
    for (const group of PROD_GROUPS) visit(entry[group], false)
  }
  return { all, prod, unresolved }
}

// ---------------------------------------------------------------------------

function collectPackages(root) {
  let source
  let result
  if (fs.existsSync(path.join(root, "yarn.lock"))) {
    source = "yarn.lock"
    result = yarnPackages(root, fs.readFileSync(path.join(root, source), "utf8"))
  } else if (fs.existsSync(path.join(root, "pnpm-lock.yaml"))) {
    source = "pnpm-lock.yaml"
    result = pnpmPackages(fs.readFileSync(path.join(root, source), "utf8"))
  } else {
    throw new AuditError("no yarn.lock or pnpm-lock.yaml found")
  }
  if (result.unresolved.length > 0) {
    const shown = result.unresolved.slice(0, 10).join(", ")
    const more = result.unresolved.length > 10 ? ` and ${result.unresolved.length - 10} more` : ""
    console.error(
      `could not resolve the production dependency graph (${shown}${more}); ` +
        `auditing every package in ${source} instead`,
    )
    return { source, scope: "all", packages: result.all }
  }
  return { source, scope: "production", packages: result.prod }
}

function decodeBody(buffer) {
  if (buffer[0] === 0x1f && buffer[1] === 0x8b) return zlib.gunzipSync(buffer)
  return buffer
}

function postOnce(body) {
  const client = BULK_ADVISORY_URL.startsWith("http:") ? http : https
  return new Promise((resolve, reject) => {
    const request = client.request(
      BULK_ADVISORY_URL,
      {
        method: "POST",
        headers: { "content-type": "application/json", accept: "application/json" },
      },
      (response) => {
        const chunks = []
        response.on("data", (chunk) => chunks.push(chunk))
        response.on("error", reject)
        response.on("end", () =>
          resolve({ status: response.statusCode, body: Buffer.concat(chunks) }),
        )
      },
    )
    request.on("error", reject)
    request.setTimeout(TIMEOUT_MS, () => request.destroy(new Error("timeout")))
    request.end(JSON.stringify(body))
  })
}

function validateAdvisories(parsed) {
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new AuditError("advisory response is not a JSON object")
  }
  for (const [name, list] of Object.entries(parsed)) {
    if (!Array.isArray(list)) throw new AuditError(`advisories for ${name} are not a list`)
    for (const advisory of list) {
      if (!advisory || !SEVERITY_ORDER.includes(advisory.severity)) {
        throw new AuditError(
          `advisory for ${name} has unknown severity '${advisory && advisory.severity}'`,
        )
      }
    }
  }
  return parsed
}

function parseAdvisories(buffer) {
  let parsed
  try {
    parsed = JSON.parse(decodeBody(buffer).toString("utf8"))
  } catch (err) {
    throw new AuditError(`unreadable advisory response: ${err.message}`)
  }
  return validateAdvisories(parsed)
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

// Network errors, timeouts, 429 and 5xx are retried; any other status
// (a retired endpoint answers 410) fails at once.
async function post(body) {
  let failure
  for (let attempt = 1; attempt <= ATTEMPTS; attempt += 1) {
    let response = null
    try {
      response = await postOnce(body)
    } catch (err) {
      failure = err.message
    }
    if (response) {
      if (response.status === 200) return parseAdvisories(response.body)
      failure = `HTTP ${response.status}: ${response.body.toString("utf8").slice(0, 200)}`
      if (response.status !== 429 && response.status < 500) break
    }
    if (attempt < ATTEMPTS) {
      console.error(`advisory request failed (${failure}), retrying`)
      await sleep(RETRY_DELAY_MS * attempt)
    }
  }
  throw new AuditError(`advisory request failed: ${failure}`)
}

async function fetchAdvisories(packages) {
  const advisories = {}
  const names = [...packages.keys()].sort()
  for (let start = 0; start < names.length; start += CHUNK_SIZE) {
    const chunk = {}
    for (const name of names.slice(start, start + CHUNK_SIZE)) {
      chunk[name] = [...packages.get(name)].sort()
    }
    // names are unique across chunks, so a plain merge loses nothing
    Object.assign(advisories, await post(chunk))
  }
  return advisories
}

function parseLevel(argv) {
  let level = "high"
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--level" && i + 1 < argv.length) level = argv[(i += 1)]
    else if (argv[i].startsWith("--level=")) level = argv[i].slice("--level=".length)
    else throw new AuditError(`unknown argument '${argv[i]}'`)
  }
  if (!SEVERITY_ORDER.includes(level)) throw new AuditError(`unknown level '${level}'`)
  return level
}

async function main(argv) {
  const level = parseLevel(argv)
  const { source, scope, packages } = collectPackages(process.cwd())
  const versions = [...packages.values()].reduce((sum, set) => sum + set.size, 0)
  console.error(`auditing ${versions} ${scope} package versions from ${source} at level '${level}'`)
  if (BULK_ADVISORY_URL !== DEFAULT_URL) {
    console.error(`using advisory endpoint override ${BULK_ADVISORY_URL}`)
  }
  if (packages.size === 0) return 0

  const advisories = await fetchAdvisories(packages)
  const matching = []
  for (const [name, list] of Object.entries(advisories)) {
    for (const advisory of list) {
      if (SEVERITY_ORDER.indexOf(advisory.severity) >= SEVERITY_ORDER.indexOf(level)) {
        matching.push([name, advisory])
      }
    }
  }
  if (matching.length === 0) {
    console.log(`no advisories at or above '${level}'`)
    return 0
  }

  matching.sort(
    (a, b) =>
      SEVERITY_ORDER.indexOf(b[1].severity) - SEVERITY_ORDER.indexOf(a[1].severity) ||
      a[0].localeCompare(b[0]),
  )
  const lines = [`audit found ${matching.length} advisories at or above '${level}':`]
  for (const [name, advisory] of matching) {
    lines.push("", `${advisory.severity}: ${name}`, `  title: ${advisory.title}`)
    lines.push(`  installed versions: ${[...(packages.get(name) || [])].sort().join(", ")}`)
    if (advisory.vulnerable_versions) {
      lines.push(`  vulnerable versions: ${advisory.vulnerable_versions}`)
    }
    if (advisory.url) lines.push(`  url: ${advisory.url}`)
  }
  console.log(lines.join("\n"))
  return 1
}

if (require.main === module) {
  // set exitCode instead of calling process.exit so piped output is flushed
  main(process.argv.slice(2)).then(
    (code) => {
      process.exitCode = code
    },
    (err) => {
      console.error(`audit could not run: ${err.message}`)
      process.exitCode = 2
    },
  )
}
