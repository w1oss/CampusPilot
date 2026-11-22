# CampusPilot 实时语义接入说明

本扩展保留现有 FastAPI、SQLite、三个 Agent、规划算法、状态机和前端页面结构。新增 `backend/app/semantic_bridge.py` 作为上游接入与生命周期模块；新增 `backend/mock_upstream.py` 作为独立消息发送程序。

## 分工与执行链

1. 任务规划 Agent 继续负责任务理解、追问和初始计划。
2. 接入模块接收已经由视觉与语言模型分析好的结构化结果，检查格式、地图版本、时间、消息编号和修订版本。
3. `agents.observe_road_semantics()` 核对并整理道路判断，明确标注为 `structured_semantic_adapter`，不额外假装调用视觉或语言模型。
4. `agents.decide_dispatch()` 结合共享任务、最新位置和剩余路线，选择观察、等待、重规划或继续。当前是可替换的确定性策略；未来接模型时保持输出协议即可。
5. 调度通过 `service.replan()`、`apply_dispatch_status()` 等工具执行。路径与站点排序继续由 `planner.py`、`graph.py` 计算。调度不能确认取货、交付或完成任务。
6. 输入、道路判断、调度决定和工具结果写入原有时间线与 Agent 调用记录；报告 API 同时返回 `semantic_events`。

接入模块不导入或调用 service 的私有函数。输入和角色处理通过显式函数连接，没有新增独立调度系统、Agent 角色、数据库服务器或第三方编排框架。

## 持续定位和消息监听

后台每约 0.1 秒按单调时钟推进已开启的模拟任务。位置快照与上游消息处理独立运行，不以到达某个路口为监听条件。前端每约 0.2 秒读取状态用于展示，前端不再推进后台托管的车辆。

位置由模拟器计算并持久化，包含坐标、路段、沿行驶方向的进度、上一端点、剩余路段和更新时间。这是按固定周期更新的模拟位置，不是硬实时定位或真实车辆遥测。后台中断后不补跑停机期间的里程；重启后从保存位置继续。到站等待人工交付，不自动记账。

人工暂停会撤销调度的自动恢复权限；也可以在自动等待期间点击“保持人工暂停”。关闭自动推进不会再移动，切换页面或关闭浏览器不等于关闭自动推进。

同一车辆的推进、消息处理、人工动作、重规划串行执行。安全停车作为独立事务先提交，障碍分析与计划变更在随后的事务中提交或回滚，因此决策失败不会让车辆恢复行驶。读取任务聚合状态使用一致快照，避免出现新旧计划混合。

## 运行配置

在项目 `.env` 中设置：

```dotenv
CAMPUSPILOT_SEMANTIC_BRIDGE=1
CAMPUSPILOT_SIM_TICK_INTERVAL=0.1
CAMPUSPILOT_SEMANTIC_MAX_AGE=30
```

`CAMPUSPILOT_SEMANTIC_BRIDGE=0` 后重启后端：关闭消息入口与后台运行器，前端恢复原有模拟推进方式，任务专属语义障碍不再参与规划。保存的消息记录仍可复盘。原有人工道路异常流程保留。

使用 `python backend/run.py`（或在 backend 中 `python run.py`）启动，保持单个 ASGI worker。当前采用进程内锁及单运行器；不要使用多个 worker 或同时启动多个进程操作同一个数据库来推进车辆。

## 消息协议

`POST /api/semantic-events` 只接收指定 `mode=sim` 任务的消息。这里以任务 ID 隔离模拟环境；不支持一条消息自动广播给多车。原有人工道路事件仍然是全局道路信息。

```json
{
  "event_id": "evt_demo_0001",
  "task_id": "task_替换成实际任务编号",
  "obstacle_id": "barrier_01",
  "revision": 1,
  "map_version": "从 GET /api/map 取得实际版本",
  "source": "mock_vision_llm",
  "lifecycle": "observed",
  "edge_id": "e0001",
  "edge_fraction": 0.7,
  "category": "construction",
  "passability": "blocked",
  "confidence": 0.96,
  "expected_duration_s": 1800,
  "summary": "施工围挡占据道路，当前无法通过"
}
```

| 字段 | 约定 |
| --- | --- |
| event_id | 每条消息的唯一编号；完全相同的消息重试不重复执行；编号相同但内容不同返回 409 |
| task_id | 对应一个已有、未结束的模拟任务；拒绝真实模式任务 |
| obstacle_id、revision | 同一障碍递增修订号；旧修订不覆盖新状态 |
| map_version、edge_id | 必须匹配后端地图版本和真实道路 ID |
| observed_at | 可选、必须带时区的 ISO 时间；省略表示本次接收时观察到；超过 30 秒视为旧消息，超过未来 5 秒拒绝 |
| edge_fraction | 可选、0 到 1，固定沿道路 `from` 到 `to` 的方向；与小车行驶方向无关；省略表示位置未知 |
| category | construction、pedestrian、crowd、other |
| passability | open、congested、blocked、unknown |
| lifecycle | observed 或 cleared；解除时使用相同 obstacle_id、递增 revision、新 event_id |
| confidence | 0 到 1；低于 0.65 的新障碍按待复查处理，不能凭低置信度解除或替换已确认的活动障碍 |
| source | mock_vision_llm 或 vision_llm；当前入口对两者均限制为模拟任务 |

