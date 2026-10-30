import { useEffect, useRef, useState } from 'react'
import { ApiError, deleteCapture, errorMessage, importCapture, saveSelection } from './api.js'
import './App.css'

function App() {
  const [file, setFile] = useState(null)
  const [capture, setCapture] = useState(null)
  const [origin, setOrigin] = useState('')
  const [draft, setDraft] = useState([])
  const [confirmed, setConfirmed] = useState(null)
  const [busy, setBusy] = useState(null)
  const [importError, setImportError] = useState('')
  const [captureError, setCaptureError] = useState('')
  const [notice, setNotice] = useState('')
  const [expired, setExpired] = useState(false)
  const fileInput = useRef(null)
  const pending = useRef(false)
  const version = useRef(0)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      version.current += 1
    }
  }, [])

  function begin(action) {
    if (pending.current) return null
    pending.current = true // Immediate guard, including before React re-renders.
    const ticket = ++version.current
    setBusy(action)
    return ticket
  }

  function current(ticket) {
    return mounted.current && version.current === ticket
  }

  function finish(ticket) {
    if (current(ticket)) {
      pending.current = false
      setBusy(null)
    }
  }

  function release(captureId) {
    // Releasing replaced/stale imports must never overwrite the current view.
    void deleteCapture(captureId).catch(() => undefined)
  }

  async function handleImport(event) {
    event.preventDefault()
    if (!file) return
    const ticket = begin('import')
    if (ticket === null) return
    setImportError('')
    setNotice('')
    const previousId = capture?.capture_id
    try {
      const imported = await importCapture(file)
      if (!current(ticket)) {
        release(imported.capture_id)
        return
      }
      setCapture(imported)
      setOrigin(imported.origin_groups[0]?.origin ?? '')
      setDraft([])
      setConfirmed(null)
      setCaptureError('')
      setExpired(false)
      setNotice('HAR imported into the local backend. No AI request was made.')
      if (previousId && previousId !== imported.capture_id) release(previousId)
    } catch (error) {
      // A failed replacement import leaves the previous capture/selection intact.
      if (current(ticket)) setImportError(errorMessage(error))
    } finally {
      finish(ticket)
    }
  }

  function changeOrigin(nextOrigin) {
    if (pending.current) return
    setOrigin(nextOrigin)
    setDraft([])
    setConfirmed(null)
    setCaptureError('')
  }

  function toggleEntry(entryId) {
    if (pending.current || expired) return
    if (!draft.includes(entryId) && draft.length >= 10) return
    setDraft(draft.includes(entryId) ? draft.filter((id) => id !== entryId) : [...draft, entryId])
    setConfirmed(null)
    setCaptureError('')
  }

  async function handleConfirm(event) {
    event.preventDefault()
    if (!capture || expired || draft.length < 1 || draft.length > 10) return
    const ticket = begin('save')
    if (ticket === null) return
    setConfirmed(null)
    setCaptureError('')
    try {
      const selection = await saveSelection(capture.capture_id, [...draft], origin)
      if (current(ticket)) setConfirmed(selection)
    } catch (error) {
      if (current(ticket)) {
        setCaptureError(errorMessage(error))
        if (error instanceof ApiError && error.code === 'capture_not_found') setExpired(true)
      }
    } finally {
      finish(ticket)
    }
  }

  function resetCapture(message) {
    setCapture(null)
    setOrigin('')
    setDraft([])
    setConfirmed(null)
    setExpired(false)
    setFile(null)
    if (fileInput.current) fileInput.current.value = ''
    setImportError('')
    setCaptureError('')
    setNotice(message)
  }

  async function handleClear() {
    if (!capture) return
    const ticket = begin('clear')
    if (ticket === null) return
    setCaptureError('')
    setConfirmed(null)
    try {
      await deleteCapture(capture.capture_id)
      if (current(ticket)) resetCapture('Capture cleared from the local backend.')
    } catch (error) {
      if (current(ticket)) {
        if (error instanceof ApiError && error.code === 'capture_not_found') {
          resetCapture('This capture expired or was removed. Import the HAR again.')
        } else {
          setCaptureError(errorMessage(error))
        }
      }
    } finally {
      finish(ticket)
    }
  }

  const transactions = capture?.transactions.filter((tx) => tx.origin === origin) ?? []

  return (
    <main className="shell">
      <header className="page-header">
        <h1>BugCapsule</h1>
        <p>Share the bug. Keep the production data.</p>
      </header>

      <section className="import-section" aria-labelledby="import-heading" aria-busy={busy === 'import'}>
        <h2 id="import-heading">Import HAR</h2>
        <form onSubmit={handleImport}>
          <label htmlFor="har-file">HAR file</label>
          <input
            ref={fileInput}
            id="har-file"
            type="file"
            accept=".har"
            disabled={Boolean(busy)}
            aria-describedby="har-help"
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null)
              setImportError('')
            }}
          />
          <p id="har-help" className="helper-text">
            Selecting a file does not upload it. Import HAR sends it to the local backend
            at http://127.0.0.1:8000. Maximum 16 MiB. This step makes no AI request.
          </p>
          <button type="submit" disabled={!file || Boolean(busy)}>
            {busy === 'import' ? 'Importing…' : 'Import HAR'}
          </button>
          {importError && <p className="error-message" role="alert">{importError}</p>}
        </form>
      </section>

      <p className="status-message" role="status">{busy === 'save' ? 'Saving selection…'
        : busy === 'clear' ? 'Clearing capture…' : busy === 'import' ? 'Importing HAR…' : notice}</p>
      {captureError && <p className="error-message" role="alert">{captureError}</p>}

      {capture && (
        <section className="review-section" aria-labelledby="review-heading" aria-busy={Boolean(busy)}>
          <div className="section-header">
            <h2 id="review-heading">Review requests</h2>
            <button type="button" className="secondary-button" disabled={Boolean(busy)} onClick={handleClear}>
              {busy === 'clear' ? 'Clearing…' : 'Clear capture'}
            </button>
          </div>
          <p>{capture.supported_count} supported · {capture.skipped_count} skipped</p>
          <p className="helper-text">Captures expire 30 minutes after import. Review and selection stay local.</p>
          {capture.origin_groups.length > 0 ? (
            <form onSubmit={handleConfirm}>
              <fieldset disabled={Boolean(busy) || expired}>
                <legend>API origin</legend>
                {capture.origin_groups.map((group) => (
                  <label className="choice" key={group.origin}>
                    <input type="radio" name="origin" value={group.origin} checked={origin === group.origin}
                      onChange={() => changeOrigin(group.origin)} />
                    <span>{group.origin} ({group.entry_ids.length} requests)</span>
                  </label>
                ))}
              </fieldset>
              <fieldset disabled={Boolean(busy) || expired} aria-describedby="selection-help selection-count">
                <legend>Transactions</legend>
                <p id="selection-help" className="helper-text">
                  Select 1–10 requests from this origin. Entry IDs distinguish requests with identical paths.
                </p>
                <p id="selection-count" role="status">Selected {draft.length} of 10</p>
                <div className="transaction-list">
                  {transactions.map((tx) => (
                    <label className="choice transaction" key={tx.entry_id}>
                      <input type="checkbox" checked={draft.includes(tx.entry_id)}
                        disabled={!draft.includes(tx.entry_id) && draft.length >= 10}
                        onChange={() => toggleEntry(tx.entry_id)} />
                      <span>
                        <strong>Entry {tx.entry_id} · {tx.method}</strong>
                        <span className="transaction-path">{tx.display_path}</span>
                        <span className="helper-text">HTTP {tx.status}</span>
                      </span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <button type="submit" disabled={Boolean(busy) || expired || draft.length < 1 || draft.length > 10}>
                {busy === 'save' ? 'Confirming…' : 'Confirm selection'}
              </button>
            </form>
          ) : <p>No supported JSON transactions were found. Clear this capture or import another HAR.</p>}
          {confirmed && (
            <div className="confirmation" role="status">
              <strong>Selection confirmed by the local backend</strong>
              <p>Count: {confirmed.count} · Entry IDs: {confirmed.entry_ids.join(', ')}</p>
              <p>Origin: {confirmed.origin}</p>
            </div>
          )}
          {capture.skipped_entries.length > 0 && (
            <div className="skipped-entries">
              <h3>Skipped entries</h3>
              <ul>{capture.skipped_entries.map((entry) => (
                <li key={entry.entry_id}>Entry {entry.entry_id}: {entry.reason}</li>
              ))}</ul>
            </div>
          )}
        </section>
      )}
    </main>
  )
}

export default App
