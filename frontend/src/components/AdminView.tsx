import { api, ApiError, newRequestId } from '../api.js'
import type { Bundle, TaskSummary } from '../types'

const React = window.React
const { useEffect, useState } = React

interface Props {
  onOpen: (id: string) => void
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

const FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'unassigned', label: '待接单' },
  { key: 'active', label: '执行中' },
  { key: 'completed', label: '已完成' },
]

type FilterKey = (typeof FILTERS)[number]['key']

export default function AdminView({ onOpen, onError }: Props) {
  const [tasks, setTasks] = useState<TaskSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<FilterKey>('all')
  const [assignee, setAssignee] = useState(() => window.localStorage.getItem('cp.admin.name') || '')
  const [selected, setSelected] = useState<Bundle | null>(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  async function load() {
    setLoading(true)
    try {
      const result = await api.listTasks()
      setTasks(result.tasks)
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '加载任务列表失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  function matchFilter(task: TaskSummary, key: FilterKey): boolean {
    if (key === 'unassigned') return !task.assignee && task.status !== 'completed' && task.status !== 'cancelled'
    if (key === 'active') return ['in_progress', 'paused', 'blocked'].includes(task.status)
    if (key === 'completed') return task.status === 'completed'
    return true
  }

  const visible = tasks.filter((task) => matchFilter(task, filter))

  async function handleOpen(taskId: string) {
    try {
      setSelected(await api.getTask(taskId))
      setNote('')
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '打开任务失败')
    }
  }

  async function handleAccept(taskId: string) {
    const name = assignee.trim()
    if (!name) {
      onError('请先在右上角填写接单人姓名或工号')
      return
    }
    window.localStorage.setItem('cp.admin.name', name)
    setBusy(true)
    try {
      const bundle = await api.acceptTask(taskId, name)
      setSelected(bundle)
      await load()
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '接单失败')
    } finally {
      setBusy(false)
    }
  }

  async function handleStatus(action: string) {
    if (!selected) return
    setBusy(true)
    try {
      const next = await api.action(selected.task.id, action, newRequestId(), selected.task.plan_version)
      setSelected(next)
      await load()
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '状态更新失败')
    } finally {
      setBusy(false)
    }
  }

  async function handleNote() {
    if (!selected) return
    if (!note.trim()) {
      onError('备注内容不能为空')
      return
    }
    setBusy(true)
    try {
      const next = await api.addNote(selected.task.id, assignee.trim() || '管理员', note.trim())
      setSelected(next)
      setNote('')
      await load()
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '添加备注失败')
    } finally {
      setBusy(false)
    }
  }

  const deliveredAll = selected ? selected.stops.length > 0 && selected.stops.every((stop) => stop.status === 'delivered') : false

  return (
    <div className="page-panel">
      <div className="page-head">
        <div>
          <h2>管理员视图</h2>
          <p className="muted">接单、更新任务状态、记录处理备注。管理员只改业务状态，不改路网与计划计算。</p>
        </div>
        <label className="admin-name">
          <span>接单人</span>
          <input
            className="input input-sm"
            value={assignee}
            onChange={(event) => setAssignee(event.target.value)}
            placeholder="姓名或工号"
          />
        </label>
      </div>

      <div className="admin-filters">
        {FILTERS.map((item) => (
          <button
            key={item.key}
            className={`chip chip-button ${filter === item.key ? 'active' : ''}`}
            onClick={() => setFilter(item.key)}
          >
            {item.label}（{tasks.filter((task) => matchFilter(task, item.key)).length}）
          </button>
        ))}
        <button className="btn btn-ghost btn-sm" onClick={load} disabled={loading}>
          刷新
        </button>
      </div>

      <div className="admin-grid">
        <div className="admin-list">
          {loading ? <p className="muted">加载中…</p> : null}
          {!loading && !visible.length ? <p className="muted">没有符合条件的任务。</p> : null}
          {visible.map((task) => (
            <button
              className={`admin-row ${selected?.task.id === task.id ? 'active' : ''}`}
              key={task.id}
              onClick={() => handleOpen(task.id)}
            >
              <div className="admin-row-head">
                <strong className="mono">{task.id}</strong>
                <span className={`status-badge status-${task.status}`}>{STATUS_TEXT[task.status] || task.status}</span>
              </div>
              <div className="admin-row-meta">
                <span>{task.pickup_location_name} 取货</span>
                <span className="mono">{task.delivered_stops}/{task.total_stops} 站</span>
                <span>{task.deadline ? `${task.deadline} 前` : '无截止'}</span>
              </div>
              <div className="admin-row-foot">
                <span className="muted">{task.assignee ? `接单：${task.assignee}` : '未接单'}</span>
                {task.assignee ? null : <span className="chip chip-sim">待接单</span>}
              </div>
            </button>
          ))}
        </div>

        <div className="admin-detail">
          {!selected ? (
            <div className="empty-state">
              <p>选择左侧任务查看详情。</p>
              <p className="muted">接单后会记录接单人和时间，备注会写入任务时间线。</p>
            </div>
          ) : (
            <>
              <div className="admin-detail-head">
                <div>
                  <strong className="mono">{selected.task.id}</strong>
                  <p className="muted">{selected.task.raw_input || '（手工创建）'}</p>
                </div>
                <span className={`status-badge status-${selected.task.status}`}>
                  {STATUS_TEXT[selected.task.status] || selected.task.status}
                </span>
              </div>

              <dl className="task-facts">
                <dt>取货点</dt>
                <dd>{selected.task.pickup_location_name}</dd>
                <dt>截止时间</dt>
                <dd>{selected.task.deadline_kind === 'clock' && selected.task.deadline ? selected.task.deadline : '无截止时间'}</dd>
                <dt>接单人</dt>
                <dd>{selected.assignment?.assignee || '未接单'}</dd>
                <dt>接单时间</dt>
                <dd className="mono">{selected.assignment ? selected.assignment.accepted_at.slice(0, 19).replace('T', ' ') : '—'}</dd>
                <dt>计划版本</dt>
                <dd className="mono">v{selected.task.plan_version}</dd>
                <dt>站点进度</dt>
                <dd className="mono">
                  {selected.stops.filter((stop) => stop.status === 'delivered').length}/{selected.stops.length}
                </dd>
              </dl>

              <div className="admin-actions">
                {!selected.assignment ? (
                  <button className="btn btn-primary" onClick={() => handleAccept(selected.task.id)} disabled={busy}>
                    接单
                  </button>
                ) : null}
                {selected.task.status === 'ready' && selected.task.plan_version > 0 ? (
                  <button className="btn btn-secondary" onClick={() => handleStatus('start')} disabled={busy}>
                    开始执行
                  </button>
                ) : null}
                {selected.task.status === 'in_progress' ? (
                  <button className="btn btn-secondary" onClick={() => handleStatus('pause')} disabled={busy}>
                    暂停
                  </button>
                ) : null}
                {selected.task.status === 'paused' || selected.task.status === 'blocked' ? (
                  <button className="btn btn-secondary" onClick={() => handleStatus('resume')} disabled={busy}>
                    继续
                  </button>
                ) : null}
                {selected.task.status === 'in_progress' || selected.task.status === 'paused' ? (
                  <button className="btn btn-ghost" onClick={() => handleStatus('cancel')} disabled={busy}>
                    取消任务
                  </button>
                ) : null}
                {deliveredAll && selected.task.status !== 'completed' ? (
                  <button className="btn btn-primary" onClick={() => handleStatus('complete')} disabled={busy}>
                    完成配送
                  </button>
                ) : null}
                <button className="btn btn-ghost" onClick={() => onOpen(selected.task.id)}>
                  在工作台打开
                </button>
              </div>

              {selected.task.plan_version < 1 ? (
                <p className="hint">该任务还没有配送计划，请先在“工作台”生成计划后再开始执行。</p>
              ) : null}

              <div className="admin-notes">
                <div className="timeline-head">处理备注</div>
                {selected.notes.length ? (
                  selected.notes.map((item) => (
                    <div className="note-item" key={item.id}>
                      <div className="note-head">
                        <strong>{item.author}</strong>
                        <span className="mono muted">{item.created_at.slice(11, 19)}</span>
                      </div>
                      <p>{item.content}</p>
                    </div>
                  ))
                ) : (
                  <p className="muted">还没有备注。</p>
                )}
                <div className="note-form">
                  <input
                    className="input input-sm"
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                    placeholder="例如：已联系收件人，物品放在值班室"
                  />
                  <button className="btn btn-mini" onClick={handleNote} disabled={busy}>
                    添加备注
                  </button>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}