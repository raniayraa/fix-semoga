import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, Legend, ResponsiveContainer,
} from 'recharts'
import { api, LiveStats, TrafficLog } from '../api/client'
import { parseQuery, evaluateQuery } from '../utils/queryLang'

interface RatePoint {
  t: string
  drop_pps: number
  pass_pps: number
  tx_pps: number
  redirect_pps: number
  mbits: number
}

const ACTION_LABELS = ['DROP', 'PASS', 'TX', 'REDIRECT', 'TTL_EXC']
const PROTO_LABELS: Record<number, string> = { 6: 'TCP', 17: 'UDP', 1: 'ICMP' }
const TIME_RANGES = ['30s', '5m', '30m', '1h', '6h', '24h', 'all']

function protoLabel(p: number) { return PROTO_LABELS[p] ?? `IP/${p}` }
function tsLabel(ns: number) {
  const d  = new Date(ns / 1e6)
  const h  = String(d.getHours()).padStart(2, '0')
  const m  = String(d.getMinutes()).padStart(2, '0')
  const sc = String(d.getSeconds()).padStart(2, '0')
  const ms = String(Math.floor((ns / 1e6) % 1000)).padStart(3, '0')
  const us = String(Math.floor((ns / 1e3) % 1000)).padStart(3, '0')
  return `${h}:${m}:${sc}.${ms}${us}`
}

/** Returns a list of page numbers (and '…' gap markers) to render. */
function pageButtons(current: number, total: number): (number | '…')[] {
  if (total <= 9) {
    return Array.from({ length: total }, (_, i) => i + 1)
  }
  const set = new Set<number>([
    1, 2,
    Math.max(1, current - 1), current, Math.min(total, current + 1),
    total - 1, total,
  ])
  const sorted = Array.from(set).filter(p => p >= 1 && p <= total).sort((a, b) => a - b)
  const result: (number | '…')[] = []
  for (let i = 0; i < sorted.length; i++) {
    if (i > 0 && sorted[i] - sorted[i - 1] > 1) result.push('…')
    result.push(sorted[i])
  }
  return result
}

