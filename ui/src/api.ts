// Tiny REST client for the FastAPI surface in er_automations.server.app.
//
// One module so the UI never inlines a fetch — easier to mock in Vitest and
// to swap the base URL when the server moves behind a reverse proxy.

export type Automation = {
  id: number
  key: string
  name: string
  customer: string
}

export type Run = {
  id: number
  automation_id: number
  user_id: number | null
  period: string
  status: 'running' | 'paused' | 'completed' | 'aborted'
  current_step_index: number
  created_at: string
}

export type StepExecution = {
  id: number
  run_id: number
  step_index: number
  attempt_no: number
  name: string
  status: 'pending' | 'running' | 'good' | 'verify' | 'bad' | 'aborted'
  live_action: Record<string, unknown> | null
  verify_rows: Array<Record<string, unknown>> | null
  flagged_columns: string[] | null
  notes: string | null
  created_at: string
  is_current: boolean
}

export type RunDetail = { run: Run; steps: StepExecution[] }

export type Edit = {
  row_id: string
  column: string
  old_value: unknown
  new_value: unknown
  remember?: boolean
}

const baseUrl = (): string =>
  (import.meta as unknown as { env?: { VITE_API_BASE?: string } }).env?.VITE_API_BASE ?? ''

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  async listAutomations(): Promise<Automation[]> {
    return json(await fetch(`${baseUrl()}/automations`))
  },
  async listRuns(automationKey?: string): Promise<(Run & { automation_key: string })[]> {
    const q = automationKey ? `?automation_key=${encodeURIComponent(automationKey)}` : ''
    return json(await fetch(`${baseUrl()}/runs${q}`))
  },
  async getRun(runId: number): Promise<RunDetail> {
    return json(await fetch(`${baseUrl()}/runs/${runId}`))
  },
  async executeStep(runId: number, stepIndex: number): Promise<StepExecution> {
    return json(await fetch(`${baseUrl()}/runs/${runId}/steps/${stepIndex}/execute`, { method: 'POST' }))
  },
  async approveStep(runId: number, stepIndex: number): Promise<void> {
    await json(await fetch(`${baseUrl()}/runs/${runId}/steps/${stepIndex}/approve`, { method: 'POST' }))
  },
  async submitEdits(runId: number, stepIndex: number, edits: Edit[]): Promise<StepExecution> {
    return json(
      await fetch(`${baseUrl()}/runs/${runId}/steps/${stepIndex}/edits`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(edits),
      }),
    )
  },
  async rejectStep(runId: number, stepIndex: number, stepExecutionId: number, notes?: string): Promise<void> {
    await json(
      await fetch(`${baseUrl()}/runs/${runId}/steps/${stepIndex}/reject`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ step_execution_id: stepExecutionId, notes }),
      }),
    )
  },
}
