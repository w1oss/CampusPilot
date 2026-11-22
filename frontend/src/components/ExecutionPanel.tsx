import { api, ApiError, newRequestId } from '../api.js'
import type { Bundle, CampusMap } from '../types'

const React = window.React
const { useState } = React

interface Props {
  map: CampusMap
  bundle: Bundle | null
  selectedEdgeId: string | null
  selectedFraction: number | null
  autoSim: boolean
  simSpeed: number
  bridgeEnabled: boolean
  onToggleAuto: (value: boolean) => void
  onSpeedChange: (value: number) => void
  onSelectEdge: (edgeId: string, fraction?: number | null) => void
  onRefresh: (bundle: Bundle) => void
  onOpenReport: () => void
  onError: (message: string) => void
}

const STATUS_TEXT: Record<string, string> = {
  ready: '待开始',
  in_progress: '执行中',
  paused: '已暂停',
  blocked: '待处理',
  completed: '已完成',
  cancelled: '已取消',
}

const STOP_TEXT: Record<string, string> = {
  pending: '待送达',
  arrived: '已到达',
  delivered: '已交付',
}

const AGENT_ACTION_LABEL: Record<string, string> = {
  parse_task: '解析需求',
  create_task: '创建任务',
  plan_stops: '生成计划',
  update_status: '更新状态',
  accept_task: '接单',
  note_update: '记录备注',
  replan_task: '重新规划',
  classify_hazard: '分析道路异常',
  export_report: '生成报告',
  observe_road_semantics: '接收上游语义判断',
  decide_dispatch: '协调实时异常',
}

