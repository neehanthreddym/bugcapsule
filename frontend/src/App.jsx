import './App.css'

function App() {
  return (
    <main className="shell">
      <header className="page-header">
        <h1>BugCapsule</h1>
        <p>Share the bug. Keep the production data.</p>
      </header>

      <section className="import-section" aria-labelledby="import-heading">
        <h2 id="import-heading">Import HAR</h2>
        <label htmlFor="har-file">HAR file</label>
        <input
          id="har-file"
          type="file"
          accept=".har"
          aria-describedby="har-help"
        />
        <p id="har-help" className="helper-text">
          Selecting a file does not upload it. Processing is not available yet.
        </p>
      </section>
    </main>
  )
}

export default App
