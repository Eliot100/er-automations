import { useEffect, useMemo, useState } from 'react'
import { api, type Automation, type Edit, type RunDetail, type StepExecution } from './api'
import './App.css'

// The UI mirrors the sketch in design/ER Automations.html:
//   left: type rail (Electricity active, Water stubbed) → list of automations
//   middle: steps column for the selected run
//   right: live-action panel + editable verify-rows table + Good/Verify/Reject

type TypeKey = 'electricity' | 'water'

const TYPES: { key: TypeKey; label: string; enabled: boolean }[] = [
  { key: 'electricity', label: 'חשמל', enabled: true },
  { key: 'water', label: 'מים', enabled: false },
]

export default function App() {
  const [activeType, setActiveType] = useState<TypeKey>('electricity')
  const [automations, setAutomations] = useState<Automation[]>([])
  const [activeAutomation, setActiveAutomation] = useState<string | null>(null)
  const [activeRunId, setActiveRunId] = useState<number | null>(null)
  const [runDetail, setRunDetail] = useState<RunDetail | null>(null)
  const [edits, setEdits] = useState<Edit[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.listAutomations().then(setAutomations).catch((e) => setError(String(e)))
  }, [])

  useEffect(() => {
    if (!activeAutomation) return
    api.listRuns(activeAutomation).then((rs) => {
      if (rs.length) setActiveRunId(rs[0].id)
    }).catch((e) => setError(String(e)))
  }, [activeAutomation])

  useEffect(() => {
    if (activeRunId == null) {
      setRunDetail(null)
      return
    }
    api.getRun(activeRunId).then(setRunDetail).catch((e) => setError(String(e)))
  }, [activeRunId])

  const currentStep: StepExecution | null = useMemo(() => {
    if (!runDetail) return null
    return runDetail.steps.find((s) => s.step_index === runDetail.run.current_step_index) ?? null
  }, [runDetail])

  async function refreshRun() {
    if (activeRunId == null) return
    setRunDetail(await api.getRun(activeRunId))
    setEdits([])
  }

  async function onExecute(idx: number) {
    if (activeRunId == null) return
    try {
      await api.executeStep(activeRunId, idx)
      await refreshRun()
    } catch (e) {
      setError(String(e))
    }
  }

  async function onApprove(idx: number) {
    if (activeRunId == null) return
    await api.approveStep(activeRunId, idx)
    await refreshRun()
  }

  async function onSubmitEdits(idx: number) {
    if (activeRunId == null || edits.length === 0) return
    await api.submitEdits(activeRunId, idx, edits)
    await refreshRun()
  }

  async function onReject(step: StepExecution) {
    const notes = window.prompt('Why are you rejecting this step?') ?? ''
    await api.rejectStep(activeRunId!, step.step_index, step.id, notes)
    await refreshRun()
  }

  return (
    <div className="layout" dir="rtl">
      <aside className="rail">
        <h2>סוגי אוטומציה</h2>
        <ul>
          {TYPES.map((t) => (
            <li key={t.key}>
              <button
                disabled={!t.enabled}
                className={t.key === activeType ? 'active' : ''}
                onClick={() => setActiveType(t.key)}
              >
                {t.label}
                {!t.enabled && <span className="muted"> (בפיתוח)</span>}
              </button>
            </li>
          ))}
        </ul>

        <h2>אוטומציות</h2>
        <ul>
          {automations.length === 0 && <li className="muted">אין אוטומציות רשומות</li>}
          {automations.map((a) => (
            <li key={a.key}>
              <button
                className={a.key === activeAutomation ? 'active' : ''}
                onClick={() => setActiveAutomation(a.key)}
              >
                {a.name}
                <span className="muted"> · {a.customer}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="steps">
        <h2>צעדים</h2>
        {!runDetail && <p className="muted">בחר אוטומציה וריצה כדי לראות צעדים</p>}
        {runDetail && (
          <>
            <p className="period">תקופה {runDetail.run.period} · סטטוס {runDetail.run.status}</p>
            <ol>
              {runDetail.steps.map((s) => (
                <li key={s.id} className={`step pip-${s.status}`}>
                  <span className="pip" aria-hidden="true" />
                  <span className="name">{s.name}</span>
                  <span className="attempt">attempt {s.attempt_no}</span>
                </li>
              ))}
              <li>
                <button
                  className="execute"
                  onClick={() => onExecute(runDetail.run.current_step_index)}
                >
                  ▶ הרץ צעד {runDetail.run.current_step_index}
                </button>
              </li>
            </ol>
          </>
        )}
      </section>

      <section className="detail">
        <h2>פעולה חיה</h2>
        {!currentStep && <p className="muted">הרץ צעד כדי לראות מידע</p>}
        {currentStep && (
          <>
            <pre className="live-action">{JSON.stringify(currentStep.live_action, null, 2)}</pre>
            <h3>שורות לאימות</h3>
            <VerifyRows
              step={currentStep}
              edits={edits}
              onChange={setEdits}
            />
            <div className="actions">
              <button className="good" onClick={() => onApprove(currentStep.step_index)}>
                ✓ אישור (Good)
              </button>
              <button className="verify" onClick={() => onSubmitEdits(currentStep.step_index)}>
                ✎ ערוך והרץ מחדש (Verify)
              </button>
              <button className="bad" onClick={() => onReject(currentStep)}>
                ✕ דחייה (Bad)
              </button>
            </div>
          </>
        )}
        {error && <div className="error">שגיאה: {error}</div>}
      </section>
    </div>
  )
}

type VerifyRowsProps = {
  step: StepExecution
  edits: Edit[]
  onChange: (next: Edit[]) => void
}

function VerifyRows({ step, edits, onChange }: VerifyRowsProps) {
  const rows = step.verify_rows ?? []
  if (rows.length === 0) return <p className="muted">אין שורות לאימות</p>
  const columns = Object.keys(rows[0]).filter((c) => c !== 'row_id')
  const flagged = new Set(step.flagged_columns ?? [])

  function updateCell(rowId: string, column: string, oldValue: unknown, newValue: string) {
    const next = edits.filter((e) => !(e.row_id === rowId && e.column === column))
    next.push({ row_id: rowId, column, old_value: oldValue, new_value: newValue })
    onChange(next)
  }

  return (
    <table className="verify">
      <thead>
        <tr>
          {columns.map((c) => (
            <th key={c} className={flagged.has(c) ? 'flagged' : ''}>{c}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => {
          const rowId = String(r.row_id ?? i)
          return (
            <tr key={rowId}>
              {columns.map((c) => {
                const editedHit = edits.find((e) => e.row_id === rowId && e.column === c)
                const value = editedHit ? String(editedHit.new_value ?? '') : String(r[c] ?? '')
                return (
                  <td key={c} className={flagged.has(c) ? 'flagged' : ''}>
                    <input
                      value={value}
                      onChange={(ev) => updateCell(rowId, c, r[c], ev.target.value)}
                    />
                  </td>
                )
              })}
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
