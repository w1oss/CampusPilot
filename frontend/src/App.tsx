import { api, ApiError } from './api.js'
import AdminView from './components/AdminView.js'
import CampusMapView from './components/CampusMap.js'
import ExecutionPanel from './components/ExecutionPanel.js'
import HistoryView from './components/HistoryView.js'
import ReportView from './components/ReportView.js'
import TaskComposer from './components/TaskComposer.js'
import type { Bundle, CampusMap as CampusMapData } from './types'

const React = window.React
const { useEffect, useState } = React

type View = 'workspace' | 'history' | 'admin' | 'report'

export default function App() {
  const [map, setMap] = useState<CampusMapData | null>(null)
  const [bundle, setBundle] = useState<Bundle | null>(null)
  const [view, setView] = useState<View>('workspace')
  const [error, setError] = useState<string | null>(null)
  const [health, setHealth] = useState<{ llm_configured: boolean; map_version: string; semantic_bridge_enabled: boolean } | null>(null)
  const [autoSim, setAutoSim] = useState(false)
  const [simSpeed, setSimSpeed] = useState(40)
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([api.map(), api.health()])
      .then(([mapData, healthData]) => {
        setMap(mapData)
        setHealth(healthData)
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : '无法连接后端服务，请确认 FastAPI 已启动'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    if (!error) return
    const timer = window.setTimeout(() => setError(null), 6000)
    return () => window.clearTimeout(timer)
  }, [error])

  const taskId = bundle?.task.id
  const taskStatus = bundle?.task.status
  const planVersion = bundle?.task.plan_version
  const bridgeEnabled = health?.semantic_bridge_enabled === true

  // Polling only reads state. Closing/navigating away from the page never owns
  // or advances the vehicle when the backend runtime is enabled.
  useEffect(() => {
    if (!bridgeEnabled || !taskId) return
    let cancelled = false
    let timer: number | undefined
    async function poll() {
      try {
        const next = await api.getTask(taskId!)
        if (cancelled) return
        setBundle(next)
        setAutoSim(!!next.runtime?.enabled)
        if (next.runtime) setSimSpeed(next.runtime.speed_mps)
      } catch (err) {
        if (!cancelled) setError(err instanceof ApiError ? err.message : '位置更新失败')
      }
      if (!cancelled) timer = window.setTimeout(poll, 200)
    }
    timer = window.setTimeout(poll, 200)
    return () => { cancelled = true; if (timer) window.clearTimeout(timer) }
  }, [bridgeEnabled, taskId])

  useEffect(() => {
    if (bridgeEnabled || !autoSim || !taskId || taskStatus !== 'in_progress') return
    let cancelled = false
    let timer: number | undefined

    const tick = async () => {
      try {
        const next = await api.simTick(taskId, 0.5, simSpeed)
        if (cancelled) return
        setBundle(next)
        if (next.sim?.waiting || next.task.status !== 'in_progress') {
          setAutoSim(false)
          return
        }
        timer = window.setTimeout(tick, 150)
      } catch (err) {
        if (!cancelled) {
          setAutoSim(false)
          setError(err instanceof ApiError ? err.message : '模拟推进失败')
        }
      }
    }

    timer = window.setTimeout(tick, 250)
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [bridgeEnabled, autoSim, taskId, taskStatus, planVersion, simSpeed])

  async function configureRuntime(enabled: boolean, speed: number) {
    if (!bridgeEnabled || !taskId) {
      setAutoSim(enabled)
      setSimSpeed(speed)
      return
    }
    try {
      const source = bundle?.upstream_simulation
      const next = await api.setRuntime(taskId, enabled, speed, source?.configured ? source.enabled : enabled, source?.category || 'pedestrian')
      setBundle(next)
      setAutoSim(!!next.runtime?.enabled)
      setSimSpeed(speed)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '后台模拟设置失败')
    }
  }

  async function openTask(id: string) {
    try {
      const loaded = await api.getTask(id)
      setBundle(loaded)
      setSelectedEdge(null)
      setAutoSim(!!loaded.runtime?.enabled)
      if (loaded.runtime) setSimSpeed(loaded.runtime.speed_mps)
      setView('workspace')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '无法打开任务')
    }
  }

  function startNewTask() {
    setBundle(null)
    setSelectedEdge(null)
    setAutoSim(false)
    setView('workspace')
  }

  function refresh(next: Bundle) {
    setBundle(next)
    if (bridgeEnabled) setAutoSim(!!next.runtime?.enabled)
    else if (next.task.status !== 'in_progress') setAutoSim(false)
  }

  if (loading) {
    return (
      <div className="boot-screen">
        <div className="boot-mark">CP</div>
        <p>正在连接 CampusPilot 后端…</p>
      </div>
    )
  }

  if (!map) {
    return (
      <div className="boot-screen">
        <div className="boot-mark">CP</div>
        <p>{error || '路网数据加载失败'}</p>
        <p className="muted">请先在 backend 目录启动 FastAPI：python -m uvicorn app.main:app --reload --port 8000</p>
      </div>
    )
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">CP</div>
          <div>
            <h1>CampusPilot</h1>
            <p>校园低速配送任务 Agent</p>
          </div>
        </div>
        <nav className="nav">
          <button className={view === 'workspace' ? 'nav-btn active' : 'nav-btn'} onClick={() => setView('workspace')}>
            工作台
          </button>
          <button className={view === 'history' ? 'nav-btn active' : 'nav-btn'} onClick={() => setView('history')}>
            历史任务
          </button>
          <button className={view === 'admin' ? 'nav-btn active' : 'nav-btn'} onClick={() => setView('admin')}>
            管理员视图
          </button>
          <button className={view === 'report' ? 'nav-btn active' : 'nav-btn'} onClick={() => setView('report')}>
            任务报告
          </button>
        </nav>
        <div className="topbar-meta">
          <span className={`connection ${health ? 'ok' : 'bad'}`} />
          <span className="mono">v{map.map_version}</span>
          <span className={`chip ${health?.llm_configured ? 'chip-real' : 'chip-sim'}`}>
            {health?.llm_configured ? '真实模型已配置' : '规则解析模式'}
          </span>
        </div>
      </header>

      {error ? (
        <div className="toast" role="alert">
          {error}
        </div>
      ) : null}

      {view === 'workspace' ? (
        <main className="workspace">
          <aside className="column column-left">
            <TaskComposer map={map} bundle={bundle} onCreated={(next) => refresh(next)} onNewTask={startNewTask} />
          </aside>
          <section className="column column-center">
            <CampusMapView
              map={map}
              bundle={bundle}
              selectedEdgeId={selectedEdge}
              onSelectEdge={setSelectedEdge}
            />
            <div className="workflow-strip">
              <span className="workflow-step">1 描述需求</span>
              <span className="workflow-arrow">→</span>
              <span className="workflow-step">2 确认任务与计划</span>
              <span className="workflow-arrow">→</span>
              <span className="workflow-step">3 执行与异常处理</span>
              <span className="workflow-arrow">→</span>
              <span className="workflow-step">4 人工交付与报告</span>
            </div>
          </section>
          <aside className="column column-right">
            <ExecutionPanel
              map={map}
              bundle={bundle}
              selectedEdgeId={selectedEdge}
              autoSim={autoSim}
              simSpeed={simSpeed}
              bridgeEnabled={bridgeEnabled}
              onToggleAuto={(enabled) => { void configureRuntime(enabled, simSpeed) }}
              onSpeedChange={(speed) => { void configureRuntime(autoSim, speed) }}
              onSelectEdge={setSelectedEdge}
              onRefresh={refresh}
              onOpenReport={() => setView('report')}
              onError={setError}
            />
          </aside>
        </main>
      ) : null}

      {view === 'history' ? (
        <main className="page">
          <HistoryView onOpen={openTask} onError={setError} />
        </main>
      ) : null}

      {view === 'admin' ? (
        <main className="page">
          <AdminView onOpen={openTask} onError={setError} />
        </main>
      ) : null}

      {view === 'report' ? (
        <main className="page">
          <ReportView bundle={bundle} onBack={() => setView('workspace')} onError={setError} />
        </main>
      ) : null}

      <footer className="footer">
        <span>示例校园数据，地点与距离为估算值，未经实地核对。</span>
        <span>模拟执行仅用于演示，真实模式由人员确认交付。</span>
      </footer>
    </div>
  )
}
