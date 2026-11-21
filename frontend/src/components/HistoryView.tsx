import { api, ApiError } from '../api.js'
import type { TaskSummary } from '../types'

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

export default function HistoryView({ onOpen, onError }: Props) {
  const [tasks, setTasks] = useState<TaskSummary[]>([])
  const [loading, setLoading] = useState(true)
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

  async function handleSeed() {
    setBusy(true)
    try {
      await api.seed()
      await load()
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '生成示例任务失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page-panel">
      <div className="page-head">
        <div>
          <h2>历史任务</h2>
          <p className="muted">真实模式与模拟模式的记录相互隔离，不会把模拟结果算作真实配送成果。</p>
        </div>
        <button className="btn btn-ghost" onClick={handleSeed} disabled={busy}>
          {busy ? '生成中…' : '生成示例任务'}
        </button>
      </div>

      {loading ? <p className="muted">加载中…</p> : null}
      {!loading && !tasks.length ? (
        <div className="empty-state">
          <p>还没有任务记录。</p>
          <p className="muted">可以在工作台创建任务，或点击“生成示例任务”载入三条演示数据。</p>
        </div>
      ) : null}

      {tasks.length ? (
        <table className="task-table">
          <thead>
            <tr>
              <th>任务编号</th>
              <th>模式</th>
              <th>状态</th>
              <th>取货点</th>
              <th>站点进度</th>
              <th>截止</th>
              <th>更新时间</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.id}>
                <td className="mono">{task.id}</td>
                <td>{task.mode === 'sim' ? '模拟' : '真实'}</td>
                <td>
                  <span className={`status-badge status-${task.status}`}>{STATUS_TEXT[task.status] || task.status}</span>
                </td>
                <td>{task.pickup_location_name}</td>
                <td className="mono">
                  {task.delivered_stops}/{task.total_stops}
                </td>
                <td className="mono">{task.deadline || '无'}</td>
                <td className="mono">{task.updated_at.slice(0, 19).replace('T', ' ')}</td>
                <td>
                  <button className="btn btn-mini" onClick={() => onOpen(task.id)}>
                    打开
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  )
}