# CampusPilot 校园低速配送任务 Agent

面向校园实验室、学院办公室和后勤的**多站点配送任务规划与执行助手**。用户用自然语言描述配送需求，系统解析地点、物品、优先级和截止时间，生成配送顺序、路线和执行清单；执行中上报道路异常后，会保留已完成站点并重新规划剩余任务，最后形成可追溯的任务报告。

底图使用团队标注的《北京邮电大学西土城路校区示意图》：6 个配送地点（教三楼、科研楼、教四楼、学一公寓、图书馆、综合服务楼）和 6 个校门的位置直接取自标注坐标，路网按校园道路走向重建。

## 边界说明

- 本应用不是自动驾驶系统，不控制真实车辆，也不提供真实道路安全保证。
- 底图与建筑位置来自团队标注的校园示意图，坐标为图像像素示意；距离按约 0.28 米/像素估算，未做实地测绘。
- 未接入学校后勤系统；模拟执行仅用于演示与测试，不计入真实配送成果。
- 真实模式由配送人员按网页清单执行，只有人工确认才会记录"已取货 / 已交付"。

## 已实现

- 自然语言解析任务；地点走白名单校验，缺取货点、配送站点或物品时在页面逐项追问，补全前不能创建
- 多站点排序：优先站点前置、截止时间约束、拥堵/封闭道路参与路径计算
- SVG 校园路网展示：路线、车辆位置、道路状态、可点击选路
- 执行闭环：待开始 → 执行中 → 到达 → 取货确认 → 交付 → 完成；支持暂停/继续/取消
- 模拟执行器：沿道路折线推进，到站等待人工确认，刷新后可恢复
- 道路异常：文字分析候选判断 → 人工确认 → 更新路况 → 重规划 → 不可达提示
- 操作幂等（`request_id`）、计划版本校验、重复交付不重复记账
- 多 Agent 协作痕迹：任务规划、道路观察、调度复盘三个 Agent 的调用记录（输入、输出、工具、耗时），任务详情与报告中可查看时间线
- 管理员视图：任务列表与筛选、接单、状态更新、处理备注；接单人与备注写入任务时间线
- 历史任务、任务报告、打印/导出 PDF
- 20 条人工标注解析样例与评测脚本（`tests/parse_cases.json`、`tests/eval_parse.py`）

## 未实现（下一阶段）

- 管理员独立视图（查看/接单/备注处理结果）
- 解析后逐字段追问交互
- 照片上传与视觉模型识别
- 20 条人工标注解析样例与真实用户试用记录

## 目录结构

```text
CampusPilot/
├─ backend/
│  ├─ app/                 FastAPI 应用（解析/规划/模拟/状态机/报告）
│  ├─ data/campus_map.json 路网数据（纳入版本控制）
│  ├─ run.py               本地启动入口（Windows 使用 Selector 事件循环）
│  ├─ tests/               smoke_flow.py 与 pytest 接口测试
│  ├─ uploads/             上传照片（内容不提交）
│  └─ var/                 SQLite 运行库（不提交，首次启动自动建表）
├─ frontend/
│  ├─ src/                 React + TypeScript 源码
│  ├─ static/              Babel 构建产物（不提交，需 npm run build）
│  ├─ vendor/              React UMD 运行时
│  └─ index.html
├─ .env.example            运行配置示例
├─ start.bat               Windows 一键启动
└─ start.sh                macOS / Linux / git-bash 一键启动
```

## 环境要求

- Python 3.11+
- Node.js 20+（只在构建前端时需要）

## 快速启动

Windows 直接双击 `start.bat`；它会安装依赖、按需构建前端并打开浏览器。

手动启动：

```bat
python -m pip install -r backend\requirements.txt
cd frontend
npm install
npm run build
cd ..\backend
python run.py
```

访问 `http://127.0.0.1:8000/`。后端同时提供页面、静态资源和 `/api`，不需要单独起前端服务。

修改前端后重新执行 `cd frontend && npm run build`，刷新页面即可。

## 实时语义消息与后台位置跟踪

保留原有三个 Agent 和目录结构，新增可关闭的 `semantic_bridge.py` 接入模块。
模拟上游可在小车行驶途中随时发送结构化语义消息，经道路观察 Agent 与调度复盘 Agent 协调，调用现有规划/执行工具处理。
支持后台持续推进位置、行人等待与解除、当前位置绕行、消息去重和模拟任务隔离。道路观察和调度策略当前为明确标注的结构化适配/确定性策略，不宣称新增了真实视觉模型或自主 LLM 推理。

