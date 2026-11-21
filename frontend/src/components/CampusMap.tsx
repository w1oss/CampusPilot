import type { Bundle, CampusMap, MapEdge } from '../types'

const React = window.React
const { useMemo, useRef } = React

interface Props {
  map: CampusMap
  bundle: Bundle | null
  selectedEdgeId: string | null
  onSelectEdge: (edgeId: string) => void
}

const STATUS_COLOR: Record<string, string> = {
  open: '#7d8d76',
  congested: '#e09b3d',
  blocked: '#cf4b3a',
}

const GATES = new Set(['北门', '东北门', '东门', '西门', '中门', '南门'])

const ROAD_WIDTH: Record<string, number> = {
  service: 5,
  living_street: 5,
  pedestrian: 4,
  footway: 2.4,
  path: 2,
  steps: 2.4,
}

function toPoints(polyline: [number, number][]): string {
  return polyline.map(([x, y]) => `${x},${y}`).join(' ')
}

export default function CampusMap({ map, bundle, selectedEdgeId, onSelectEdge }: Props) {
  const width = map.coordinate_system.width
  const height = map.coordinate_system.height
  const background = map.background
  const nodes = map.nodes
  const edges = map.edges
  const routePolyline = bundle?.plan?.polyline || []
  const vehicle = bundle?.state?.position

  const scale = width / 860
  const scaleValue = (value: number) => Math.round(value * scale * 10) / 10

  const selectRef = useRef(onSelectEdge)
  selectRef.current = onSelectEdge

  const roadStatus = useMemo(() => bundle?.road_status || {}, [bundle?.road_status])
  const deliveredIds = useMemo(
    () => new Set((bundle?.stops || []).filter((stop) => stop.status === 'delivered').map((stop) => stop.location_id)),
    [bundle?.stops],
  )
  const arrivedIds = useMemo(
    () => new Set((bundle?.stops || []).filter((stop) => stop.status === 'arrived').map((stop) => stop.location_id)),
    [bundle?.stops],
  )
  const activeStopId = useMemo(() => {
    const next = bundle?.plan?.ordered_stops?.find((stop) => stop.status !== 'delivered')
    return next?.location_id || null
  }, [bundle?.plan])

  const baseLayer = useMemo(() => {
    if (!background) {
      return null
    }
    return (
      <g pointerEvents="none">
        {background.greens.map((area, index) => (
          <polygon key={`green-${index}`} points={toPoints(area.polygon)} fill="#dcead0" stroke="#cfe0bf" strokeWidth={1} />
        ))}
        {background.buildings.map((building, index) => (
          <polygon key={`building-${index}`} points={toPoints(building.polygon)} fill="#e8e4d8" stroke="#cdc7b7" strokeWidth={1} />
        ))}
        {background.roads.map((road, index) => (
          <polyline
            key={`road-${index}`}
            points={toPoints(road.polyline)}
            fill="none"
            stroke={road.kind === 'footway' || road.kind === 'path' || road.kind === 'steps' ? '#efece1' : '#fbfaf4'}
            strokeWidth={ROAD_WIDTH[road.kind] || 3}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ))}
      </g>
    )
  }, [background])

  const hitLayer = useMemo(
    () => (
      <g>
        {edges.map((edge) => (
          <polyline
            key={edge.id}
            points={toPoints(edge.polyline)}
            fill="none"
            stroke="transparent"
            strokeWidth={scaleValue(12)}
            strokeLinecap="round"
            strokeLinejoin="round"
            style={{ cursor: 'pointer' }}
            onClick={() => selectRef.current(edge.id)}
          >
            <title>{`${edge.name}（${edge.id}）· 约 ${edge.distance_m} 米`}</title>
          </polyline>
        ))}
      </g>
    ),
    [edges, scale],
  )

  const statusLayer = useMemo(() => {
    const marked = edges.filter((edge) => {
      const status = roadStatus[edge.id] || edge.base_status
      return status !== 'open' || edge.id === selectedEdgeId
    })
    if (marked.length === 0) {
      return null
    }
    return (
      <g pointerEvents="none">
        {marked.map((edge) => {
          const status = roadStatus[edge.id] || edge.base_status
          const selected = edge.id === selectedEdgeId
          return (
            <polyline
              key={edge.id}
              points={toPoints(edge.polyline)}
              fill="none"
              stroke={selected ? '#1e6fa8' : STATUS_COLOR[status] || STATUS_COLOR.open}
              strokeWidth={selected ? scaleValue(7) : scaleValue(6)}
              strokeDasharray={status === 'blocked' ? `${scaleValue(10)} ${scaleValue(7)}` : undefined}
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          )
        })}
      </g>
    )
  }, [edges, roadStatus, selectedEdgeId, scale])

  return (
    <div className="map-shell">
      <div className="map-caption">
        <div>
          <strong>北邮西土城路校区 · 校园示意图</strong>
          <span className="muted"> 地图版本 {map.map_version} · 团队标注底图</span>
        </div>
        <div className="map-caption-right">
          {bundle?.state?.current_edge_id ? <span className="chip chip-live">模拟执行中</span> : null}
          {bundle?.task.mode === 'real' ? <span className="chip">人工交付模式</span> : <span className="chip chip-sim">模拟模式</span>}
        </div>
      </div>

      <svg
        className="campus-map"
        viewBox={`0 0 ${width} ${height}`}
        style={{ aspectRatio: `${width} / ${height}` }}
        role="img"
        aria-label="北京邮电大学西土城路校区配送路网"
      >
        <defs>
          <filter id="soft-shadow" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="0" dy="2" stdDeviation="3" floodColor="#1d332b" floodOpacity="0.18" />
          </filter>
        </defs>

        <rect width={width} height={height} fill="#eef1e8" />
        {map.background_image ? (
          <image
            href={map.background_image}
            x={0}
            y={0}
            width={width}
            height={height}
            preserveAspectRatio="none"
            pointerEvents="none"
          />
        ) : (
          baseLayer
        )}
        {hitLayer}
        {statusLayer}

        {routePolyline.length > 1 ? (
          <>
            <polyline
              points={toPoints(routePolyline)}
              fill="none"
              stroke="#0f5d4a"
              strokeWidth={scaleValue(9)}
              strokeLinecap="round"
              strokeLinejoin="round"
              opacity={0.25}
            />
            <polyline
              points={toPoints(routePolyline)}
              fill="none"
              stroke="#12735a"
              strokeWidth={scaleValue(4)}
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeDasharray={`${scaleValue(14)} ${scaleValue(10)}`}
              className="route-dash"
            />
          </>
        ) : null}

        {nodes.map((node) => {
          if (node.kind === 'location') {
            const isDelivered = deliveredIds.has(node.id)
            const isArrived = arrivedIds.has(node.id)
            const isActive = activeStopId === node.id && !isDelivered
            const fill = isDelivered ? '#2c8a63' : isArrived || isActive ? '#d9782d' : '#ffffff'
            const labelWidth = node.name.length * scaleValue(13) + scaleValue(8)
            const labelHeight = scaleValue(18)
            return (
              <g key={node.id}>
                {isActive ? (
                  <circle cx={node.x} cy={node.y} r={scaleValue(14)} fill="#d9782d" opacity={0.18} className="pulse-dot" />
                ) : null}
                <circle
                  cx={node.x}
                  cy={node.y}
                  r={scaleValue(8)}
                  fill={fill}
                  stroke="#123c32"
                  strokeWidth={scaleValue(2)}
                  filter="url(#soft-shadow)"
                />
                {isDelivered ? (
                  <path
                    d={`M ${node.x - scaleValue(3.5)} ${node.y} l ${scaleValue(2.5)} ${scaleValue(2.5)} l ${scaleValue(4.5)} ${-scaleValue(5)}`}
                    stroke="#fff"
                    strokeWidth={scaleValue(1.8)}
                    fill="none"
                    strokeLinecap="round"
                  />
                ) : null}
                <g transform={`translate(${node.x}, ${node.y - scaleValue(16)})`}>
                  <rect
                    x={-labelWidth / 2}
                    y={-labelHeight + scaleValue(6)}
                    width={labelWidth}
                    height={labelHeight}
                    rx={scaleValue(5)}
                    fill="#fffdf7"
                    stroke="#dcd8ca"
                  />
                  <text x={0} y={scaleValue(1)} textAnchor="middle" fontSize={scaleValue(12)} fill="#22352c" fontWeight={600}>
                    {node.name}
                  </text>
                </g>
              </g>
            )
          }
          if (node.kind !== 'junction') {
            return null
          }
          return (
            <g key={node.id}>
              <circle cx={node.x} cy={node.y} r={scaleValue(2.5)} fill="#aeb5a5" />
              {GATES.has(node.name) ? (
                <text x={node.x + scaleValue(8)} y={node.y + scaleValue(4)} fontSize={scaleValue(11)} fill="#6f7667" fontWeight={600}>
                  {node.name}
                </text>
              ) : null}
            </g>
          )
        })}

        {vehicle ? (
          <g transform={`translate(${vehicle.x}, ${vehicle.y})`} filter="url(#soft-shadow)">
            <circle r={scaleValue(10)} fill="#fffdf7" stroke="#123c32" strokeWidth={scaleValue(2)} />
            <circle r={scaleValue(5)} fill={bundle?.task.mode === 'sim' ? '#d9782d' : '#1e6fa8'} />
            <text x={0} y={-scaleValue(16)} textAnchor="middle" fontSize={scaleValue(11)} fill="#123c32" fontWeight={700}>
              {bundle?.task.mode === 'sim' ? '模拟车辆' : '执行人'}
            </text>
          </g>
        ) : null}
      </svg>

      <div className="map-footer">
        <div className="legend">
          <span><i className="dot dot-open" />可通行</span>
          <span><i className="dot dot-congested" />拥堵</span>
          <span><i className="dot dot-blocked" />封闭</span>
          <span><i className="dot dot-route" />计划路线</span>
          <span><i className="dot dot-arrived" />当前站点</span>
          <span><i className="dot dot-delivered" />已交付</span>
        </div>
        <p className="map-notice">{map.data_notice}</p>
      </div>
    </div>
  )
}