// Local import, selection and preview only. Never log or persist captured data.
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
  selection_required: 'Confirm your transaction selection again before preparing a preview.',
  invalid_preview_settings: 'Choose a supported failure pattern and unique labels from the offered choices.',
  label_choices_unavailable: 'These requests are unsupported or exceed the schema-label or privacy limits. Choose a smaller or different selection.',
  preview_unavailable: 'These requests cannot produce a preview within the privacy limits. Choose a smaller or different selection.',
  approval_required: 'Approve sending this exact preview before requesting a plan.',
  invalid_plan_request: 'The planning request was rejected. Review the active preview and its digest.',
  stale_preview: 'The capture or preview changed. Any stale result was discarded; no retry was made.',
  planning_consumed: 'Planning was already submitted for this capture. No additional model request was made.',
  missing_meta_configuration: 'The backend Meta API key is missing or invalid. No model request was attempted.',
  planning_timeout: 'Meta planning timed out. It was not retried.',
  planning_transport_error: 'Meta planning failed due to a network or TLS error. It was not retried.',
  planning_http_error: 'Meta returned an HTTP error. It was not retried.',
  planning_incomplete: 'Meta returned an incomplete response. Partial output was rejected; it was not retried.',
  planning_response_error: 'Meta did not return an acceptable tool response. It was not retried.',
  planning_invalid_proposal: 'The proposed plan failed local validation. It was not retried.',
  planning_failed: 'Planning stopped safely. It was not automatically retried.',
  invalid_host: 'The backend accepts loopback hosts only. Use the local development servers.',
  invalid_origin: 'Open the frontend at http://localhost:5173 or http://127.0.0.1:5173.',
  invalid_request: 'The local backend rejected the request. Try again.',
  not_found: 'The local API route is unavailable. Check that the BugCapsule backend is running.',
  method_not_allowed: 'The local API does not support this request. Check the backend version.',
  internal_error: 'The local backend could not complete the request. Try again.',
  unavailable: 'Could not reach the local backend. Start it at http://127.0.0.1:8000 and use frontend port 5173.',
  timeout: 'The local backend request timed out. The operation may have reached the backend; do not assume it was cancelled.',
  invalid_response: 'The local backend returned an unexpected response. Try again.',
}

export class ApiError extends Error {
  constructor(code, diagnostics = null) {
    const safeCode = Object.hasOwn(ERROR_MESSAGES, code) ? code : 'internal_error'
    super(ERROR_MESSAGES[safeCode])
    this.name = 'ApiError'
    this.code = safeCode
    this.diagnostics = safeDiagnostics(diagnostics)
  }
}

export function errorMessage(error) {
  return error instanceof ApiError ? error.message : ERROR_MESSAGES.internal_error
}

async function request(path, options, timeoutMs = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), timeoutMs)
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
      throw new ApiError(data?.error?.code, data?.error?.diagnostics)
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

function safeDiagnostics(value) {
  if (!value || typeof value !== 'object') return null
  const safe = {}
  for (const key of ['attempts', 'latency_ms', 'http_status']) {
    if (isId(value[key])) safe[key] = value[key]
  }
  if (typeof value.request_attempted === 'boolean') safe.request_attempted = value.request_attempted
  const incomplete = value.incomplete
  if (incomplete?.status === 'incomplete') {
    safe.incomplete = { status: 'incomplete', reason: ['max_output_tokens', 'content_filter'].includes(incomplete.reason) ? incomplete.reason : 'unknown' }
    for (const key of ['max_output_tokens', 'input_tokens', 'output_tokens', 'total_tokens', 'reasoning_tokens']) {
      if (isId(incomplete[key])) safe.incomplete[key] = incomplete[key]
    }
  }
  return safe
}

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

export async function fetchSchemaLabels(captureId) {
  const { status, data } = await request(`/api/captures/${encodeURIComponent(captureId)}/schema-labels`, { method: 'GET' })
  if (status !== 200 || !Array.isArray(data?.labels) || data.labels.length > 64
    || data.labels.some((label) => typeof label !== 'string' || new TextEncoder().encode(label).length > 64)
    || new Set(data.labels).size !== data.labels.length) {
    throw new ApiError('invalid_response')
  }
  return data.labels
}

export async function prepareModelPreview(captureId, failurePattern, approvedLabels) {
  const { status, data } = await request(`/api/captures/${encodeURIComponent(captureId)}/model-preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ failure_pattern: failurePattern, approved_labels: approvedLabels }),
  })
  const metadata = data?.metadata
  if (status !== 200 || typeof data?.preview_id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(data.preview_id)
    || typeof data.preview_json !== 'string' || !isId(data.byte_count) || data.byte_count > 65_536
    || new TextEncoder().encode(data.preview_json).length !== data.byte_count
    || typeof data.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(data.sha256)
    || !metadata || typeof metadata.examples_truncated !== 'boolean'
    || !isId(metadata.examples_omitted) || !isId(metadata.max_examples_per_node) || !isId(metadata.max_examples_total)) {
    throw new ApiError('invalid_response')
  }
  return data // Preserve the exact preview_json string; never reserialize it.
}

export async function requestModelPlan(captureId, preview) {
  const { status, data } = await request(`/api/captures/${encodeURIComponent(captureId)}/plan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ preview_id: preview.preview_id, sha256: preview.sha256, approved: true }),
  }, 45_000) // Allow the backend's existing 30-second transport timeout to report safely.
  const refs = (values) => Array.isArray(values) && values.every((ref) => typeof ref === 'string')
  const path = (value) => Array.isArray(value) && value.every((part) => part && (typeof part.key === 'string' || part.each === '*'))
  if (status !== 200 || data?.preview_id !== preview.preview_id || data.sha256 !== preview.sha256
    || typeof data.proposal_id !== 'string' || data.validation !== 'schema_validated'
    || data.transformations !== 'not_run' || data.verification !== 'not_run'
    || !Array.isArray(data.replacements) || data.replacements.some((item) => !item || typeof item.ref !== 'string' || !['id', 'email', 'name', 'text'].includes(item.kind))
    || !Array.isArray(data.relationships) || data.relationships.some((item) => !item || !refs(item.refs) || typeof item.reason !== 'string')
    || !data.check || typeof data.check.type !== 'string' || !refs(data.check.refs) || typeof data.check.note !== 'string'
    || !['resolved', 'definition_reference_roles_required', 'invalid_configuration'].includes(data.check.resolution)
    || !Array.isArray(data.check.locations) || data.check.locations.some((location) => !location || !isId(location.entry_id) || typeof location.ref !== 'string' || typeof location.target !== 'string' || !path(location.path))
    || !Array.isArray(data.check.identity_groups) || !data.check.identity_groups.every(refs)
    || typeof data.needs_review !== 'boolean' || typeof data.review_reason !== 'string'
    || !Array.isArray(data.resolved_replacements) || data.resolved_replacements.some((rule) => !rule || !isIds(rule.entry_ids) || typeof rule.target !== 'string' || typeof rule.kind !== 'string' || !path(rule.path))
    || !data.usage || typeof data.usage !== 'object') {
    throw new ApiError('invalid_response')
  }
  return { ...data, diagnostics: safeDiagnostics(data.diagnostics) }
}