接收成功返回 `accepted`、`duplicate`、`decision` 与最新 `bundle`。`accepted=false` 表示旧消息或证据不足，未覆盖活动障碍；它不是路线规划成功标志。`decision.action` 为 observe、wait、replan、local_avoid、continue、unchanged 或 ignored。不可达时通过 `bundle.task.status=blocked`、`bundle.plan.feasible=false` 和不可达站点明确反馈。

输入有效期只用于拒绝迟到消息，不自动删除已观察到的障碍。预计持续时间也不等于解除证据；需要上游复查确认。短暂行人先等待，等待达到 10 秒仍未解除时重新评估绕行，不能直接认定道路已经畅通。

多个障碍按对象维护，解除其中一个不会解除其他障碍。无关道路和已经位于车后方的障碍不会无故触发重规划。相同通行影响的持续观测不反复生成计划。

## 安全停车与放行

危险消息按两个明确步骤处理，停车不等待决策：

1. **先停车（独立事务提交）**：消息通过最小校验后，若障碍位于剩余路线且比当前路况更严重，立即把任务置为 `paused`，写入 `sim_runtime.emergency_hold=1`、`hold_reason` 和 `task_events.safety_stop`，并记录 `auto_resume=1` 表示决策完成后可自动放行。该事务先提交，后续分析或规划失败不会回滚停车。
2. **后决策并放行（另一事务）**：道路观察、调度决策与 `/replan` 在第二事务执行。只有决策为继续、或重规划得到可行路线且保留自动恢复权限时，才清除 `emergency_hold` 并恢复 `in_progress`；等待、不可达、人工暂停或决策异常时车辆保持停车。

后台推进器每个周期先触发并交付消息，再执行移动；车辆处于 `paused` 时不会推进。重复事件只重新执行第二步决策，不重复写入障碍。`bundle.runtime.emergency_hold` 可在前端或接口中查看当前是否处于急停待决策状态。

## 自动模拟源与人工上报的边界

地图选中道路只作为“管理员人工上报道路异常”的表单输入，点击本身不修改路况，也不生成机器消息。人工确认仅用于人工上报的真实性确认，不是机器消息决策的前置条件。

“上游语义接收与自动模拟”不再提供逐条发送或选择道路的表单。外部消息到达 `/api/semantic-events` 后直接经过道路观察与调度处理。页面只展示接收记录、活动障碍和决策结果。

为了不依赖外部脚本也能演示，界面提供“持续上游模拟源”。模拟任务属于场景（默认 `sim_default`），真实任务属于独立场景，二者不共享障碍。模拟源启用后按车辆当前道路持续产生新的障碍对象，多个障碍可以同时存在；车辆绕行到新路段后仍可能发现新障碍。

施工等长期事件在场景中保存两份状态：

- **现场真实状态**（`obstacle_truth`，只由模拟器/剧本读写）；
- **系统已知路况**（`road_known`，Agent 与规划只读这一份）。

剧本可以让施工结束，但系统不会立即知道。模拟上游必须在可观察范围内读取现场状态，确认移除后才更新共享路况。没有观察机会就保留最后一次已知的封路状态。

当前采用简化演示规则，不计算相机朝向、遮挡与车道宽度：

- **施工 / 维修栏**：车辆距道路连接处不超过 20 米时，可以复查该连接处所有直接相连方向的整条路段，不论道路长短、障碍点位于该路段哪里。十字、丁字、Y 字路口、转弯处及更多分叉都按实际连接关系处理，不要求固定的路口形状或道路数量。封闭道路和单行道路也可以观察，观察方向不等于允许行驶的方向。范围不穿过下一连接处继续扩展；在路段中部仍可观察 20 米内的障碍点。道路连接关系由端点决定，不依赖节点的 kind 标签，也不把几何上靠近的平行道路当成相连道路。
- **纸箱**：原障碍点必须在车旁 10 米内。模拟将置信度至少 0.65、位置明确且非强制禁行的纸箱视为有空间通过的小障碍。调度返回 `local_avoid`，在原道路上用平滑短弧线通过，然后回到道路中心线；不触发安全停车、不新增全局计划版本、不把整条路标为封闭。上游原始 blocked 证据保留，`semantic_obstacles.handling=local_avoidance` 表示处理方式，`road_status` 表示实际规划约束；`bundle.local_avoidance` 给出地图紫色短弧与纸箱位置，车辆实际展示坐标也沿短弧移动。经过纸箱不代表纸箱已消失，其他任务仍会避让。
- **其他障碍**：沿用同路或同一路口相邻道路、障碍点距车不超过 20 米的规则。行人等待策略保留。

