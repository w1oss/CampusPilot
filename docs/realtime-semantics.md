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

同一车辆的推进、消息处理、人工动作、重规划串行执行，并共享同一 SQLite 事务。消息与计划变更一起提交或一起回滚。读取任务聚合状态也使用一致快照，避免出现新旧计划混合。

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

接收成功返回 `accepted`、`duplicate`、`decision` 与最新 `bundle`。`accepted=false` 表示旧消息或证据不足，未覆盖活动障碍；它不是路线规划成功标志。`decision.action` 为 observe、wait、replan、continue、unchanged 或 ignored。不可达时通过 `bundle.task.status=blocked`、`bundle.plan.feasible=false` 和不可达站点明确反馈。

输入有效期只用于拒绝迟到消息，不自动删除已观察到的障碍。预计持续时间也不等于解除证据；需要上游复查确认。短暂行人先等待，等待达到 10 秒仍未解除时重新评估绕行，不能直接认定道路已经畅通。

多个障碍按对象维护，解除其中一个不会解除其他障碍。无关道路和已经位于车后方的障碍不会无故触发重规划。相同通行影响的持续观测不反复生成计划。

## 后台运行器接口

```http
POST /api/tasks/{task_id}/sim/runtime
Content-Type: application/json

{"enabled": true, "speed_mps": 15}
```

设置 `enabled=false` 停止后台推进。控制结果与 `GET /api/tasks/{task_id}` 都返回 `runtime`。任务仍需先有计划并由用户开始、确认取货；控制运行器不会代替这些业务操作。

后台托管时直接调用 `/sim/tick` 返回 `409 runtime_owns_motion`，防止前后端重复推进。未托管的任务仍支持原有 `/sim/tick`。

## 路段中途规划

规划器在当前路段几何线上建立临时起点，连接允许驶向的真实端点，复用 Dijkstra 和剩余站点排序。临时节点、路段仅存在于本次计算，不修改地图 JSON 或共享地图缓存。执行时把临时首段转换回真实道路及正确方向、进度，保持当前位置不变。

已知围挡位置时，只连接不穿越障碍的一侧；单行路不允许反向退出。当前路段整段封闭或障碍位置未知时，保持当前位置并报告不可达，不假定可以退回路口。后续明确解除后会重算。人工重规划也采用同一位置处理；车辆在路段中时重新调用初始 `/plan` 会拒绝，提示使用 `/replan`。

`/replan` 支持 `request_id` 去重和可选 `plan_version` 校验。新计划不可行时不能通过“继续执行”绕过校验。

## 独立上游模拟

先按 README 构建前端并启动后端，再运行：

```bat
cd backend
python mock_upstream.py --demo --category pedestrian --delay 3 --clear-after 3
```

该命令创建并开始一个专用演示任务，以 5 m/s 持续推进，在 3 秒后独立发送行人消息，再在 3 秒后发送解除消息。触发依赖经过时间，不依赖标记点。输出任务 ID、消息处理结果和接收时的位置；可以在历史任务中打开该任务查看。

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

新增测试覆盖途中绕行的位置和真实后续运动、行人等待解除、人工暂停权限、重复和乱序消息、多障碍叠加、真实/模拟任务隔离、错误回滚、后台无浏览器移动及单一推进来源。