前端执行面板开启“后端持续跟踪并推进位置”后，关闭浏览器也不会中断后台移动；到站仍需人工确认交付。
下方“上游语义接收与自动模拟”独立于地图选路：开启后按车辆当前路段持续发现新障碍，多个障碍可同时存在；绕行到其他路段仍可能发现新的障碍。经过旧障碍附近时由上游顺路复查，确认恢复后才更新共享路况；施工不会到点自动消失，也不依赖人工点按钮解除。地图点击只用于“管理员人工上报道路异常”。

也可以先启动后端，再从另一个终端运行：

```bat
cd backend
python mock_upstream.py --demo --category pedestrian --delay 0 --clear-after 0
```

消息协议、配置开关、接口和模块分工见 [实时语义接入说明](docs/realtime-semantics.md)。
简化可视与避让：距道路连接处 20 米内可复查所有直接相连方向的施工/维修栏，适用于十字、丁字、Y 字路口和转弯处，包括已封闭方向，不受路段长度限制；纸箱按 10 米范围观察，在原道路上用紫色短弧线避让，不触发全局绕路。恢复仍需明确观测，强制禁行继续优先。
模拟后台使用单个后端 worker，沿用 `python run.py` 启动即可。

## 配置真实模型

默认使用规则解析（结果会明确标注"规则模拟解析"）。复制 `.env.example` 为项目根目录的 `.env`，填写三项后重启后端：

```dotenv
CAMPUSPILOT_LLM_BASE_URL=https://api.deepseek.com/v1
CAMPUSPILOT_LLM_API_KEY=sk-你的密钥
CAMPUSPILOT_LLM_MODEL=deepseek-chat
```

接口要求 OpenAI 兼容的 `/chat/completions`。配置成功后 `/api/health` 返回 `llm_configured: true`，页面右上角显示"真实模型已配置"。模型调用失败会自动回落到规则解析，不会阻塞主流程。

`.env` 已在 `.gitignore` 中，不会被提交。

## 验证

```bat
cd backend
python -m py_compile app\*.py tests\*.py
python tests\smoke_flow.py
python -m pytest tests -q
python tests\eval_parse.py --require 18

cd ..\frontend
npm run build
```

`tests/smoke_flow.py` 走 service 层完整流程；`tests/test_api_flow.py` 通过 ASGI 走 HTTP 全链路，包含静态资源、非法输入和不可达场景。`tests/eval_parse.py` 对 `tests/parse_cases.json` 中 20 条人工标注样例逐条比对，输出通过数与失败原因；`--require 18` 按验收线返回非零状态，`--parser auto` 可对比真实模型解析。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 服务状态、地图版本、模型是否配置 |
| GET | `/api/map` | 路网、节点、道路与当前路况 |
| POST | `/api/tasks/parse` | 文本解析为任务草稿，返回 `missing_fields` |
| POST | `/api/tasks` | 用户确认后创建任务 |
| GET | `/api/tasks` | 历史任务列表，可按 `mode` 过滤 |
| GET | `/api/tasks/{id}` | 任务聚合状态（任务、站点、计划、执行位置、时间线） |
| POST | `/api/tasks/{id}/plan` | 生成配送计划 |
| POST | `/api/tasks/{id}/actions` | 状态动作，需 `request_id` 与 `plan_version` |
| POST | `/api/tasks/{id}/replan` | 基于当前路况重规划剩余站点 |
| POST | `/api/tasks/{id}/sim/tick` | 模拟执行器推进 |
| GET | `/api/tasks/{id}/report` | 任务报告 |
| POST | `/api/tasks/{id}/accept` | 管理员接单，记录接单人与时间 |
| POST | `/api/tasks/{id}/notes` | 添加管理员处理备注 |
| GET/POST | `/api/road-events` | 查询 / 确认道路事件 |
| POST | `/api/road-events/analyze` | 文字异常的候选判断（需人工确认） |
| POST | `/api/seed` | 生成三条演示任务 |

动作类型：`start`、`pause`、`resume`、`arrive`、`confirm_pickup`、`confirm_delivery`、`complete`、`cancel`。

## 设计约束

- **地点白名单**：模型和用户都不能创建新地点；所有地点引用 `campus_map.json` 中的 ID。
- **确定性逻辑**：路径、排序、时间、状态流转、报告指标由代码计算，模型只做理解与候选判断。
- **道路事件不覆盖原始数据**：封路写入 `road_events` 表，不修改 `campus_map.json`，便于恢复与审计。
- **模拟与真实隔离**：`mode=sim` 才能调用模拟推进；模拟记录不进入真实配送成果。
- **状态由业务事件驱动**：动画只做展示，刷新后从后端状态恢复。- **Agent 与确定性代码分工**：Agent 只负责理解、判断和编排；日期、状态、白名单、优先级和报告指标由代码计算。每次 Agent 调用写入 `agent_traces`，记录输入摘要、输出摘要、工具、状态与耗时。
