import { api, ApiError } from '../api.js'
import type { Bundle, CampusMap, Draft, ParseResult } from '../types'

const React = window.React
const { useMemo, useState } = React

interface Props {
  map: CampusMap
  bundle: Bundle | null
  onCreated: (bundle: Bundle) => void
  onNewTask: () => void
}

const EXAMPLES = [
  '从计算机学院取3份实验材料，分别送到科研楼、实验室和学一楼，实验室材料优先，16:00前完成。',
  '从后勤服务中心取2箱饮用水送到图书馆和学一楼，图书馆优先。',
  '从计算机学院取5份会议资料送到科研楼和后勤服务中心，17点前送到。',
]

const PRIORITY_LABEL: Record<string, string> = { urgent: '优先', normal: '普通' }

const CATEGORY_LABEL: Record<string, string> = {
  teaching: '教学与科研',
  dorm: '学生公寓',
  service: '生活服务',
  facility: '场馆与后勤',
  other: '其他',
}

export default function TaskComposer({ map, bundle, onCreated, onNewTask }: Props) {
  const [text, setText] = useState(EXAMPLES[0])
  const [parsed, setParsed] = useState<ParseResult | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [bulkItems, setBulkItems] = useState('')

  const locations = useMemo(() => map.nodes.filter((n) => n.kind === 'location'), [map])

  const groupedLocations = useMemo(() => {
    const groups = new Map<string, typeof locations>()
    for (const node of locations) {
      const key = node.category || 'other'
      const bucket = groups.get(key) || []
      bucket.push(node)
      groups.set(key, bucket)
    }
    return Array.from(groups.entries())
  }, [locations])

  const locationName = (id: string) => locations.find((n) => n.id === id)?.name || id

  async function handleParse() {
    setBusy(true)
    setError(null)
    try {
      const result = await api.parse(text.trim())
      setParsed(result)
      setDraft({ ...result.draft, stops: result.draft.stops.map((s) => ({ ...s, items: [...s.items] })) })
      setBulkItems('')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '解析失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }

  function updateStop(index: number, patch: Partial<Draft['stops'][number]>) {
    if (!draft) return
    setDraft({ ...draft, stops: draft.stops.map((stop, i) => (i === index ? { ...stop, ...patch } : stop)) })
  }

  function removeStop(index: number) {
    if (!draft) return
    setDraft({ ...draft, stops: draft.stops.filter((_, i) => i !== index) })
  }

  function addStop() {
    if (!draft) return
    const used = new Set([draft.pickup_location_id, ...draft.stops.map((s) => s.location_id)])
    const candidate = locations.find((n) => !used.has(n.id))
    if (!candidate) {
      setError('没有更多可添加的校园地点')
      return
    }
    setDraft({ ...draft, stops: [...draft.stops, { location_id: candidate.id, items: [], priority: 'normal' }] })
  }

  function applyBulkItems() {
    if (!draft) return
    const items = bulkItems
      .split(/[，,、]/)
      .map((item) => item.trim())
      .filter(Boolean)
    if (!items.length) {
      setError('请先填写物品名称，例如“3份实验材料”')
      return
    }
    setDraft({ ...draft, stops: draft.stops.map((stop) => ({ ...stop, items: [...items] })) })
    setError(null)
  }

  async function handleCreate() {
    if (!draft) return
    if (!draft.pickup_location_id) {
      setError('请选择取货点')
      return
    }
    if (!draft.stops.length) {
      setError('请至少添加一个配送站点')
      return
    }
    if (draft.stops.some((s) => !s.items.length)) {
      setError('每个站点都必须分配物品，不能只写一句全局物品说明')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const created = await api.createTask({
        mode: draft.mode,
        pickup_location_id: draft.pickup_location_id,
        stops: draft.stops,
        deadline: draft.deadline_kind === 'clock' ? draft.deadline : null,
        deadline_kind: draft.deadline_kind,
        raw_input: text.trim(),
        parser: parsed?.parser || 'manual',
      })
      setParsed(null)
      setDraft(null)
      onCreated(created)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '创建任务失败')
    } finally {
      setBusy(false)
    }
  }

  if (bundle) {
    const task = bundle.task
    return (
      <div className="panel">
        <div className="panel-title">
          <span>当前任务</span>
          <span className={`status-badge status-${task.status}`}>{statusLabel(task.status)}</span>
        </div>
        <dl className="task-facts">
          <dt>任务编号</dt>
          <dd className="mono">{task.id}</dd>
          <dt>原始需求</dt>
          <dd className="task-raw">{task.raw_input || '（手工创建）'}</dd>
          <dt>取货点</dt>
          <dd>{task.pickup_location_name}</dd>
          <dt>截止时间</dt>
          <dd>{task.deadline_kind === 'clock' && task.deadline ? task.deadline : '无截止时间'}</dd>
          <dt>解析方式</dt>
          <dd>
            <span className={`chip ${task.parser.startsWith('llm') ? 'chip-real' : 'chip-sim'}`}>
              {task.parser.startsWith('llm') ? '真实模型解析' : '规则模拟解析'}
            </span>
          </dd>
          <dt>执行模式</dt>
          <dd>{task.mode === 'sim' ? '模拟执行（演示）' : '真实人工交付'}</dd>
          <dt>取货状态</dt>
          <dd>{task.pickup_status === 'picked_up' ? '已确认取货' : '待取货'}</dd>
        </dl>
        <button className="btn btn-ghost btn-block" onClick={onNewTask}>
          新建任务
        </button>
        <p className="hint">
          模拟模式由虚拟位置执行器驱动，仅用于演示与测试，记录与真实模式隔离，不计入真实配送成果。
        </p>
      </div>
    )
  }

  const unknownLocations = (parsed?.missing_fields || [])
    .filter((field) => field.startsWith('unknown_location:'))
    .map((field) => field.slice('unknown_location:'.length))

  const emptyStopIndexes = (draft?.stops || [])
    .map((stop, index) => (stop.items.length ? -1 : index))
    .filter((index) => index >= 0)

  const issues: Array<{ key: string; question: string; action: any; blocking: boolean }> = []

  if (draft && !draft.pickup_location_id) {
    issues.push({
      key: 'pickup',
      question: '从哪里取货？',
      blocking: true,
      action: (
        <select
          className="input input-sm"
          value={draft.pickup_location_id}
          onChange={(event) => setDraft({ ...draft, pickup_location_id: event.target.value })}
        >
          <option value="">请选择取货点</option>
          {groupedLocations.map(([key, items]) => (
            <optgroup key={key} label={CATEGORY_LABEL[key] || key}>
              {items.map((node) => (
                <option key={node.id} value={node.id}>
                  {node.name}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      ),
    })
  }

  if (draft && !draft.stops.length) {
    issues.push({
      key: 'stops',
      question: '要送到哪些地点？至少添加一个配送站点。',
      blocking: true,
      action: (
        <button className="chip chip-button" onClick={addStop}>
          + 添加配送站点
        </button>
      ),
    })
  }

  if (draft && emptyStopIndexes.length) {
    issues.push({
      key: 'items',
      question: `第 ${emptyStopIndexes.map((index) => index + 1).join('、')} 站还没有物品。请逐站填写，或填一次统一物品后应用。`,
      blocking: true,
      action: (
        <div className="ask-actions">
          <input
            className="input input-sm"
            value={bulkItems}
            onChange={(event) => setBulkItems(event.target.value)}
            placeholder="例如：3份实验材料"
          />
          <button className="chip chip-button" onClick={applyBulkItems}>
            应用到所有站点
          </button>
        </div>
      ),
    })
  }

  unknownLocations.forEach((name) => {
    issues.push({
      key: `unknown-${name}`,
      question: `原文里的“${name}”不在校园地点白名单内，系统不会自动替换。`,
      blocking: false,
      action: <p className="hint">请从下方站点列表选择正确地点，或删除后重新添加。</p>,
    })
  })

  const blockingIssues = issues.filter((issue) => issue.blocking)

  return (
    <div className="panel">
      <div className="panel-title">
        <span>任务创建</span>
        <span className="chip chip-sim">自然语言解析</span>
      </div>

      <label className="field">
        <span className="field-label">描述配送需求</span>
        <textarea
          className="input textarea"
          rows={5}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="例如：从计算机学院取 3 份实验材料，送到科研楼、实验楼和学一楼，实验室优先，16:00 前完成。"
        />
      </label>
      <div className="example-row">
        {EXAMPLES.map((example, index) => (
          <button key={index} className="chip chip-button" onClick={() => setText(example)}>
            示例 {index + 1}
          </button>
        ))}
      </div>

      <button className="btn btn-primary btn-block" onClick={handleParse} disabled={busy || !text.trim()}>
        {busy ? '解析中…' : '解析需求'}
      </button>

      {error ? <p className="error-text">{error}</p> : null}

      {parsed && draft ? (
        <div className="draft">
          <div className="draft-head">
            <span>解析结果</span>
            <span className={`chip ${parsed.parser.startsWith('llm') ? 'chip-real' : 'chip-sim'}`}>
              {parsed.parser.startsWith('llm') ? '真实模型解析' : '规则模拟解析'}
            </span>
          </div>

          {issues.length ? (
            <div className="ask-panel">
              <div className="ask-head">
                <strong>{blockingIssues.length ? `还需要补充 ${blockingIssues.length} 项` : '信息已基本完整'}</strong>
                {issues.length > blockingIssues.length ? (
                  <span className="muted">另有 {issues.length - blockingIssues.length} 条地点提示</span>
                ) : null}
              </div>
              {issues.map((issue, index) => (
                <div className={`ask-item${issue.blocking ? '' : ' ask-item-note'}`} key={issue.key}>
                  <span className="ask-index">{index + 1}</span>
                  <div className="ask-body">
                    <p className="ask-question">{issue.question}</p>
                    {issue.action}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="ask-done">信息已齐全，可以创建任务。</p>
          )}

          <div className="form-grid">
            <label className="field">
              <span className="field-label">取货点</span>
              <select
                className="input"
                value={draft.pickup_location_id}
                onChange={(event) => setDraft({ ...draft, pickup_location_id: event.target.value })}
              >
                <option value="">请选择</option>
                {groupedLocations.map(([key, items]) => (
                  <optgroup key={key} label={CATEGORY_LABEL[key] || key}>
                    {items.map((node) => (
                      <option key={node.id} value={node.id}>
                        {node.name}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="field-label">执行模式</span>
              <select
                className="input"
                value={draft.mode}
                onChange={(event) => setDraft({ ...draft, mode: event.target.value as Draft['mode'] })}
              >
                <option value="sim">模拟执行（演示）</option>
                <option value="real">真实人工交付</option>
              </select>
            </label>
          </div>

          <div className="stops-editor">
            <div className="stops-editor-head">
              <span>配送站点（{draft.stops.length}/5）</span>
              <button className="chip chip-button" onClick={addStop} disabled={draft.stops.length >= 5}>
                + 添加站点
              </button>
            </div>
            {draft.stops.map((stop, index) => (
              <div className="stop-edit" key={`${stop.location_id}-${index}`}>
                <div className="stop-edit-row">
                  <select
                    className="input input-sm"
                    value={stop.location_id}
                    onChange={(event) => updateStop(index, { location_id: event.target.value })}
                  >
                    {groupedLocations.map(([key, items]) => (
                      <optgroup key={key} label={CATEGORY_LABEL[key] || key}>
                        {items.map((node) => (
                          <option key={node.id} value={node.id}>
                            {node.name}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                  <select
                    className="input input-sm"
                    value={stop.priority}
                    onChange={(event) => updateStop(index, { priority: event.target.value as 'normal' | 'urgent' })}
                  >
                    <option value="normal">普通</option>
                    <option value="urgent">优先</option>
                  </select>
                  <button className="icon-btn" onClick={() => removeStop(index)} aria-label="删除站点">
                    ×
                  </button>
                </div>
                <input
                  className="input input-sm"
                  value={stop.items.join('，')}
                  onChange={(event) =>
                    updateStop(index, {
                      items: event.target.value
                        .split(/[，,、]/)
                        .map((item) => item.trim())
                        .filter(Boolean),
                    })
                  }
                  placeholder="本站物品，用逗号分隔"
                />
              </div>
            ))}
          </div>

          <div className="form-grid">
            <label className="field">
              <span className="field-label">截止时间（可选）</span>
              <input
                className="input"
                type="time"
                value={draft.deadline || ''}
                disabled={draft.deadline_kind !== 'clock'}
                onChange={(event) => setDraft({ ...draft, deadline: event.target.value || null, deadline_kind: 'clock' })}
              />
            </label>
            <label className="check-field">
              <input
                type="checkbox"
                checked={draft.deadline_kind === 'none'}
                onChange={(event) =>
                  setDraft({ ...draft, deadline_kind: event.target.checked ? 'none' : 'clock', deadline: event.target.checked ? null : draft.deadline })
                }
              />
              <span>无截止时间</span>
            </label>
          </div>

          {parsed.notes.length ? (
            <ul className="note-list">
              {parsed.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          ) : null}

          <button className="btn btn-primary btn-block" onClick={handleCreate} disabled={busy || blockingIssues.length > 0}>
            {busy ? '创建中…' : blockingIssues.length ? `还有 ${blockingIssues.length} 项待补充` : '确认任务信息'}
          </button>
          <p className="hint">确认后任务进入“待开始”，仍可在右侧确认配送计划。</p>
        </div>
      ) : null}
    </div>
  )
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    ready: '待开始',
    in_progress: '执行中',
    paused: '已暂停',
    blocked: '待处理',
    completed: '已完成',
    cancelled: '已取消',
  }
  return labels[status] || status
}

function fieldLabel(field: string): string {
  const labels: Record<string, string> = {
    pickup_location: '取货地点',
    delivery_stops: '配送站点',
    items: '物品',
    item_allocation: '每站物品分配',
  }
  return labels[field] || field
}