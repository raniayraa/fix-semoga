export interface PlaybookInfo {
  id: string
  filename: string
  description: string
}

export interface JobStatus {
  job_id: string
  playbook_id: string
  status: 'running' | 'done' | 'error' | 'aborted'
  pause_state: 'paused_start' | 'paused_stop' | null
  exit_code: number | null
}

export interface PktFileInfo {
  name: string
  last_modified: number
}

export interface PktgenConfig {
  nodes: Record<string, string>
}

export interface NodeEntry {
  ip: string
  label: string
  pkt_file: string
  enabled: boolean
}

export interface NodeRegistryResponse {
  nodes: NodeEntry[]
}

export interface ExperimentResult {
  name: string
  mtime: number
  files: string[]
  display_name: string | null
  description: string | null
}

export interface LatencyMetrics {
  min_ns: number
  avg_ns: number
  max_ns: number
  jitter_ns: number
}

export interface MetricsSummary {
  peak_forwarded_pps: number
  peak_forwarded_gbps: number
  sender_injection_pps: number
  packet_loss_pct: number
  nic_drop_rate_mean: number
  nic_drop_rate_peak: number
  forwarding_efficiency_pct: number
  throughput_std_dev: number
}

export interface CsvRow {
  time: string
  port: string
  metric: string
  value: string
}

export interface NodeCsvData {
  filename: string
  rows: CsvRow[]
}

export interface PktFileData {
  filename: string
  content: string
}

export type WsMessage =
  | { type: 'log'; line: string }
  | { type: 'state'; status: string; pause_state: string | null }
  | { type: 'done'; exit_code: number; status: string }
  | { type: 'ping' }

const BASE = '/api'

async function get<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path)
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(BASE + path, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(BASE + path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

async function patch<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(BASE + path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export const api = {
  listPlaybooks: () => get<PlaybookInfo[]>('/playbooks'),
  runPlaybook: (id: string, variant?: string) =>
    post<{ job_id: string }>(`/playbooks/${id}/run`, variant ? { variant } : undefined),
  runAll: (variant?: string) => post<{ job_id: string }>('/jobs/run-all', variant ? { variant } : undefined),
  getJob: (jobId: string) => get<JobStatus>(`/jobs/${jobId}`),
  sendSignal: (jobId: string, signal: 'start_traffic' | 'stop_traffic' | 'abort') =>
    post<{ ok: boolean }>(`/jobs/${jobId}/signal`, { signal }),
  listPktFiles: () => get<PktFileInfo[]>('/pkt-files'),
  getPktFile: (name: string) => get<{ name: string; content: string }>(`/pkt-files/${name}`),
  savePktFile: (name: string, content: string) =>
    put<{ ok: boolean }>(`/pkt-files/${name}`, { content }),
  getPktgenConfig: () => get<PktgenConfig>('/pktgen-config'),
  savePktgenConfig: (nodes: Record<string, string>) =>
    put<{ ok: boolean }>('/pktgen-config', { nodes }),
  getNodeRegistry: () => get<NodeRegistryResponse>('/node-registry'),
  updateNode: (ip: string, update: { enabled?: boolean; pkt_file?: string }) =>
    patch<NodeRegistryResponse>(`/node-registry/${encodeURIComponent(ip)}`, update),
  listResults: () => get<ExperimentResult[]>('/results'),
  getResultCsv: (exp: string, file: string) => get<NodeCsvData>(`/results/${exp}/${file}`),
  getResultPkt: (exp: string, file: string) => get<PktFileData>(`/results/${exp}/${file}`),
  renameExperiment: (exp: string, displayName: string) =>
    put<{ ok: boolean; display_name: string; new_name: string }>(`/results/${exp}/rename`, { display_name: displayName }),
  updateDescription: (exp: string, description: string) =>
    put<{ ok: boolean }>(`/results/${exp}/description`, { description }),
  getMetrics: (exp: string) => get<MetricsSummary>(`/results/${exp}/metrics`),
  getCpuTimeseries: (exp: string, node: string): Promise<string> =>
    fetch(BASE + `/results/${exp}/cpu/${node}`).then(r => {
      if (!r.ok) throw new Error(r.statusText)
      return r.text()
    }),
  getLatency: (exp: string) => get<LatencyMetrics>(`/results/${exp}/latency`),
}

export function createJobWebSocket(
  jobId: string,
  onMessage: (msg: WsMessage) => void,
  onClose?: () => void,
): WebSocket {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const ws = new WebSocket(`${proto}://${window.location.host}/ws/jobs/${jobId}`)
  ws.onmessage = (e) => {
    try {
      onMessage(JSON.parse(e.data))
    } catch {}
  }
  ws.onclose = onClose ?? (() => {})
  return ws
}
