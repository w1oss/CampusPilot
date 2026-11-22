export type StopStatus = 'pending' | 'arrived' | 'delivered'
export type TaskStatus = 'ready' | 'in_progress' | 'paused' | 'blocked' | 'completed' | 'cancelled'
export type Priority = 'normal' | 'urgent'
export type Mode = 'real' | 'sim'

export interface MapNode {
  id: string
  name: string
  aliases?: string[]
  kind: 'location' | 'junction' | 'waypoint'
  x: number
  y: number
}

export interface MapBackground {
  buildings: { name: string; polygon: [number, number][] }[]
  roads: { kind: string; name: string; polyline: [number, number][] }[]
  greens: { kind: string; polygon: [number, number][] }[]
}

export interface MapEdge {
  id: string
  name: string
  from: string
  to: string
  bidirectional: boolean
  polyline: [number, number][]
  distance_m: number
  base_status: 'open' | 'congested' | 'blocked'
}

export interface RoadEvent {
  id: string
  edge_id: string
  status: string
  source: string
  confirmed_by: string
  note: string
  created_at: string
  expires_at: string | null
}

export interface CampusMap {
  map_id: string
  map_version: string
  data_notice: string
  coordinate_system: { width: number; height: number; unit: string }
  background_image?: string
  nodes: MapNode[]
  edges: MapEdge[]
  background?: MapBackground
  road_status: Record<string, string>
  road_events: RoadEvent[]
}

export interface StopDraft {
  location_id: string
  items: string[]
  priority: Priority
}

export interface Draft {
  mode: Mode
  pickup_location_id: string
  stops: StopDraft[]
  deadline: string | null
  deadline_kind: 'none' | 'clock'
}

export interface ParseResult {
  draft: Draft
  missing_fields: string[]
  notes: string[]
  parser: string
  raw_text: string
  agent_trace?: { id: string; agent: string; action: string; summary: string }
}

export interface PlanStop {
  stop_id: string
  location_id: string
  location_name: string
  items: string[]
  priority: Priority
  seq: number
  eta: string
  status: StopStatus
}

export interface UnreachableStop {
  stop_id: string
  location_id: string
  location_name: string
  reason: string
}

export interface Plan {
  plan_version: number
  created_at: string
  ordered_stops: PlanStop[]
  route_node_ids: string[]
  route_edge_ids: string[]
  polyline: [number, number][]
  estimated_duration_seconds: number
  assumptions: string[]
  unreachable_stops: UnreachableStop[]
  warnings: string[]
  feasible: boolean
}

export interface Stop {
  id: string
  seq: number
  location_id: string
  location_name: string
  items: string[]
  priority: Priority
  status: StopStatus
  arrived_at: string | null
  delivered_at: string | null
}

export interface Task {
  id: string
  mode: Mode
  raw_input: string
  parser: string
  pickup_location_id: string
  pickup_location_name: string
  deadline: string | null
  deadline_kind: 'none' | 'clock'
  status: TaskStatus
  plan_version: number
  pickup_status: 'pending' | 'picked_up'
  created_at: string
  updated_at: string
}

export interface ExecutionState {
  mode: Mode
  plan_version: number
  current_edge_id: string | null
  edge_progress: number
  last_node_id: string | null
  position: { x: number; y: number }
  updated_at: string
  remaining_edge_ids: string[]
}

export interface SemanticEvent {
  event_id: string
  task_id: string
  obstacle_id: string
  revision: number
  map_version: string
  source?: 'mock_vision_llm' | 'vision_llm'
  observed_at?: string
  lifecycle: 'observed' | 'cleared'
  edge_id: string
  edge_fraction?: number | null
  category: 'construction' | 'pedestrian' | 'crowd' | 'other'
  passability: 'open' | 'congested' | 'blocked' | 'unknown'
  confidence?: number
  expected_duration_s?: number
  summary: string
}

