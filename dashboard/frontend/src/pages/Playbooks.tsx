import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { PlaybookInfo } from '../api/client'
import { PlaybookCard } from '../components/PlaybookCard'
import { LogViewer } from '../components/LogViewer'
import { TrafficControl } from '../components/TrafficControl'

type CardStatus = 'idle' | 'running' | 'paused' | 'done' | 'error' | 'aborted'

interface RunState {
  jobId: string
  playbookId: string
  status: CardStatus
  pauseState: string | null
}

export function Playbooks() {
  const [playbooks, setPlaybooks] = useState<PlaybookInfo[]>([])
  const [runs, setRuns] = useState<Record<string, RunState>>({}) // keyed by playbookId
  const [activeId, setActiveId] = useState<string | null>(null)
  const [allJobId, setAllJobId] = useState<string | null>(null)
  const [allPauseState, setAllPauseState] = useState<string | null>(null)
  const [allVariant, setAllVariant] = useState<'kernel' | 'xdp' | 'vpp'>('kernel')
  const [variant04, setVariant04] = useState<'kernel' | 'xdp' | 'vpp'>('kernel')

  useEffect(() => {
    api.listPlaybooks().then(setPlaybooks).catch(console.error)
  }, [])

  const startRun = async (playbookId: string, variant?: string) => {
    try {
      const { job_id } = await api.runPlaybook(playbookId, variant)
      setRuns(prev => ({
        ...prev,
        [playbookId]: { jobId: job_id, playbookId, status: 'running', pauseState: null },
      }))
      setActiveId(playbookId)
    } catch (e) {
      console.error(e)
    }
  }

  const startAll = async () => {
    try {
      const { job_id } = await api.runAll(allVariant)
      setAllJobId(job_id)
      setAllPauseState(null)
      setActiveId('__all__')
    } catch (e) {
      console.error(e)
    }
  }

  const handleAbort = async (playbookId: string) => {
    const run = runs[playbookId]
    if (!run) return
    try {
      await api.sendSignal(run.jobId, 'abort')
      setRuns(prev => ({ ...prev, [playbookId]: { ...prev[playbookId], status: 'aborted' } }))
    } catch (e) {
      console.error(e)
    }
  }

  const handleStateChange = (playbookId: string) => (status: string, pauseState: string | null) => {
    setRuns(prev => {
      const cur = prev[playbookId]
      if (!cur) return prev
      const newStatus: CardStatus = pauseState ? 'paused' : (status as CardStatus)
      return { ...prev, [playbookId]: { ...cur, status: newStatus, pauseState } }
    })
  }

  const handleDone = (playbookId: string) => (exitCode: number) => {
    setRuns(prev => {
      const cur = prev[playbookId]
      if (!cur) return prev
      return { ...prev, [playbookId]: { ...cur, status: exitCode === 0 ? 'done' : 'error', pauseState: null } }
    })
  }

  const getStatus = (id: string): CardStatus => runs[id]?.status ?? 'idle'

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <h2 style={{ margin: 0, color: '#222' }}>Playbooks</h2>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
            <span style={{ fontSize: 13, color: '#555', fontWeight: 600 }}>Forwarder:</span>
            {(['kernel', 'xdp', 'vpp'] as const).map(v => (
              <label key={v} style={{ cursor: 'pointer', fontSize: 13, color: '#444', display: 'flex', alignItems: 'center', gap: 4 }}>
                <input
                  type="radio"
                  name="allVariant"
                  value={v}
                  checked={allVariant === v}
                  onChange={() => setAllVariant(v)}
                />
                {v === 'kernel' ? 'Kernel' : v === 'xdp' ? 'XDP' : 'VPP'}
              </label>
            ))}
          </div>
          <button
            onClick={startAll}
            style={{
              padding: '8px 22px',
              borderRadius: 6,
              border: 'none',
              background: '#F6A800',
              color: '#fff',
              fontWeight: 700,
              fontSize: 14,
              cursor: 'pointer',
            }}
          >
            Run All
          </button>
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {playbooks.map(pb => {
          const isRunning = getStatus(pb.id) === 'running' || getStatus(pb.id) === 'paused'
          return (
            <div key={pb.id}>
              <PlaybookCard
                playbook={pb}
                status={getStatus(pb.id)}
                onRun={() => startRun(pb.id, pb.id === '04' ? variant04 : undefined)}
                onAbort={() => handleAbort(pb.id)}
              />
              {pb.id === '04' && !isRunning && (
                <div style={{ display: 'flex', gap: 16, marginTop: 6, paddingLeft: 4 }}>
                  {(['kernel', 'xdp', 'vpp'] as const).map(v => (
                    <label key={v} style={{ cursor: 'pointer', fontSize: 13, color: '#444', display: 'flex', alignItems: 'center', gap: 4 }}>
                      <input
                        type="radio"
                        name="variant04"
                        value={v}
                        checked={variant04 === v}
                        onChange={() => setVariant04(v)}
                      />
                      {v === 'kernel' ? 'Kernel' : v === 'xdp' ? 'XDP' : 'VPP'}
                    </label>
                  ))}
                </div>
              )}
              {activeId === pb.id && runs[pb.id] && (
                <div style={{ marginTop: 8 }}>
                  <LogViewer
                    jobId={runs[pb.id].jobId}
                    onStateChange={handleStateChange(pb.id)}
                    onDone={handleDone(pb.id)}
                  />
                  {pb.id === '05' && (
                    <TrafficControl
                      jobId={runs[pb.id].jobId}
                      pauseState={runs[pb.id].pauseState}
                    />
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {allJobId && activeId === '__all__' && (
        <div style={{ marginTop: 24 }}>
          <h3 style={{ color: '#222', marginBottom: 8 }}>Run All — Output</h3>
          <LogViewer
            jobId={allJobId}
            onStateChange={(_status, pauseState) => setAllPauseState(pauseState)}
            onDone={() => setAllPauseState(null)}
          />
          <TrafficControl jobId={allJobId} pauseState={allPauseState} />
        </div>
      )}
    </div>
  )
}
