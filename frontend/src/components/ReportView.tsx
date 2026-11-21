import { api, ApiError } from '../api.js'
import type { Bundle, Report } from '../types'

const React = window.React
const { useEffect, useState } = React

interface Props {
  bundle: Bundle | null
  onBack: () => void
  onError: (message: string) => void
}

const EVENT_LABEL: Record<string, string> = {
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
}

export default function ReportView({ bundle, onBack, onError }: Props) {
  const [report, setReport] = useState<Report | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!bundle) {
      setReport(null)
      return
    }
    setLoading(true)
    api
      .report(bundle.task.id)
      .then(setReport)
      .catch((err) => onError(err instanceof ApiError ? err.message : '加载报告失败'))
      .finally(() => setLoading(false))
  }, [bundle?.task.id])

  if (!bundle) {
    return (
      <div className="page-panel">
        <div className="empty-state">
          <p>还没有可生成报告的任务。</p>
          <button className="btn btn-ghost" onClick={onBack}>
            返回工作台
          </button>
        </div>
      </div>
    )
  }

  if (loading || !report) {
    return (
      <div className="page-panel">
        <p className="muted">报告生成中…</p>
      </div>
    )
  }

  return (
    <div className="page-panel report">
      <div className="page-head">
        <div>
          <h2>配送任务报告</h2>
          <p className="muted">
            任务 {report.task.id} · {report.task.mode === 'sim' ? '模拟执行（演示）' : '真实人工交付'} · 解析方式{' '}
            {report.task.parser.startsWith('llm') ? '真实模型' : '规则模拟'}
          </p>
        </div>
        <div className="head-actions">
          <button className="btn btn-ghost" onClick={onBack}>
            返回工作台
          </button>
          <button className="btn btn-secondary" onClick={() => window.print()}>
            打印 / 导出 PDF
          </button>
        </div>
      </div>

      <section className="report-grid">
        <div className="metric">
          <span>完成率</span>
          <strong>{Math.round(report.summary.completion_rate * 100)}%</strong>
        </div>
        <div className="metric">
          <span>已交付 / 总站点</span>
          <strong>
            {report.summary.delivered_stops} / {report.summary.total_stops}
          </strong>
        </div>
        <div className="metric">
          <span>计划版本</span>
          <strong>{report.summary.plan_versions}</strong>
        </div>
        <div className="metric">
          <span>重规划次数</span>
          <strong>{report.summary.replan_count}</strong>
        </div>
        <div className="metric">
          <span>实际执行时长</span>
          <strong>{report.summary.actual_duration_seconds ? `${Math.round(report.summary.actual_duration_seconds / 60)} 分` : '未完成'}</strong>
        </div>
        <div className="metric">
          <span>估算配送耗时</span>
          <strong>{report.summary.estimated_duration_seconds ? `${Math.round(report.summary.estimated_duration_seconds / 60)} 分` : '—'}</strong>
        </div>
        <div className="metric">
          <span>Agent 调用</span>
          <strong>{report.agent_summary?.total_calls ?? 0} 次</strong>
        </div>
      </section>

      <section className="report-block">
        <h3>原始需求</h3>
        <p className="report-raw">{report.task.raw_input || '（手工创建，无自然语言输入）'}</p>
        <p className="muted">
          取货点：{report.task.pickup_location} · 截止时间：{report.task.deadline || '无'} · 当前状态：{report.task.status}
        </p>
      </section>

      <section className="report-block">
        <h3>站点交付明细</h3>
        <table className="task-table">
          <thead>
            <tr>
              <th>站点</th>
              <th>物品</th>
              <th>优先级</th>
              <th>状态</th>
              <th>到达时间</th>
              <th>交付时间</th>
            </tr>
          </thead>
          <tbody>
            {report.stops.map((stop) => (
              <tr key={stop.stop_id}>
                <td>{stop.location_name}</td>
                <td>{stop.items.join('、')}</td>
                <td>{stop.priority === 'urgent' ? '优先' : '普通'}</td>
                <td>{stop.status === 'delivered' ? '已交付' : stop.status === 'arrived' ? '已到达' : '待送达'}</td>
                <td className="mono">{stop.arrived_at ? stop.arrived_at.slice(11, 19) : '—'}</td>
                <td className="mono">{stop.delivered_at ? stop.delivered_at.slice(11, 19) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="report-block">
        <h3>计划版本与异常</h3>
        {report.plans.map((plan) => (
          <div className="plan-version" key={plan.plan_version}>
            <div className="plan-version-head">
              <strong>计划 v{plan.plan_version}</strong>
              <span className="muted">{plan.created_at.slice(0, 19).replace('T', ' ')}</span>
            </div>
            <ol>
              {plan.ordered_stops.map((stop) => (
                <li key={stop.stop_id}>
                  {stop.location_name}（预计 {stop.eta}，{stop.priority === 'urgent' ? '优先' : '普通'}，{stop.items.join('、')}）
                </li>
              ))}
            </ol>
            {plan.warnings.length ? (
              <ul className="warning-list">
                {plan.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            ) : null}
            {plan.unreachable_stops.length ? (
              <p className="error-text">
                不可达：{plan.unreachable_stops.map((stop) => stop.location_name).join('、')}
              </p>
            ) : null}
          </div>
        ))}
      </section>

      {report.agent_summary && report.agent_summary.total_calls > 0 ? (
        <section className="report-block">
          <h3>Agent 协作记录</h3>
          <p className="muted">
            共 {report.agent_summary.total_calls} 次调用，失败/兜底 {report.agent_summary.failed_calls} 次 ·{' '}
            {report.agent_summary.agents.map((item) => `${item.agent_name} ${item.calls} 次`).join(' / ')}
          </p>
          <div className="timeline timeline-wide">
            {(report.agent_traces || []).map((trace) => (
              <div className="timeline-item agent-timeline-item" key={trace.id}>
                <span className="mono">{trace.created_at.slice(11, 19)}</span>
                <span className={`agent-badge agent-${trace.agent}`}>{trace.agent_name}</span>
                <span>{AGENT_ACTION_LABEL[trace.action] || trace.action}</span>
                <span className="muted">{trace.output_summary}</span>
                <span className="muted mono">{trace.duration_ms}ms</span>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      <section className="report-block">
        <h3>操作时间线</h3>
        <div className="timeline timeline-wide">
          {report.timeline.map((entry) => (
            <div className="timeline-item" key={entry.seq}>
              <span className="mono">{entry.created_at.slice(11, 19)}</span>
              <span>{EVENT_LABEL[entry.type] || entry.type}</span>
            </div>
          ))}
        </div>
      </section>

      <p className="report-notice">{report.data_notice}</p>
    </div>
  )
}