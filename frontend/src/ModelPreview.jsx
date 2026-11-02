import { useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage, fetchSchemaLabels, prepareModelPreview } from './api.js'

const PATTERNS = [
  ['duplicate_pagination', 'Duplicate pagination records'],
  ['missing_null_fields', 'Missing or null fields'],
  ['inconsistent_identifier_references', 'Inconsistent identifier references'],
]

function ModelPreview({ captureId, onInvalidSelection }) {
  const [choices, setChoices] = useState({ labels: [], loading: true, error: '' })
  const [pattern, setPattern] = useState('')
  const [approved, setApproved] = useState([])
  const [preview, setPreview] = useState(null)
  const [preparing, setPreparing] = useState(false)
  const [error, setError] = useState('')
  const mounted = useRef(true)
  const pending = useRef(false)
  const version = useRef(0)

  useEffect(() => {
    mounted.current = true
    let active = true
    fetchSchemaLabels(captureId).then((labels) => {
      if (active) setChoices({ labels, loading: false, error: '' })
    }).catch((error) => {
      if (!active) return
      setChoices({ labels: [], loading: false, error: errorMessage(error) })
      if (error instanceof ApiError && ['capture_not_found', 'selection_required'].includes(error.code)) {
        onInvalidSelection(error)
      }
    })
    return () => {
      active = false
      mounted.current = false
      version.current += 1
    }
  }, [captureId, onInvalidSelection])

  function invalidate() {
    version.current += 1
    setPreview(null)
    setError('')
  }

  function selectionError(error) {
    if (error instanceof ApiError && ['capture_not_found', 'selection_required'].includes(error.code)) {
      onInvalidSelection(error)
    }
  }

  async function retryLabels() {
    if (pending.current) return
    pending.current = true
    invalidate()
    const ticket = version.current
    setApproved([])
    setChoices({ labels: [], loading: true, error: '' })
    try {
      const labels = await fetchSchemaLabels(captureId)
      if (mounted.current && ticket === version.current) setChoices({ labels, loading: false, error: '' })
    } catch (error) {
      if (mounted.current && ticket === version.current) {
        setChoices({ labels: [], loading: false, error: errorMessage(error) })
        selectionError(error)
      }
    } finally {
      pending.current = false
    }
  }

  async function prepare(event) {
    event.preventDefault()
    if (pending.current || choices.loading || choices.error || !pattern) return
    pending.current = true
    invalidate()
    const ticket = version.current
    setPreparing(true)
    try {
      const result = await prepareModelPreview(captureId, pattern, [...approved])
      if (mounted.current && ticket === version.current) setPreview(result)
    } catch (error) {
      if (mounted.current && ticket === version.current) {
        setError(errorMessage(error))
        selectionError(error)
      }
    } finally {
      pending.current = false
      if (mounted.current && ticket === version.current) setPreparing(false)
    }
  }

  return (
    <section className="model-section" aria-labelledby="model-heading" aria-busy={preparing || choices.loading}>
      <h2 id="model-heading">Prepare model preview</h2>
      <p>Choose the failure to check. This records your intent; it does not establish that the failure exists.</p>
      <form onSubmit={prepare}>
        <fieldset disabled={preparing || choices.loading}>
          <legend>Failure pattern</legend>
          <label htmlFor="failure-pattern">Failure to check</label>
          <select id="failure-pattern" value={pattern} required onChange={(event) => {
            if (pending.current) return
            invalidate()
            setPattern(event.target.value)
          }}>
            <option value="">Choose a failure pattern</option>
            {PATTERNS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </fieldset>
        <fieldset disabled={preparing || choices.loading} aria-describedby="label-help">
          <legend>Schema-label approval</legend>
          <p id="label-help" className="helper-text">
            All names start unapproved and will be aliased. Checking a name makes that exact name visible
            to Meta if you later approve sending. Names themselves may contain private information.
          </p>
          {choices.loading && <p role="status">Loading local schema-label choices…</p>}
          {choices.labels.map((label) => (
            <label className="choice" key={label}>
              <input type="checkbox" checked={approved.includes(label)} onChange={() => {
                if (pending.current) return
                invalidate()
                setApproved(approved.includes(label) ? approved.filter((name) => name !== label) : [...approved, label])
              }} />
              <span>{label === '' ? '(empty field name)' : label}</span>
            </label>
          ))}
          {!choices.loading && !choices.error && choices.labels.length === 0 && <p>No schema-label names were found.</p>}
        </fieldset>
        {choices.error && (
          <div>
            <p className="error-message" role="alert">{choices.error}</p>
            <button type="button" className="secondary-button" onClick={retryLabels}>Retry label choices</button>
          </div>
        )}
        <button type="submit" disabled={preparing || choices.loading || Boolean(choices.error) || !pattern}>
          {preparing ? 'Preparing locally…' : 'Prepare model preview'}
        </button>
      </form>
      <p className="helper-text">
        Preparation is local. Schema structure and approved labels can reveal context; pseudonymization
        does not guarantee anonymization. No AI request has been made. Preparing grants no send or execution approval.
      </p>
      {preparing && <p role="status">Preparing the exact model payload locally…</p>}
      {error && <p className="error-message" role="alert">{error}</p>}
      {preview && (
        <div>
          <h3>Exact model payload preview</h3>
          <p role="status">Prepared locally · {preview.byte_count} UTF-8 bytes · No AI request made</p>
          <p className="preview-digest">SHA-256: <code>{preview.sha256}</code></p>
          <p>{preview.metadata.examples_truncated ? 'Examples were limited.' : 'No examples were omitted.'}
            {' '}Omitted examples: {preview.metadata.examples_omitted}.
            {' '}Example limits: {preview.metadata.max_examples_per_node} per node,
            {' '}{preview.metadata.max_examples_total} total. Structural summaries remain in the payload.</p>
          <pre className="model-preview-text" tabIndex={0} role="region" aria-label="Exact model payload preview">
            {preview.preview_json}
          </pre>
        </div>
      )}
    </section>
  )
}

export default ModelPreview
