import type {
  Bundle,
  CampusMap,
  Draft,
  ParseResult,
  Report,
  StopDraft,
  TaskSummary,
} from './types'

export class ApiError extends Error {
  code: string

  constructor(code: string, message: string) {
    super(message)
    this.code = code
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  })
  const text = await response.text()
  let payload: unknown = {}
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = { message: text }
    }
  }
  if (!response.ok) {
    const data = payload as { code?: string; message?: string; detail?: string }
    throw new ApiError(data.code || `http_${response.status}`, data.message || data.detail || `请求失败（${response.status}）`)
  }
  return payload as T
}

export function newRequestId(): string {
  return `req_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`
}

export const api = {
  health: () => request<{ status: string; map_version: string; llm_configured: boolean; llm_model: string | null; data_notice: string }>('/api/health'),
  map: () => request<CampusMap>('/api/map'),
  parse: (text: string) =>
    request<ParseResult>('/api/tasks/parse', { method: 'POST', body: JSON.stringify({ text }) }),
  createTask: (payload: { mode: string; pickup_location_id: string; stops: StopDraft[]; deadline: string | null; deadline_kind: string; raw_input: string; parser: string }) =>
    request<Bundle>('/api/tasks', { method: 'POST', body: JSON.stringify({ ...payload, confirmed: true }) }),
  acceptTask: (id: string, assignee: string, note = '') =>
    request<Bundle>(`/api/tasks/${id}/accept`, { method: 'POST', body: JSON.stringify({ assignee, note }) }),
  addNote: (id: string, author: string, content: string) =>
    request<Bundle>(`/api/tasks/${id}/notes`, { method: 'POST', body: JSON.stringify({ author, content }) }),
  listTasks: () => request<{ tasks: TaskSummary[] }>('/api/tasks'),
  getTask: (id: string) => request<Bundle>(`/api/tasks/${id}`),
  plan: (id: string) => request<Bundle>(`/api/tasks/${id}/plan`, { method: 'POST', body: JSON.stringify({}) }),
  action: (id: string, action: string, requestId: string, planVersion: number, extra: Record<string, unknown> = {}) =>
    request<Bundle>(`/api/tasks/${id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action, request_id: requestId, plan_version: planVersion, ...extra }),
    }),
  replan: (id: string, reason: string) =>
    request<Bundle>(`/api/tasks/${id}/replan`, {
      method: 'POST',
      body: JSON.stringify({ request_id: newRequestId(), reason }),
    }),
  simTick: (id: string, seconds: number, speedMps?: number) =>
    request<Bundle>(`/api/tasks/${id}/sim/tick`, {
      method: 'POST',
      body: JSON.stringify({ seconds, speed_mps: speedMps ?? null }),
    }),
  report: (id: string) => request<Report>(`/api/tasks/${id}/report`),
  roadEvents: () => request<{ road_events: Bundle['road_events']; road_status: Record<string, string> }>('/api/road-events'),
  createRoadEvent: (payload: { edge_id: string; status: string; source: string; confirmed_by: string; note: string }) =>
    request<{ id: string; road_status: Record<string, string> }>('/api/road-events', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  analyze: (edgeId: string, text: string, taskId?: string) =>
    request<{ edge_name: string; candidate_status: string | null; risk: string; analyzer: string; notice: string }>(
      '/api/road-events/analyze',
      { method: 'POST', body: JSON.stringify({ edge_id: edgeId, text, task_id: taskId ?? null }) },
    ),
  seed: () => request<{ created: { id: string; text: string }[] }>('/api/seed', { method: 'POST', body: JSON.stringify({ reset: false }) }),
}

export type { Draft }