import { useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage, requestModelPlan } from './api.js'
import ExecutionReview from './ExecutionReview.jsx'

function Diagnostics({ value }) {
  if (!value) return null
  return (
    <div className="planning-diagnostics">
      {value.attempts !== undefined && <p>Model request attempts: {value.attempts}</p>}
      {value.request_attempted !== undefined && <p>Model request attempted: {value.request_attempted ? 'yes' : 'no'}</p>}
      {value.latency_ms !== undefined && <p>Elapsed: {value.latency_ms} ms</p>}
      {value.http_status !== undefined && <p>Meta HTTP status: {value.http_status}</p>}
      {value.incomplete && (
        <div>
          <p>Response: incomplete · Reason: {value.incomplete.reason}</p>
          {['max_output_tokens', 'input_tokens', 'output_tokens', 'total_tokens', 'reasoning_tokens'].map((key) => (
            value.incomplete[key] !== undefined && <p key={key}>{key}: {value.incomplete[key]}</p>
          ))}
        </div>
      )}
    </div>
  )
}

const displayPath = (path) => path.length === 0 ? '(document root)'
  : path.map((segment) => segment.each === '*' ? '[*]' : JSON.stringify(segment.key)).join(' → ')

function PlanReview({ captureId, preview, submitted, onSubmitted, onInvalidSelection }) {
  const [approved, setApproved] = useState(false)
  const [waiting, setWaiting] = useState(false)
  const [review, setReview] = useState(null)
  const [error, setError] = useState(null)
  const pending = useRef(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  async function requestPlan(event) {
    event.preventDefault()
    if (!approved || submitted || pending.current) return
    pending.current = true
    setWaiting(true)
    setApproved(false)
    setError(null)
    onSubmitted()
    try {
      const proposal = await requestModelPlan(captureId, preview)
      if (mounted.current) setReview(proposal)
    } catch (failure) {
      if (mounted.current) {
        setError(failure)
        if (failure instanceof ApiError && ['capture_not_found', 'selection_required'].includes(failure.code)) {
          onInvalidSelection(failure)
        }
      }
    } finally {
      if (mounted.current) setWaiting(false)
    }
  }

  return (
    <section className="plan-review" aria-labelledby="plan-heading" aria-busy={waiting}>
      <h3 id="plan-heading">Request a model plan</h3>
      <p>Review the complete preview above before approving. This sends it to Meta and may incur API usage.</p>
      <form onSubmit={requestPlan}>
        <label className="choice">
          <input type="checkbox" checked={approved} disabled={submitted || waiting} onChange={(event) => setApproved(event.target.checked)} />
          <span>I approve sending this exact preview, identified by the SHA-256 digest above, to Meta for planning.</span>
        </label>
        <button type="submit" disabled={!approved || submitted || waiting}>
          {waiting ? 'Requesting plan…' : 'Approve and request plan'}
        </button>
      </form>
      {waiting && <p role="status">Waiting for Meta and local proposal validation. No transformations or verification are running.</p>}
      {submitted && !waiting && !review && !error && <p>This capture already has a planning submission. This step will not make another initial model request.</p>}
      {error && (
        <div>
          <p className="error-message" role="alert">{errorMessage(error)}</p>
          <Diagnostics value={error instanceof ApiError ? error.diagnostics : null} />
          <p>No automatic retry was made. If no result arrived, the backend may still have attempted the request.</p>
        </div>
      )}
      {review && (
        <div className="proposal-details">
          <h3>Validated proposal for human review</h3>
          <p>Planning validation alone does not establish semantic correctness or failure preservation. Inspect local verification below after approved execution.</p>
          <p><strong>Model needs review: {review.needs_review ? 'yes' : 'no'}</strong></p>
          {review.review_reason && <p className="model-prose">{review.review_reason}</p>}
          <h4>Replacement proposals</h4>
          {review.replacements.length === 0 ? <p>No replacements proposed.</p> : (
            <ul>{review.replacements.map((item, index) => <li key={`${item.ref}:${index}`}>{item.ref}: {item.kind}</li>)}</ul>
          )}
          <h4>Resolved local replacement plan</h4>
          <p className="helper-text">Local field paths below may contain private schema names. They are not added to model input.</p>
          <ul>{review.resolved_replacements.map((rule, index) => (
            <li key={index}>Entries {rule.entry_ids.join(', ')} · {rule.target} · {displayPath(rule.path)} · {rule.kind}</li>
          ))}</ul>
          <h4>Relationships</h4>
          {review.relationships.length === 0 ? <p>No relationships proposed.</p> : (
            <ul>{review.relationships.map((item, index) => (
              <li key={index}>{item.refs.join(' ↔ ')}<p className="model-prose">{item.reason}</p></li>
            ))}</ul>
          )}
          <h4>Selected verification check</h4>
          <p>{review.check.type} · {review.check.refs.join(', ')}</p>
          {review.check.note && <p className="model-prose">{review.check.note}</p>}
          <p>Configuration: {review.check.resolution === 'resolved' ? 'resolved locally; local verification results appear below after execution approval'
            : review.check.resolution === 'definition_reference_roles_required' ? 'explicit definition/reference roles still required'
              : 'invalid configuration; further human review required'}</p>
          <ul>{review.check.locations.map((location) => (
            <li key={location.ref}>{location.ref} · Entry {location.entry_id} · {location.target} · {displayPath(location.path)}</li>
          ))}</ul>
          {review.check.identity_groups.length > 0 && <p>Identity groups: {review.check.identity_groups.map((group) => group.join(' + ')).join(' / ')}</p>}
          <Diagnostics value={review.diagnostics} />
          <p>Usage: {['input_tokens', 'output_tokens', 'total_tokens'].filter((key) => Number.isSafeInteger(review.usage[key]))
            .map((key) => `${key}=${review.usage[key]}`).join(' · ') || 'not available'}</p>
          <ExecutionReview key={`${review.proposal_id}:${review.execution_review?.review_id}:${review.execution_review?.sha256}`}
            captureId={captureId} proposal={review} onInvalidSelection={onInvalidSelection} />
        </div>
      )}
      <p>Planning alone does not apply transformations or run verification. Local execution requires separate approval. No export is available in this step.</p>
    </section>
  )
}

export default PlanReview