export default function Monitoring() {
  const [chart, setChart] = useState<RatePoint[]>([])
  const [logs, setLogs] = useState<TrafficLog[]>([])
  const [filterAction, setFilterAction] = useState(-1)   // -1 = all
  const [filterProto,  setFilterProto]  = useState(-1)
  const [filterRange,  setFilterRange]  = useState('5m')
  const [running, setRunning] = useState(false)
  const [pageSize, setPageSize] = useState(50)
  const [page,     setPage]     = useState(1)
  const [filterQuery, setFilterQuery] = useState('')
  const prevStats = useRef<LiveStats | null>(null)
  const prevTime  = useRef<number>(Date.now())

  // Poll live stats every 2 seconds for the chart.
  useEffect(() => {
    const tick = async () => {
      try {
        const s = await api.getStatus()
        setRunning(s.daemon_running)
        if (!s.daemon_running) return
        const st = await api.getStatsLive()
        const now = Date.now()
        const dt = (now - prevTime.current) / 1000 || 1

        if (prevStats.current) {
          const prev = prevStats.current
          const totalBytes =
            (st.drop.bytes - prev.drop.bytes) +
            (st.pass.bytes - prev.pass.bytes) +
            (st.tx.bytes - prev.tx.bytes) +
            (st.redirect.bytes - prev.redirect.bytes)

          const pt: RatePoint = {
            t: new Date().toLocaleTimeString(),
            drop_pps:     Math.max(0, (st.drop.packets     - prev.drop.packets)     / dt),
            pass_pps:     Math.max(0, (st.pass.packets     - prev.pass.packets)     / dt),
            tx_pps:       Math.max(0, (st.tx.packets       - prev.tx.packets)       / dt),
            redirect_pps: Math.max(0, (st.redirect.packets - prev.redirect.packets) / dt),
            mbits:        Math.max(0, (totalBytes * 8) / dt / 1e6),
          }
          setChart(prev => [...prev.slice(-59), pt])
        }
        prevStats.current = st
        prevTime.current  = now
      } catch { /* ignore */ }
    }
    tick()
    const id = setInterval(tick, 2000)
    return () => clearInterval(id)
  }, [])

  // Fetch logs when filters change, and auto-refresh every 5s.
  const fetchLogs = useCallback(async () => {
    try {
      const params: Parameters<typeof api.getLogs>[0] = { limit: 500 }
      if (filterAction >= 0) params.action = filterAction
      if (filterProto  >= 0) params.proto  = filterProto
      if (filterRange !== 'all') params.range = filterRange
      const data = await api.getLogs(params)
      setLogs(data)
    } catch { /* ignore */ }
  }, [filterAction, filterProto, filterRange])

  useEffect(() => {
    fetchLogs()
    const id = setInterval(fetchLogs, 5000)
    return () => clearInterval(id)
  }, [fetchLogs])

  // Reset to page 1 whenever filters or page size change.
  useEffect(() => { setPage(1) }, [filterAction, filterProto, filterRange, pageSize, filterQuery])

  const queryResult = useMemo(() => {
    if (filterQuery.trim() === '') return { ok: true as const, logs }
    const parsed = parseQuery(filterQuery)
    if (!parsed.ok) return { ok: false as const, logs, error: parsed.error }
    if (parsed.ast == null) return { ok: true as const, logs }
    const ast = parsed.ast
    return { ok: true as const, logs: logs.filter(l => evaluateQuery(ast, l)) }
  }, [filterQuery, logs])

  const filteredLogs = queryResult.logs
  const queryError   = !queryResult.ok

  const totalPages  = Math.max(1, Math.ceil(filteredLogs.length / pageSize))
  const visibleLogs = filteredLogs.slice((page - 1) * pageSize, page * pageSize)

  return (
    <div>
      <h1 style={s.title}>Monitoring</h1>

      {!running && (
        <div style={s.notice}>XDP is not running. Start it from the Firewall page.</div>
      )}

      {/* Throughput chart */}
      <section style={s.card}>
        <h2 style={s.cardTitle}>Throughput (live, 2s interval)</h2>
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={chart}>
            <CartesianGrid strokeDasharray="3 3" stroke="#E0E0E0" />
            <XAxis dataKey="t" tick={{ fill: '#5A5A5A', fontSize: 11 }} interval="preserveStartEnd" />
            <YAxis yAxisId="pps" tick={{ fill: '#5A5A5A', fontSize: 11 }} />
            <YAxis yAxisId="mb" orientation="right" tick={{ fill: '#5A5A5A', fontSize: 11 }} />
            <Tooltip contentStyle={{ background: '#FFFFFF', border: '1px solid #C8C8C8', fontSize: 12, color: '#1A1A1A' }} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line yAxisId="pps" type="monotone" dataKey="drop_pps"     name="DROP pps"     stroke="#f87171" dot={false} />
            <Line yAxisId="pps" type="monotone" dataKey="pass_pps"     name="PASS pps"     stroke="#4ade80" dot={false} />
            <Line yAxisId="pps" type="monotone" dataKey="tx_pps"       name="TX pps"       stroke="#60a5fa" dot={false} />
            <Line yAxisId="pps" type="monotone" dataKey="redirect_pps" name="REDIR pps"    stroke="#a78bfa" dot={false} />
            <Line yAxisId="mb"  type="monotone" dataKey="mbits"        name="Mbits/s"      stroke="#fbbf24" dot={false} strokeDasharray="4 2" />
          </LineChart>
        </ResponsiveContainer>
      </section>

      {/* Log table */}
      <section style={s.card}>
        <div style={s.logHeader}>
          <h2 style={s.cardTitle}>Traffic Log</h2>
          <div style={s.filters}>
            <input
              type="text"
              style={{
                ...s.sel,
                minWidth: 280,
                fontFamily: 'monospace',
                ...(queryError ? { borderColor: '#DC2626', outline: '1px solid #DC2626' } : {}),
              }}
              placeholder="addr.src eq 1.2.3.4 and port.dst eq 443"
              value={filterQuery}
              onChange={e => setFilterQuery(e.target.value)}
              spellCheck={false}
              autoComplete="off"
            />
            <select style={s.sel} value={filterAction} onChange={e => setFilterAction(+e.target.value)}>
              <option value={-1}>All Actions</option>
              {ACTION_LABELS.map((l, i) => <option key={i} value={i}>{l}</option>)}
            </select>
            <select style={s.sel} value={filterProto} onChange={e => setFilterProto(+e.target.value)}>
              <option value={-1}>All Protocols</option>
              <option value={1}>ICMP</option>
              <option value={6}>TCP</option>
              <option value={17}>UDP</option>
            </select>
            <select style={s.sel} value={filterRange} onChange={e => setFilterRange(e.target.value)}>
              {TIME_RANGES.map(r => <option key={r} value={r}>{r === 'all' ? 'All time' : `Last ${r}`}</option>)}
            </select>
            <select style={s.sel} value={pageSize} onChange={e => setPageSize(+e.target.value)}>
              <option value={25}>25 / page</option>
              <option value={50}>50 / page</option>
              <option value={100}>100 / page</option>
              <option value={250}>250 / page</option>
            </select>
            <button style={s.refreshBtn} onClick={fetchLogs}>Refresh</button>
          </div>
        </div>

        <div style={{ overflowX: 'auto' }}>
          <table style={s.table}>
            <thead>
              <tr>
                {['Time', 'Src IP:Port', 'Dst IP:Port', 'Proto', 'Action', 'Size'].map(h => (
                  <th key={h} style={s.th}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visibleLogs.length === 0 && (
                <tr><td colSpan={6} style={{ ...s.td, color: '#475569', textAlign: 'center' }}>No logs</td></tr>
              )}
              {visibleLogs.map((l, idx) => (
                <tr key={l.id} style={{ ...s.tr, background: idx % 2 === 0 ? '#FFFFFF' : '#EBF0F5' }}>
                  <td style={s.td}>{tsLabel(l.timestamp_ns)}</td>
                  <td style={s.td}>{l.src_ip}{l.src_port ? `:${l.src_port}` : ''}</td>
                  <td style={s.td}>{l.dst_ip}{l.dst_port ? `:${l.dst_port}` : ''}</td>
                  <td style={s.td}>{protoLabel(l.protocol)}</td>
                  <td style={s.td}>
                    <span style={{ ...s.badge, background: (ACTION_COLORS[l.action] ?? { bg: '#EEEEEE', color: '#333' }).bg, color: (ACTION_COLORS[l.action] ?? { bg: '#EEEEEE', color: '#333' }).color }}>
                      {ACTION_LABELS[l.action] ?? l.action}
                    </span>
                  </td>
                  <td style={s.td}>{l.pkt_len} B</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {totalPages > 1 && (
          <div style={s.pagination}>
            {/* First page */}
            <button
              style={{ ...s.pageBtn, ...(page === 1 ? s.pageBtnDisabled : {}) }}
              disabled={page === 1}
              onClick={() => setPage(1)}
              title="First page"
            >⟪</button>
            {/* Prev page */}
            <button
              style={{ ...s.pageBtn, ...(page === 1 ? s.pageBtnDisabled : {}) }}
              disabled={page === 1}
              onClick={() => setPage(p => Math.max(1, p - 1))}
              title="Previous page"
            >‹</button>

            {/* Page numbers */}
            {pageButtons(page, totalPages).map((btn, i) =>
              btn === '…'
                ? <span key={`ellipsis-${i}`} style={s.pageDots}>…</span>
                : <button
                    key={btn}
                    style={{ ...s.pageBtn, ...(btn === page ? s.pageBtnActive : {}) }}
                    onClick={() => setPage(btn)}
                  >{btn}</button>
            )}

            {/* Next page */}
            <button
              style={{ ...s.pageBtn, ...(page === totalPages ? s.pageBtnDisabled : {}) }}
              disabled={page === totalPages}
              onClick={() => setPage(p => Math.min(totalPages, p + 1))}
              title="Next page"
            >›</button>
            {/* Last page */}
            <button
              style={{ ...s.pageBtn, ...(page === totalPages ? s.pageBtnDisabled : {}) }}
              disabled={page === totalPages}
              onClick={() => setPage(totalPages)}
              title="Last page"
            >⟫</button>
          </div>
        )}
      </section>
    </div>
  )
}

const ACTION_COLORS: Record<number, { bg: string; color: string }> = {
  0: { bg: '#FFCDD2', color: '#B71C1C' }, // DROP
  1: { bg: '#C8E6C9', color: '#1B5E20' }, // PASS
  2: { bg: '#BBDEFB', color: '#0D47A1' }, // TX
  3: { bg: '#E1BEE7', color: '#4A148C' }, // REDIRECT
  4: { bg: '#FFE0B2', color: '#E65100' }, // TTL_EXCEEDED
}

const s: Record<string, React.CSSProperties> = {
  title: { fontSize: 20, fontWeight: 600, color: '#1A1A1A', marginBottom: 20 },
  notice: {
    padding: '12px 16px', background: '#FFF3CD', borderRadius: 2,
    border: '1px solid #F6A800', color: '#7A5200', marginBottom: 16, fontSize: 13,
  },
  card: {
    background: '#FFFFFF', borderRadius: 2, padding: 20,
    marginBottom: 16, border: '1px solid #C8C8C8',
    boxShadow: '0 1px 3px rgba(0,0,0,0.08)',
  },
  cardTitle: { fontSize: 13, fontWeight: 600, color: '#3A3A3A', marginBottom: 16, textTransform: 'uppercase' as const, letterSpacing: '0.05em' },
  logHeader: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 },
  filters: { display: 'flex', gap: 8, flexWrap: 'wrap' },
  sel: {
    background: '#FFFFFF', border: '1px solid #C8C8C8', borderRadius: 2,
    color: '#1A1A1A', padding: '4px 8px', fontSize: 12,
  },
  refreshBtn: {
    padding: '4px 10px', background: '#FFFFFF', border: '1px solid #C8C8C8',
    borderRadius: 2, color: '#1A1A1A', cursor: 'pointer', fontSize: 12,
  },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 12.5 },
  th: {
    padding: '8px 10px', textAlign: 'left', color: '#3A4A58',
    background: '#D8E0E8', borderBottom: '1px solid #B8C4CE',
    fontWeight: 600, whiteSpace: 'nowrap',
    fontSize: 11, textTransform: 'uppercase' as const, letterSpacing: '0.05em',
  },
  td: { padding: '7px 10px', color: '#1A1A1A', borderBottom: '1px solid #E8E8E8' },
  tr: {} as React.CSSProperties,
  badge: {
    display: 'inline-block', padding: '2px 7px', borderRadius: 2,
    fontSize: 11, fontWeight: 600,
  },
  pagination: {
    display: 'flex', justifyContent: 'flex-end', alignItems: 'center',
    gap: 4, marginTop: 12, flexWrap: 'wrap',
  },
  pageBtn: {
    minWidth: 30, height: 26, background: '#FFFFFF', border: '1px solid #C8C8C8',
    borderRadius: 2, color: '#5A5A5A', cursor: 'pointer', fontSize: 12,
    padding: '0 6px',
  },
  pageBtnActive: {
    background: '#F6A800', borderColor: '#D99A00', color: '#1A1A1A',
  },
  pageBtnDisabled: {
    opacity: 0.4, cursor: 'default',
  },
  pageDots: {
    color: '#AAAAAA', fontSize: 12, padding: '0 2px', userSelect: 'none',
  },
}