export default function ExecutionPanel({
  map,
  bundle,
  selectedEdgeId,
  selectedFraction,
  autoSim,
  simSpeed,
  bridgeEnabled,
  onToggleAuto,
  onSpeedChange,
  onSelectEdge,
  onRefresh,
  onOpenReport,
  onError,
}: Props) {
  const [busy, setBusy] = useState(false)
  const [roadStatus, setRoadStatus] = useState<'open' | 'congested' | 'blocked'>('blocked')
  const [roadNote, setRoadNote] = useState('')
  const [candidate, setCandidate] = useState<string | null>(null)
  const [candidateNotice, setCandidateNotice] = useState<string | null>(null)
  const [replanNotice, setReplanNotice] = useState<string | null>(null)
  const [upstreamKind, setUpstreamKind] = useState<'construction' | 'pedestrian' | 'crowd' | 'box' | 'barrier'>('pedestrian')
  const [hardBlock, setHardBlock] = useState(false)
  const [positionUnknown, setPositionUnknown] = useState(true)
  const [truthNotice, setTruthNotice] = useState<string | null>(null)

  if (!bundle) {
    return (
      <div className="panel">
        <div className="panel-title">执行控制</div>
        <div className="empty-state">
          <p>还没有任务。</p>
          <p className="muted">在左侧输入配送需求并确认任务后，这里会出现计划、站点清单和执行按钮。</p>
        </div>
      </div>
    )
  }

  const task = bundle.task
  const plan = bundle.plan
  const deliveredAll = bundle.stops.length > 0 && bundle.stops.every((s) => s.status === 'delivered')
  const activeStop = bundle.stops.find((s) => s.status !== 'delivered')
  const readOnly = task.status === 'completed' || task.status === 'cancelled'

  async function configureUpstream(enabled: boolean, category = upstreamKind) {
    return run(() => api.setRuntime(task.id, !!bundle?.runtime?.enabled, simSpeed, enabled, category))
  }


  async function resetScene() {
    const sceneId = bundle?.scene?.id || 'sim_default'
    await run(async () => {
      await api.resetScene(sceneId)
      setTruthNotice(null)
      return api.getTask(task.id)
    })
  }

  async function scriptRecoverTruth(obstacleId: string) {
    const sceneId = bundle?.scene?.id || 'sim_default'
    await run(async () => {
      const result = await api.clearTruth(sceneId, obstacleId)
      setTruthNotice(result.notice)
      return api.getTask(task.id)
    })
  }

  async function run(work: () => Promise<Bundle>) {
    setBusy(true)
    try {
      const result = await work()
      onRefresh(result)
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '操作失败')
    } finally {
      setBusy(false)
    }
  }

  function action(name: string, extra: Record<string, unknown> = {}) {
    return run(() => api.action(task.id, name, newRequestId(), task.plan_version, extra))
  }

  async function handlePlan() {
    await run(() => api.plan(task.id))
    setReplanNotice(null)
  }

  async function handleAnalyze() {
    if (!selectedEdgeId) {
      onError('请先在地图上点击一条道路，或在下方下拉框中选择道路')
      return
    }
    setBusy(true)
    try {
      const result = await api.analyze(selectedEdgeId, roadNote, task.id)
      setCandidate(result.candidate_status || '未得出明确结论')
      setCandidateNotice(result.notice)
      if (result.candidate_status === 'blocked' || result.candidate_status === 'congested') {
        setRoadStatus(result.candidate_status as 'congested' | 'blocked')
      }
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '分析失败')
    } finally {
      setBusy(false)
    }
  }

  async function handleConfirmRoadEvent() {
    if (!selectedEdgeId) {
      onError('请选择道路')
      return
    }
    if (!positionUnknown && selectedFraction == null) {
      onError('请先在地图上点击该道路的具体位置，或勾选“位置未知（整段受影响）”')
      return
    }
    setBusy(true)
    try {
      await api.createRoadEvent({
        edge_id: selectedEdgeId,
        edge_fraction: positionUnknown ? null : selectedFraction,
        status: roadStatus,
        source: 'user_text',
        authority: hardBlock ? 'hard' : 'normal',
        scene_id: bundle?.scene?.id || 'sim_default',
        confirmed_by: 'operator',
        note: roadNote || '用户上报道路异常',
      })
      const refreshed = await api.getTask(task.id)
      onRefresh(refreshed)
      setCandidate(null)
      setCandidateNotice(null)
      setReplanNotice(bridgeEnabled && autoSim ? '人工路况已保存，后台调度会自动判断是否需要调整路线。' : '路况已更新。请点击“重新规划剩余站点”查看调整后的计划。')
      if (task.mode === 'sim' && autoSim && !bridgeEnabled) {
        onToggleAuto(false)
      }
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '更新路况失败')
    } finally {
      setBusy(false)
    }
  }

  async function handleReplan() {
    setBusy(true)
    try {
      const refreshed = await api.replan(task.id, roadNote || '用户上报道路异常后重规划')
      onRefresh(refreshed)
      const diff = refreshed.replan_diff
      if (diff?.changed && diff.moved?.length) {
        setReplanNotice(`计划版本更新为 v${refreshed.task.plan_version}，${diff.moved.length} 个站点的顺序发生变化。已完成站点保持不变。`)
      } else {
        setReplanNotice(`计划版本更新为 v${refreshed.task.plan_version}。剩余站点顺序未变化或存在不可达站点，请查看提示。`)
      }
      onToggleAuto(false)
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '重规划失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="panel">
      <div className="panel-title">
        <span>执行控制</span>
        <span className={`status-badge status-${task.status}`}>{STATUS_TEXT[task.status] || task.status}</span>
      </div>

      {!plan ? (
        <div className="plan-empty">
          <p className="muted">任务已创建，但还没有配送计划。</p>
          <button className="btn btn-primary btn-block" onClick={handlePlan} disabled={busy}>
            生成并确认配送计划
          </button>
        </div>
      ) : (
        <>
          <div className="plan-summary">
            <div className="plan-summary-row">
              <span>计划版本</span>
              <strong className="mono">v{plan.plan_version}</strong>
            </div>
            <div className="plan-summary-row">
              <span>预计总耗时</span>
              <strong className="mono">{formatDuration(plan.estimated_duration_seconds)}</strong>
            </div>
            <div className="plan-summary-row">
              <span>配送站点</span>
              <strong className="mono">{plan.ordered_stops.length}</strong>
            </div>
            {plan.ordered_stops[0] ? (
              <div className="plan-summary-row">
                <span>预计送达</span>
                <strong className="mono">
                  {plan.ordered_stops[0].eta} – {plan.ordered_stops[plan.ordered_stops.length - 1].eta}
                </strong>
              </div>
            ) : null}
          </div>

          {plan.warnings.length ? (
            <ul className="warning-list">
              {plan.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ) : null}

          {plan.unreachable_stops.length ? (
            <div className="unreachable-box">
              <strong>不可达站点</strong>
              {plan.unreachable_stops.map((stop) => (
                <p key={stop.stop_id}>
                  {stop.location_name}：{stop.reason}
                </p>
              ))}
            </div>
          ) : null}

          <div className="action-grid">
            {task.status === 'ready' ? (
              <button className="btn btn-primary" onClick={() => action('start')} disabled={busy}>
                开始配送
              </button>
            ) : null}
            {task.status === 'in_progress' ? (
              <button className="btn btn-secondary" onClick={() => action('pause')} disabled={busy}>
                暂停
              </button>
            ) : null}
            {task.status === 'paused' || task.status === 'blocked' ? (
              <button className="btn btn-primary" onClick={() => action('resume')} disabled={busy}>
                继续执行
              </button>
            ) : null}
            {task.pickup_status === 'pending' && task.status === 'in_progress' ? (
              <button className="btn btn-accent" onClick={() => action('confirm_pickup')} disabled={busy}>
                确认已取货
              </button>
            ) : null}
            {deliveredAll && task.status !== 'completed' ? (
              <button className="btn btn-primary" onClick={() => action('complete')} disabled={busy}>
                完成配送
              </button>
            ) : null}
            <button className="btn btn-ghost" onClick={onOpenReport}>
              查看报告
            </button>
          </div>

          {task.mode === 'sim' && !readOnly ? (
            <div className="sim-controls">
              <label className="check-field">
                <input type="checkbox" checked={autoSim} onChange={(event) => onToggleAuto(event.target.checked)} />
                <span>{bridgeEnabled ? '后端持续跟踪并推进位置' : '自动推进模拟位置'}</span>
              </label>
              <label className="speed-field">
                <span>演示速度</span>
                <select className="input input-sm" value={simSpeed} onChange={(event) => onSpeedChange(Number(event.target.value))}>
                  <option value={5}>5 m/s</option>
                  <option value={15}>15 m/s</option>
                  <option value={40}>40 m/s</option>
                  <option value={80}>80 m/s</option>
                </select>
              </label>
              <p className="hint">{bridgeEnabled ? '开启后，切换页面或关闭浏览器仍持续运行；到站等待人工交付。' : '演示加速只影响模拟行驶，不改变计划中的估算配送耗时。'}</p>
              {bundle.state ? <p className="hint">当前位置：({bundle.state.position.x}, {bundle.state.position.y}) · 路段 {bundle.state.current_edge_id || '站点'} · 更新于 {new Date(bundle.state.updated_at).toLocaleTimeString()}</p> : null}
              {(bundle.runtime?.waiting || bundle.sim?.waiting) === 'awaiting_pickup' ? <p className="hint hint-strong">车辆在取货点等待，请先确认取货。</p> : null}
              {(bundle.runtime?.waiting || bundle.sim?.waiting) === 'awaiting_delivery' ? <p className="hint hint-strong">车辆已到达站点，等待人工确认交付。</p> : null}
              {bundle.runtime?.hold_reason ? <p className="hint hint-strong">{bundle.runtime.hold_reason}</p> : null}
              {bridgeEnabled && bundle.runtime?.hold_reason ? <button className="btn btn-ghost btn-sm" onClick={() => action('pause')} disabled={busy}>保持人工暂停</button> : null}
            </div>
          ) : null}

          <div className="stops-list">
            <div className="stops-list-head">
              <span>站点清单</span>
              <span className="muted">按当前计划顺序</span>
            </div>
            {plan.ordered_stops.map((planned) => {
              const stop = bundle.stops.find((s) => s.id === planned.stop_id)
              if (!stop) return null
              return (
                <div className={`stop-item stop-${stop.status}`} key={stop.id}>
                  <div className="stop-index">{planned.seq}</div>
                  <div className="stop-main">
                    <div className="stop-title">
                      <strong>{stop.location_name}</strong>
                      {stop.priority === 'urgent' ? <span className="tag tag-urgent">优先</span> : null}
                      <span className={`tag tag-${stop.status}`}>{STOP_TEXT[stop.status]}</span>
                    </div>
                    <div className="stop-meta">
                      <span>预计 {planned.eta}</span>
                      <span>{stop.items.join('、') || '未分配物品'}</span>
                    </div>
                  </div>
                  {task.mode === 'real' && stop.status === 'pending' && !readOnly ? (
                    <button className="btn btn-mini" onClick={() => action('arrive', { stop_id: stop.id })} disabled={busy}>
                      到达
                    </button>
                  ) : null}
                  {stop.status !== 'delivered' && !readOnly ? (
                    <button className="btn btn-mini btn-accent" onClick={() => action('confirm_delivery', { stop_id: stop.id })} disabled={busy}>
                      确认交付
                    </button>
                  ) : null}
                </div>
              )
            })}
          </div>
        </>
      )}

      {bridgeEnabled && task.mode === 'sim' && !readOnly && plan ? (
        <div className="exception-box">
          <div className="exception-head"><strong>上游语义接收与自动模拟</strong><span className="chip">自动决策</span></div>
          <p className="hint">持续接收上游分析结果，收到后直接结合车辆位置决策，无需管理员确认。地图点击仅用于下方的人工上报。</p>
          <label className="check-field">
            <input type="checkbox" checked={!!bundle.upstream_simulation?.enabled} disabled={busy}
              onChange={(event) => { void configureUpstream(event.target.checked) }} />
            <span>启用自动上游模拟源</span>
          </label>
          <label className="field"><span>自动模拟场景</span>
            <select className="input input-sm" value={bundle.upstream_simulation?.enabled ? bundle.upstream_simulation.category : upstreamKind}
              disabled={busy}
              onChange={(event) => {
                const category = event.target.value as typeof upstreamKind
                setUpstreamKind(category)
                if (bundle.upstream_simulation?.enabled) void configureUpstream(true, category)
              }}>
              <option value="pedestrian">行人横穿</option>
              <option value="box">纸箱/小障碍</option>
              <option value="construction">施工围挡</option>
              <option value="barrier">维修栏</option>
              <option value="crowd">人群拥挤</option>
            </select>
          </label>
          <p className="hint">持续观测：施工和维修栏会触发停车与绕行；接近道路连接处 20 米内，可观察所有直接相连的道路方向，包括已封闭方向，不受路段长度限制。十字、丁字、Y 字路口和转弯处都适用。纸箱在 10 米内观察，以紫色小弧线在原路段避让，不重规划。只有明确观测到恢复才解除封路。</p>
          {bundle.upstream_simulation?.enabled ? <p className="candidate">模拟源状态：持续观测中 · 已发现 {bundle.upstream_simulation.generated_count || 0} 个障碍 · 场景时间 {Math.round(bundle.scene?.virtual_seconds || 0)} 秒</p> : <p className="hint">模拟源已关闭；外部上游消息接口仍在监听。</p>}
          <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => { void resetScene() }}>
            重置模拟场景（保留管理员上报）
          </button>
          {bundle.runtime?.hold_reason ? <p className="hint hint-strong">当前调度：{bundle.runtime.hold_reason}</p> : null}
          {(() => {
            const machine = (bundle.semantic_obstacles || []).filter((o) => o.source !== 'admin')
            const admin = (bundle.semantic_obstacles || []).filter((o) => o.source === 'admin')
            const hard = Object.entries(bundle.road_marks || {}).filter(([, mark]) => mark.authority === 'hard')
            const edgeName = (id: string) => map.edges.find((edge) => edge.id === id)?.name || id
            const positionText = (obstacle: { position_known?: boolean; edge_fraction?: number | null }) =>
              obstacle.position_known || obstacle.edge_fraction != null ? '已定位' : '位置未知 · 整段受影响'
            return (
              <>
                {machine.length ? (
                  <>
                    <p className="hint hint-strong">机器观测</p>
                    {machine.map((obstacle) => (
                      <div className="stop-item" key={obstacle.obstacle_id}>
                        <div className="stop-main">
                          <strong>{edgeName(obstacle.edge_id)}（{obstacle.edge_id}）</strong>
                          <p className="hint">{obstacle.summary || '道路异常'} · {positionText(obstacle)}{obstacle.handling === 'local_avoidance' ? ' · 局部避让，道路可通行' : ''}</p>
                        </div>
                        <span className="tag tag-blocked">机器</span>
                      </div>
                    ))}
                  </>
                ) : null}
                {admin.length || hard.length ? (
                  <>
                    <p className="hint hint-strong">管理员上报</p>
                    {admin.map((obstacle) => (
                      <div className="stop-item" key={obstacle.obstacle_id}>
                        <div className="stop-main">
                          <strong>{edgeName(obstacle.edge_id)}（{obstacle.edge_id}）</strong>
                          <p className="hint">
                            {obstacle.summary || '道路异常'} · {positionText(obstacle)}
                            {obstacle.clear_evidence ? ` · 恢复确认 ${obstacle.clear_evidence}/2` : ''}
                          </p>
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-end' }}>
                          <span className="tag tag-blocked">人工</span>
                          <button className="btn btn-ghost btn-sm" disabled={busy}
                            onClick={() => { void scriptRecoverTruth(obstacle.obstacle_id) }}>
                            模拟现场恢复
                          </button>
                        </div>
                      </div>
                    ))}
                    {hard.map(([edgeId]) => (
                      <div className="stop-item" key={`hard-${edgeId}`}>
                        <div className="stop-main">
                          <strong>{edgeName(edgeId)}（{edgeId}）</strong>
                          <p className="hint">管理员强制禁行 · 机器观测不可解除</p>
                        </div>
                        <span className="tag tag-blocked">强制</span>
                      </div>
                    ))}
                  </>
                ) : null}
              </>
            )
          })()}
          {truthNotice ? <p className="hint">{truthNotice}</p> : null}
          {bundle.agent_traces.filter((trace) => trace.action === 'observe_road_semantics').slice(-2).reverse().map((trace) => (
            <p className="hint" key={trace.id}>{trace.input_summary} · {trace.output_summary}</p>
          ))}
        </div>
      ) : null}

      {!readOnly ? (
        <div className="exception-box">
          <div className="exception-head">
            <strong>管理员人工上报道路异常</strong>
            <span className="chip">人工确认后生效</span>
          </div>
          <label className="field">
            <span className="field-label">道路</span>
            <select className="input input-sm" value={selectedEdgeId || ''} onChange={(event) => { onSelectEdge(event.target.value, null); setPositionUnknown(true) }}>
              <option value="">请选择道路（或点击地图）</option>
              {map.edges.map((edge) => (
                <option key={edge.id} value={edge.id}>
                  {edge.name}（{edge.id}）
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">现场说明</span>
            <textarea
              className="input textarea"
              rows={2}
              value={roadNote}
              onChange={(event) => setRoadNote(event.target.value)}
              placeholder="例如：中央大道施工封闭，车辆无法通过"
            />
          </label>
          <div className="form-grid">
            <label className="field">
              <span className="field-label">确认路况</span>
              <select className="input input-sm" value={roadStatus} onChange={(event) => setRoadStatus(event.target.value as 'open' | 'congested' | 'blocked')}>
                <option value="blocked">封闭</option>
                <option value="congested">拥堵</option>
                <option value="open">恢复通行</option>
              </select>
            </label>
            <div className="exception-buttons">
              <button className="btn btn-ghost btn-sm" onClick={handleAnalyze} disabled={busy}>
                分析文字
              </button>
              <button className="btn btn-primary btn-sm" onClick={handleConfirmRoadEvent} disabled={busy}>
                确认并更新路况
              </button>
            </div>
          </div>
          <label className="check-field">
            <input type="checkbox" checked={positionUnknown} onChange={(event) => setPositionUnknown(event.target.checked)} />
            <span>位置未知（整段受影响，机器不会自动解除整段）</span>
          </label>
          {!positionUnknown ? (
            <p className="hint">
              {selectedFraction == null
                ? '请在地图上点击该道路的具体位置'
                : `已选位置：路段 ${Math.round(selectedFraction * 100)}% 处`}
            </p>
          ) : null}
          <label className="check-field">
            <input type="checkbox" checked={hardBlock} onChange={(event) => setHardBlock(event.target.checked)} />
            <span>强制禁行（机器观测不可解除）</span>
          </label>
          {candidate ? (
            <p className="candidate">
              候选判断：<strong>{candidate === 'blocked' ? '封闭' : candidate === 'congested' ? '拥堵' : candidate}</strong>
              {candidateNotice ? <span className="muted"> · {candidateNotice}</span> : null}
            </p>
          ) : null}
          <button className="btn btn-secondary btn-block" onClick={handleReplan} disabled={busy}>
            重新规划剩余站点
          </button>
          {replanNotice ? <p className="replan-notice">{replanNotice}</p> : null}
        </div>
      ) : null}

      {bundle.agent_traces.length ? (
        <div className="agent-trace">
          <div className="timeline-head">
            <span>Agent 协作记录</span>
            <span className="muted">{bundle.agent_traces.length} 次调用</span>
          </div>
          {bundle.agent_traces.slice(-6).reverse().map((trace) => (
            <div className="agent-trace-item" key={trace.id}>
              <span className={`agent-badge agent-${trace.agent}`}>{trace.agent_name}</span>
              <div className="agent-trace-body">
                <strong>{AGENT_ACTION_LABEL[trace.action] || trace.action}</strong>
                <p>{trace.output_summary}</p>
              </div>
              <span className="muted mono">{trace.duration_ms}ms</span>
            </div>
          ))}
        </div>
      ) : null}

      <div className="timeline">
        <div className="timeline-head">操作记录</div>
        {bundle.timeline.slice(-5).reverse().map((entry) => (
          <div className="timeline-item" key={entry.seq}>
            <span className="mono">{entry.created_at.slice(11, 19)}</span>
            <span>{eventLabel(entry.type)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function formatDuration(seconds: number): string {
  const total = Math.round(seconds)
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return `${minutes} 分 ${rest} 秒`
}

function eventLabel(type: string): string {
  const labels: Record<string, string> = {
    task_created: '任务创建',
    plan_generated: '生成配送计划',
    'action:start': '开始配送',
    'action:pause': '暂停',
    'action:resume': '继续',
    'action:complete': '完成任务',
    'action:cancel': '取消任务',
    stop_arrived: '到达站点',
    stop_delivered: '确认交付',
    pickup_confirmed: '确认取货',
    road_event: '道路异常',
    replan: '重新规划',
    semantic_event: '收到上游语义消息',
    semantic_decision: '调度 Agent 完成判断',
  }
  return labels[type] || type
}