export interface SemanticObstacle {
  obstacle_id: string
  edge_id: string
  edge_fraction?: number | null
  category?: string
  effective_status: string
  active: boolean
  summary?: string
  source?: 'mock_vision_llm' | 'vision_llm' | 'admin'
  authority?: 'normal' | 'hard'
  position_known?: boolean
  clear_evidence?: number
  observed_at?: string
}

export interface SimRuntime {
  enabled: boolean
  speed_mps: number
  hold_reason: string
  auto_resume: boolean
  emergency_hold?: boolean
  waiting: string | null
}

export interface TimelineEntry {
  seq: number
  type: string
  payload: Record<string, unknown>
  plan_version: number | null
  created_at: string
}

export interface TaskAssignment {
  task_id: string
  assignee: string
  note: string
  accepted_at: string
}

export interface TaskNote {
  id: string
  task_id: string
  author: string
  content: string
  created_at: string
}

export interface AgentTrace {
  id: string
  task_id: string | null
  agent: string
  agent_name: string
  action: string
  input_summary: string
  output_summary: string
  tools: string[]
  status: string
  duration_ms: number
  created_at: string
}

export interface AgentSummary {
  total_calls: number
  failed_calls: number
  agents: { agent_name: string; calls: number; failed: number; duration_ms: number }[]
}

export interface ReplanDiff {
  changed: boolean
  moved?: { stop_id: string; from_seq: number | null; to_seq: number | null }[]
  new_unreachable?: UnreachableStop[]
  warnings?: string[]
}

export interface Bundle {
  task: Task
  stops: Stop[]
  plan: Plan | null
  plan_history: Plan[]
  state: ExecutionState | null
  road_status: Record<string, string>
  road_events: RoadEvent[]
  timeline: TimelineEntry[]
  agent_traces: AgentTrace[]
  assignment: TaskAssignment | null
  notes: TaskNote[]
  map_version: string
  replan_diff?: ReplanDiff
  sim?: { waiting: string | null; arrived_stop_ids: string[] }
  runtime?: SimRuntime
  semantic_obstacles?: SemanticObstacle[]
  road_marks?: Record<string, { status: string; sources: string[]; authority: 'normal' | 'hard'; position_known: boolean }>
  upstream_simulation?: { enabled: boolean; configured: boolean; category: 'pedestrian' | 'construction' | 'crowd' | 'box' | 'barrier'; phase: number; elapsed: number; generated_count?: number; last_edge_id?: string | null }
  scene?: { id: string; kind: string; virtual_seconds: number }
}

export interface TaskSummary {
  id: string
  mode: Mode
  status: TaskStatus
  pickup_location_name: string
  deadline: string | null
  plan_version: number
  created_at: string
  updated_at: string
  total_stops: number
  delivered_stops: number
  assignee: string | null
  accepted_at: string | null
}

export interface Report {
  task: {
    id: string
    mode: Mode
    status: TaskStatus
    pickup_location: string
    deadline: string | null
    deadline_kind: string
    parser: string
    raw_input: string
    created_at: string
    plan_version: number
    pickup_status: string
  }
  summary: {
    total_stops: number
    delivered_stops: number
    pending_stops: number
    completion_rate: number
    plan_versions: number
    replan_count: number
    actual_duration_seconds: number | null
    estimated_duration_seconds: number | null
  }
  stops: {
    stop_id: string
    location_id: string
    location_name: string
    items: string[]
    priority: Priority
    status: StopStatus
    arrived_at: string | null
    delivered_at: string | null
  }[]
  latest_plan: PlanStop[]
  plans: {
    plan_version: number
    created_at: string
    ordered_stops: PlanStop[]
    estimated_duration_seconds: number
    assumptions: string[]
    unreachable_stops: UnreachableStop[]
    warnings: string[]
  }[]
  road_events: RoadEvent[]
  timeline: TimelineEntry[]
  delays: string[]
  data_notice: string
  agent_summary?: AgentSummary
  agent_traces?: AgentTrace[]
}
