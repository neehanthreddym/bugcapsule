import { useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage, executePlan } from './api.js'

const OUTCOMES = {
  preserved: 'Configured failure preserved',
  changed: 'Configured failure changed',
  not_present: 'Selected failure not present in the baseline',
  inconclusive: 'Verification inconclusive',
}

const occurrence = (location) => `Entry ${location.entry_id} / record ${location.record.length ? location.record.join('.') : 'root'} (${location.target}; ${location.refs.join(', ')})`

function Evidence({ title, evidence }) {
  if (!evidence) return <section><h5>{title}</h5><p>No conclusive evidence available.</p></section>
  return (
    <section className="evidence-panel">
      <h5>{title}</h5>
      <dl>
        <dt>Records / observations</dt><dd>{evidence.observations}</dd>
        <dt>Distinct identities</dt><dd>{evidence.distinct_identities ?? 'not applicable'}</dd>
        <dt>Failure occurrences / duplicate pairs</dt><dd>{evidence.failures}</dd>
      </dl>
      <h6>Selected record counts</h6>
      <ul>{evidence.record_counts.map((item, index) => <li key={index}>Entry {item.location.entry_id}: {item.count} ({item.location.refs.join(', ')})</li>)}</ul>
      {evidence.equality_groups.length > 0 && <div>
        <h6>Equal identity locations</h6>
        <ul>{evidence.equality_groups.map((group, index) => <li key={index}>{group.map(occurrence).join(' ↔ ')}</li>)}</ul>
      </div>}
      {evidence.fields.length > 0 && <div>
        <h6>Field states</h6>
        <ul>{evidence.fields.map((item, index) => <li key={index}>{occurrence(item.location)}: {item.state} ({item.value_type ?? 'missing'})</li>)}</ul>
      </div>}
      {evidence.resolutions.length > 0 && <div>
        <h6>Reference resolution</h6>
        <ul>{evidence.resolutions.map((item, index) => <li key={index}>{occurrence(item.location)}: {item.state}
          {item.definitions.length > 0 && ` → ${item.definitions.map(occurrence).join('; ')}`}</li>)}</ul>
      </div>}
    </section>
  )
}

function ExecutionReview({ captureId, proposal, onInvalidSelection }) {
  const [approved, setApproved] = useState(false)
  const [started, setStarted] = useState(false)
  const [waiting, setWaiting] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const mounted = useRef(true)
  const pending = useRef(false)
  const review = proposal.execution_review
  const ready = review?.status === 'ready' && proposal.check.resolution === 'resolved' && !proposal.needs_review

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  async function run(event) {
    event.preventDefault()
    if (!ready || !approved || pending.current || started) return
    pending.current = true
    setStarted(true)
    setApproved(false)
    setWaiting(true)
    setError(null)
    try {
      const actual = await executePlan(captureId, proposal)
      if (mounted.current) setResult(actual)
    } catch (failure) {
      if (mounted.current) {
        setError(failure)
        if (failure instanceof ApiError && ['capture_not_found', 'selection_required'].includes(failure.code)) onInvalidSelection(failure)
      }
    } finally {
      if (mounted.current) setWaiting(false)
    }
  }

  const outcome = result?.verification?.outcome
  return (
    <section className="execution-review" aria-labelledby="execution-heading" aria-busy={waiting}>
      <h4 id="execution-heading">Approve local execution</h4>
      <p>This applies the displayed plan locally and checks only the configured failure. It makes no additional AI request.</p>
      {!ready ? <p role="status">Execution blocked: {review?.reason === 'reference_roles_required'
        ? 'explicit definition/reference roles are required; no roles have been guessed.'
        : review?.reason === 'model_requires_review' ? 'the model flagged this proposal for further human review.'
          : 'the verification check or proposal is unresolved.'}</p> : <div>
        <p>Execution review · Attempt {review.attempt}</p>
        <p className="preview-digest">SHA-256: <code>{review.sha256}</code></p>
        <details>
          <summary>Exact local execution configuration (plan, check and roles)</summary>
          <pre className="model-preview-text" tabIndex={0} role="region" aria-label="Exact local execution configuration">{review.review_json}</pre>
        </details>
        <form onSubmit={run}>
          <label className="choice">
            <input type="checkbox" checked={approved} disabled={started} onChange={(event) => setApproved(event.target.checked)} />
            <span>I approve applying this exact local execution configuration and checking its configured failure.</span>
          </label>
          <button type="submit" disabled={!approved || started}>{waiting ? 'Running locally…' : 'Approve and run locally'}</button>
        </form>
      </div>}
      {waiting && <p role="status">Running deterministic local replacement and verification… No additional AI request is being made.</p>}
      {error && <p className="error-message" role="alert">Execution error: {errorMessage(error)} No automatic retry was made.</p>}
      {result && <div className="execution-result" data-outcome={outcome ?? 'error'}>
        <h4 role="status">{OUTCOMES[outcome] ?? 'Execution error'}</h4>
        <p>Session status: {result.status} · Attempt: {result.attempt}</p>
        <p>Reason: {result.verification?.reason ?? result.reason}</p>
        {result.verification && <div className="evidence-comparison">
          <Evidence title="Baseline" evidence={result.verification.baseline} />
          {result.candidate_created ? <Evidence title="Candidate" evidence={result.verification.transformed} />
            : <section><h5>Candidate</h5><p>No transformed candidate was created. Only the baseline was checked.</p></section>}
        </div>}
        <p className="helper-text">Record locations use zero-based array indices. No original scalar values are displayed.</p>
        <ul>{result.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul>
      </div>}
      <p>“Preserved” means the configured failure survived. It does not establish privacy or complete application compatibility.
        No additional AI request or export occurs in this step. Changed verification never triggers an automatic revision.</p>
    </section>
  )
}

export default ExecutionReview
