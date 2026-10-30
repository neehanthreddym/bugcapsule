// Local import/selection only. Never log or persist captures or API responses.
export const API_BASE = 'http://127.0.0.1:8000'
export const MAX_IMPORT_BYTES = 16 * 1024 * 1024
const REQUEST_TIMEOUT_MS = 30_000

const ERROR_MESSAGES = {
  invalid_har: 'This file is not a supported, valid HAR. Choose another HAR file.',
  unsupported_media_type: 'The local backend requires an unencoded JSON HAR file.',
  payload_too_large: 'The HAR exceeds the 16 MiB import limit. Choose a smaller file.',
  invalid_selection: 'Select 1–10 unique requests from one API origin, then try again.',
  capture_not_found: 'This capture expired or was removed. Import the HAR again.',
  capture_capacity: 'The local backend already holds five captures. Clear one or wait for the 30-minute expiry.',
  invalid_host: 'The backend accepts loopback hosts only. Use the local development servers.',
  invalid_origin: 'Open the frontend at http://localhost:5173 or http://127.0.0.1:5173.',
  invalid_request: 'The local backend rejected the request. Try again.',
  not_found: 'The local API route is unavailable. Check that the BugCapsule backend is running.',
  method_not_allowed: 'The local API does not support this request. Check the backend version.',
  internal_error: 'The local backend could not complete the request. Try again.',
  unavailable: 'Could not reach the local backend. Start it at http://127.0.0.1:8000 and use frontend port 5173.',
  timeout: 'The local backend did not respond within 30 seconds. Try again; an import may have reached the backend.',
  invalid_response: 'The local backend returned an unexpected response. Try again.',
}

export class ApiError extends Error {
  constructor(code) {
    const safeCode = Object.hasOwn(ERROR_MESSAGES, code) ? code : 'internal_error'
    super(ERROR_MESSAGES[safeCode])
    this.name = 'ApiError'
    this.code = safeCode
  }
}

export function errorMessage(error) {
  return error instanceof ApiError ? error.message : ERROR_MESSAGES.internal_error
}

async function request(path, options) {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...options,
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
      signal: controller.signal,
    })
    let data = null
    if (response.status !== 204) {
      try {
        data = await response.json()
      } catch {
        throw new ApiError('invalid_response')
      }
    }
    if (!response.ok) {
      // Use fixed local messages, never arbitrary server prose or error bodies.
      throw new ApiError(data?.error?.code)
    }
    return { status: response.status, data }
  } catch (error) {
    if (error instanceof ApiError) throw error
    throw new ApiError(controller.signal.aborted ? 'timeout' : 'unavailable')
  } finally {
    clearTimeout(timeout)
  }
}

const isId = (id) => Number.isSafeInteger(id) && id >= 0
const isIds = (ids) => Array.isArray(ids) && ids.every(isId) && new Set(ids).size === ids.length
const isSelection = (value) => value && isIds(value.entry_ids)
  && value.count === value.entry_ids.length && value.count <= 10
  && (value.count === 0 ? value.origin === null : typeof value.origin === 'string')
const skippedReasons = new Set(['unsupported MIME type', 'unsupported URL scheme', 'unsupported content encoding'])

function isCapture(value) {
  return value && typeof value.capture_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value.capture_id)
    && isId(value.entry_count) && isId(value.supported_count) && isId(value.skipped_count)
    && Array.isArray(value.transactions) && value.transactions.length === value.supported_count
    && value.transactions.every((tx) => isId(tx.entry_id) && typeof tx.method === 'string'
      && Number.isInteger(tx.status) && tx.status >= 100 && tx.status <= 599
      && typeof tx.origin === 'string' && typeof tx.display_path === 'string')
    && isIds(value.transactions.map((tx) => tx.entry_id))
    && Array.isArray(value.origin_groups) && value.origin_groups.every((group) => typeof group.origin === 'string'
      && isIds(group.entry_ids) && group.entry_ids.length > 0
      && group.entry_ids.every((id) => value.transactions.some((tx) => tx.entry_id === id && tx.origin === group.origin)))
    && new Set(value.origin_groups.map((group) => group.origin)).size === value.origin_groups.length
    && value.origin_groups.reduce((count, group) => count + group.entry_ids.length, 0) === value.supported_count
    && Array.isArray(value.skipped_entries) && value.skipped_entries.length === value.skipped_count
    && value.skipped_entries.every((entry) => isId(entry.entry_id) && skippedReasons.has(entry.reason))
    && value.entry_count === value.supported_count + value.skipped_count && isSelection(value.selection)
}

export async function importCapture(file) {
  if (file.size > MAX_IMPORT_BYTES) throw new ApiError('payload_too_large')
  const { status, data } = await request('/api/captures', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: file,
  })
  if (status !== 201 || !isCapture(data)) throw new ApiError('invalid_response')
  return data
}

export async function saveSelection(captureId, entryIds, origin) {
  const { status, data } = await request(`/api/captures/${encodeURIComponent(captureId)}/selection`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entry_ids: entryIds }),
  })
  if (status !== 200 || !isSelection(data) || data.origin !== origin
    || data.count !== entryIds.length || data.entry_ids.some((id, index) => id !== entryIds[index])) {
    throw new ApiError('invalid_response')
  }
  return data
}

export async function deleteCapture(captureId) {
  const { status } = await request(`/api/captures/${encodeURIComponent(captureId)}`, { method: 'DELETE' })
  if (status !== 204) throw new ApiError('invalid_response')
}