模拟源在车前约 9 米产生纸箱观测；短弧前后各最多 8 米、侧向偏移最多 2 米，并在端点前回到中心线。它仅用于示意，不进行真实轨迹碰撞检测。位置未知、低置信度、通行情况未知的纸箱仍交给安全停车和全局决策；同路段的其他封路或强制禁行不会被纸箱策略覆盖。整段且位置未知的管理员上报仍不能因看到路口就自动解除。

障碍按 `obstacle_id` 分别保存，同一道路可以同时有围挡和人群；解除其中一个不会把整条道路改成畅通。

共享路况以场景为单位：A 任务发现的施工，同场景的 B 任务规划时也能看到；任务完成不会删除共享障碍。不同模拟场景、模拟与真实模式继续隔离。场景使用统一虚拟时钟推进（演示为 60 倍速），现场状态按虚拟时间结束，但不直接通知 Agent。

管理员人工上报仍是独立通道：普通上报写入共享路况，带位置的上报需两次明确恢复观测才能更新；标记为“强制禁行”的记录不写共享路况，机器观测不能解除。场景重置入口 `POST /api/scenes/{scene_id}/reset` 默认保留管理员上报和对应现场记录，仅清空机器观测、对应现场状态和模拟源游标；任务本身保留。

通过命令行或外部 API 启用运动时，`auto_upstream` 默认 false，避免与外部消息发送程序重复发布。

## 后台运行器接口

```http
POST /api/tasks/{task_id}/sim/runtime
Content-Type: application/json

{"enabled": true, "speed_mps": 15, "auto_upstream": true, "upstream_category": "pedestrian"}
```

设置 `enabled=false` 停止后台推进。控制结果与 `GET /api/tasks/{task_id}` 都返回 `runtime`、`upstream_simulation` 和 `scene`；`upstream_simulation` 包含 enabled、category、generated_count、last_edge_id 和 next_generate_at，`phase` 仅为兼容字段。任务仍需先有计划并由用户开始、确认取货；控制运行器不会代替这些业务操作。

后台托管时直接调用 `/sim/tick` 返回 `409 runtime_owns_motion`，防止前后端重复推进。未托管的任务仍支持原有 `/sim/tick`。

## 路段中途规划

规划器在当前路段几何线上建立临时起点，连接允许驶向的真实端点，复用 Dijkstra 和剩余站点排序。临时节点、路段仅存在于本次计算，不修改地图 JSON 或共享地图缓存。执行时把临时首段转换回真实道路及正确方向、进度，保持当前位置不变。

已知围挡位置时，只连接不穿越障碍的一侧；单行路不允许反向退出。当前路段整段封闭或障碍位置未知时，保持当前位置并报告不可达，不假定可以退回路口。后续明确解除后会重算。人工重规划也采用同一位置处理；车辆在路段中时重新调用初始 `/plan` 会拒绝，提示使用 `/replan`。

`/replan` 支持 `request_id` 去重和可选 `plan_version` 校验。新计划不可行时不能通过“继续执行”绕过校验。

## 独立上游模拟

先按 README 构建前端并启动后端，再运行：

```bat
cd backend
python mock_upstream.py --demo --category pedestrian --delay 0 --clear-after 0
```

该命令创建并开始一个专用演示任务，以 5 m/s 持续推进，启动后立即发送场景消息并先停车。脚本是外部观测者，消息直接写入共享已知路况，不读取场景真值；`--clear-after 0` 表示不自动解除，后续可由经过附近的车辆复查更新。触发依赖经过时间，不依赖标记点。输出任务 ID、消息处理结果和接收时的位置；可以在历史任务中打开该任务查看。

演示优先从教三楼送往图书馆。若当前路况使其不可达，自动选择其他可通行地点；不会清空道路记录或擅自开放道路。所有地点均不可达时，在创建任务前给出明确提示。独立冒烟脚本使用 `backend/var/test_smoke.db`，不会重置应用运行库。

给已有任务发消息：

```bat
python mock_upstream.py --task-id task_xxx --category construction --delay 2 --clear-after 8
python mock_upstream.py --task-id task_xxx --edge-id e0001 --fraction 0.7 --category crowd
```

不指定道路时，发送前读取最新位置并选择车前方位置；程序本身不调用重规划或确认交付。

## 验证

```bat
cd frontend
npm ci
npm run build
cd ../backend
python -m pytest tests -q
python tests/smoke_flow.py
python tests/eval_parse.py --require 18
```

新增测试覆盖途中绕行的位置和真实后续运动、行人等待解除、人工暂停权限、重复和乱序消息、多障碍叠加、真实/模拟任务隔离、停车先于决策、决策失败保持停车、后台无浏览器移动及单一推进来源。
