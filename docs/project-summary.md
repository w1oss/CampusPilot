# CampusPilot 开发工作总结

> 更新时间：2026-11-21  
> 项目目录：`D:\CampusPilot`  
> 当前地图版本：`bupt-xitucheng-grid` / `4.0.0`

---

## 一、项目是什么

CampusPilot 是一个校园多站点配送任务规划与执行助手：

- 用户用自然语言描述“从哪里取什么、送到哪里、什么优先、什么时候前完成”；
- 系统解析成结构化任务，生成配送顺序、路线和执行清单；
- 执行中可上报道路异常，系统保留已完成站点并重新规划剩余任务；
- 任务完成后生成可追溯报告；
- 管理员可以接单、更新状态、写处理备注。

**边界：**

- 不是自动驾驶，不控制真实车辆；
- 底图与建筑位置来自团队标注的校园示意图，距离为估算值，未做实地测绘；
- 模拟执行只用于演示，真实交付必须由人员确认；
- 地图只覆盖北邮西土城路校区的一个简化区域，不是全校导航。

**技术栈：**

- 前端：React + TypeScript，Babel 编译到 `static/`，加载 React UMD；
- 后端：FastAPI + SQLite；
- 地图：SVG 叠加在 2491×3509 的校园底图上；
- 模型：DeepSeek（OpenAI 兼容接口），未配置时回落规则解析；
- 部署：单端口 8000，FastAPI 同时托管页面、静态资源与 `/api`。

---

## 二、已完成的工作

### 1. 工程启动与交付包

| 内容 | 说明 |
| --- | --- |
| `README.md` | 项目定位、边界、目录、启动、模型配置、API 一览、验证命令 |
| `.gitignore` | 忽略数据库、上传内容、`node_modules`、构建产物、`.env`、临时目录 |
| `.env.example` | DeepSeek 与速度参数模板 |
| `start.bat` | Windows 一键启动：装依赖 → 按需构建前端 → 启动 → 打开浏览器 |
| `start.sh` | macOS / Linux / git-bash 启动 |
| `backend/run.py` | 本地启动入口，Windows 使用 Selector 事件循环 |
| `backend/app/config.py` | 自动读取根目录 `.env`，无额外依赖 |
| Git 仓库 | 已 `git init`，分支 `main`；**尚未做首次提交** |

### 2. 前后端链路修复

项目早期存在两套冲突的前端加载方式（静态 `/static/main.js` 与 ESM/Vite 写法）。现已统一为：

- Babel 编译 `src/` → `frontend/static/`；
- 浏览器加载 `/vendor/react.production.min.js` 和 `/static/main.js`；
- FastAPI 同源托管 `/`、`/static`、`/vendor`，不需要单独起前端服务。

### 3. 地图与路网

**底图：** 使用队友 `map_design` 工具产出的《北京邮电大学西土城路校区示意图》原图（2491×3509），保存为 `frontend/vendor/campus-base.jpg`，由 `/vendor` 提供。

**地点（6 个，坐标取自队友标注）：**

| 地点 ID | 显示名称 | 别名 |
| --- | --- | --- |
| `cs_building` | 教三楼 | 计算机学院、计院、计算机楼 |
| `research_building` | 科研楼 | 科研大楼 |
| `lab_building` | 教四楼 | 实验室、实验楼、教四 |
| `xueyi_building` | 学一公寓 | 学一楼、学1楼、学一 |
| `library` | 图书馆 | 图书楼 |
| `logistics_center` | 综合服务楼 | 后勤服务中心、后勤、物美 |

**路口（6 个校门）：** 北门、东北门、东门、西门、中门、南门。

**路网：** 按示意图的 6 条纵向路 × 10 条横向路建模，共 **77 个节点、121 段路**；所有路线只走横平竖直的道路网格，6 个地点各用一条短入口线接入最近路口；距离按约 0.28 米/像素估算。

**校验结果：**

- 6 个地点两两可达；
- 教三楼 → 图书馆 616 米、9 段路；
- 图书馆保持单入口，封入口正确判定不可达；
- 4 条主路封路后均有替代路线。

**已修复的问题：**

- 早期地图用点对点直线连接，会穿建筑；已改为道路网格；
- 地图数据文件有 `background_image`，但接口序列化漏了这个字段，导致页面只显示几个点；已修复 `MapData.to_public_dict()`，现在接口返回 `/vendor/campus-base.jpg`。

### 4. 任务解析（规则 + DeepSeek）

**规则解析已修复：**

- 缺取货点不再自动猜，改为提示补充；
- 未知取货点不再把配送点顶替成取货点；
- 白名单外的配送地点（如“体育馆”）会明确提示；
- 支持中文时间（“下午4点半” → 16:30）、别名、优先级、顺序优先、多物品。

**真实模型：**

- 用户已在根目录 `.env` 配置 DeepSeek；
- 真实调用返回 `llm:deepseek-chat`；
- 模型解析失败或无有效取货点时自动回落规则解析，并明确标注。

**20 条人工标注样例集：**

- `backend/tests/parse_cases.json`：20 条输入 + 标准答案；
- `backend/tests/eval_parse.py`：逐条比对，输出通过数与失败原因；
- 覆盖：标准解析、缺取货点、缺物品、未知地点、中文时间、别名、多物品、优先级、无截止时间等；
- 判定口径：**“正确解析或正确追问”都算通过**。

**测试稳定性：** `tests/test_api_flow.py` 显式禁用外部模型，避免 pytest 依赖网络；模型能力由 `eval_parse.py --parser auto` 单独测。

### 5. 逐项补问与任务创建

解析后如果缺关键信息，页面显示带编号的问题卡片：

| 缺什么 | 页面怎么问 | 用户怎么补 |
| --- | --- | --- |
| 取货点 | “从哪里取货？” | 卡片内下拉选择 |
| 配送站点 | “要送到哪些地点？” | 添加配送站点 |
| 物品 | “第 N 站还没有物品” | 统一物品输入并应用到所有站点 |
| 白名单外地名 | 指出哪个地点无效 | 提示改站点，不阻断创建 |

关键项未补全时，“确认任务信息”按钮禁用，并显示“还有 N 项待补充”。

### 6. 计划、执行与异常

**计划：**

- 多站点排序：优先站点前置、截止时间约束；
- 最短路径：基于道路网格；
- 输出：顺序、每站物品、ETA、路线、预计耗时、不可达站点。

**执行：**

- 状态机：待开始 → 执行中 → 暂停/继续 → 完成/取消；
- 动作：开始、到达、确认取货、确认交付、完成、取消；
- 幂等：`request_id`，重复交付不重复记账；
- 模拟执行：沿折线推进，到站等待人工确认，刷新后可恢复；
- 真实模式：由人员按页面清单执行，人工确认才记录交付。

**道路异常与重规划：**

- 文字上报 → 候选判断 → 人工确认 → 更新路况 → 重规划；
- 保留已完成站点；
- 不可达时明确提示，不伪造路线；
- 路况事件存 `road_events` 表，不修改原始地图文件。

### 7. 多 Agent 协作

**三个角色（单进程内的协作层，不是三个独立服务）：**

| Agent | 职责 | 已记录的动作 |
| --- | --- | --- |
| 任务规划 Agent | 理解需求、补问、生成任务与计划 | `parse_task`、`create_task`、`plan_stops` |
| 道路观察 Agent | 图文异常候选判断 | `classify_hazard`（目前仅文字规则） |
| 调度复盘 Agent | 状态更新、重规划、备注、报告 | `update_status`、`replan_task`、`accept_task`、`note_update` |

**实现：**

- `backend/app/agents.py`：Agent 定义、痕迹记录、汇总；
- `agent_traces` 表：Agent、动作、输入摘要、输出摘要、工具、状态、耗时、时间；
- 页面展示：
  - 执行控制面板底部“Agent 协作记录”（最近 6 条）；
  - 任务报告页完整时间线 + 调用次数统计；
  - 痕迹中可直接看到“解析=llm:deepseek-chat”，作为真实模型调用证据。

**边界：** Agent 只负责理解、判断和编排；日期、状态、白名单、优先级和报告指标由确定性代码计算。

### 8. 管理员视图

**后端：**

- `task_assignments` 表：接单人与接单时间；
- `task_notes` 表：处理备注；
- `POST /api/tasks/{id}/accept`：接单；
- `POST /api/tasks/{id}/notes`：添加备注；
- 任务列表与详情返回接单信息、备注；
- 接单与备注写入 Agent 痕迹。

**前端：**

- 顶部导航“管理员视图”；
- 筛选：全部 / 待接单 / 执行中 / 已完成；
- 任务列表：状态、取货点、站点进度、截止、接单人；
- 详情：任务信息、接单信息、状态操作（开始/暂停/继续/取消/完成）、备注列表与输入；
- “在工作台打开”跳到完整执行页。

**当前边界：** 逐站“确认交付”仍在工作台；管理员页负责接单、状态、备注和完成确认。

### 9. 历史任务与报告

- 历史任务列表：状态、取货点、站点进度、截止、接单人、更新时间；
- 任务报告：完成率、已交付/总站点、计划版本、重规划次数、Agent 调用次数、站点明细、计划历史、道路事件、Agent 协作时间线；
- 支持打印/导出 PDF（浏览器打印）。

### 10. 可视化预览

- `docs/campus-map-preview.svg`：地图预览，可直接查看底图与路网；
- `docs/ui-preview.html`：前后端界面静态预览，可切换工作台 / 历史任务 / 管理员视图 / 任务报告；
- 预览数据为示例，真实交互需本机启动服务。

---

## 三、验证结果（最近一次）

| 检查 | 结果 |
| --- | --- |
| 规则解析 20 条 | 20/20 |
| DeepSeek 20 条 | 20/20（18 条模型 + 2 条规则兜底） |
| pytest | 3 passed |
| service 冒烟（解析→计划→模拟→封路→重规划→报告） | SMOKE OK |
| HTTP 全链路 | CHAIN OK |
| 管理员 service 流程（接单→备注→计划→执行→完成） | ADMIN OK |
| 管理员 HTTP 接口 | ADMIN HTTP OK |
| 前端 TypeScript 检查 | 通过 |
| 前端 Babel 构建 | 11 个文件成功 |
| 地图底图资源 | `/vendor/campus-base.jpg` 返回 200，2.8MB |
| 地图接口字段 | `background_image` 已正常返回 |

> 说明：沙箱环境禁止监听本地回环端口，真实 8000 端口页面只能在本机验证。本机验证记录需另行补充。

---

## 四、当前未完成与风险

| 项目 | 状态 | 影响 |
| --- | --- | --- |
| 照片上传 + 视觉异常分析 | **未完成**（道路观察 Agent 目前只有文字规则） | 方案要求“至少三类异常返回风险和下一步动作”，图片部分缺失 |
| Git 首次提交 | **未提交** | 提交记录与可复现证据缺失 |
| docs 证据目录 | 只有总结、地图预览、界面预览 | 执行说明建议的 requirements / architecture / api-contract / codearts 记录 / prompt 库 / 部署 / 测试报告 / 用户反馈未补齐 |
| 真实用户试用 | 未开始 | 验收要求“一位非队员可独立完成一次任务”需现场或记录证明 |
| 管理员页逐站交付 | 当前边界 | 管理员完成配送需先到工作台逐站确认交付 |
| 路网像素级对齐 | 待用户肉眼核对 | 网格坐标由锚点与建筑间隙推断，个别线可能偏离底图道路 |
| 地图地点数量 | 6 个可交互地点 | 底图其他楼不可点击；如需扩展需补入口节点与白名单 |

---

## 五、下一步建议

### P0：先做可提交版本

1. **Git 首次提交**  
   当前包含地图、20 条样例、DeepSeek、Agent 痕迹、管理员视图，是完整节点。提交前确认 `.env`、数据库、上传内容未进入版本库。

2. **补 docs 证据**  
   至少补齐：`requirements.md`、`architecture.md`、`api-contract.md`、`deployment.md`、`test-report.md`，以及码道交互记录与 Prompt 库。  
   原则：真实记录，未运行的检查写“未运行”，不伪造通过。

### P0：补齐最后一个功能缺口

3. **照片上传 + 视觉异常分析**  
   - 后端：上传接口、图片存储、视觉模型调用、候选结果、人工确认；
   - 前端：异常上报面板支持上传图片、预览、候选状态；
   - 归属：道路观察 Agent，记录 `analyze_image`、`classify_hazard` 痕迹；
   - 验收：至少三类异常（施工/拥堵/封路/行人密集）返回风险和下一步动作。

### P1：验收与答辩

4. **真实用户试用**  
   找至少 1 位非队员（最好 3 位）独立完成一次任务，记录操作路径、卡点和修改。

5. **答辩材料**  
   演示脚本、截图/录屏、验证清单、Agent 协作记录展示、规则与模型 20 条对比数据。

### P2：可选优化

6. **管理员页逐站交付**  
   把“确认取货 / 逐站交付”搬进管理员页，减少页面跳转。

7. **扩展可交互地点**  
   把队友数据中的 41 栋楼逐步接入白名单与路网入口。

8. **地图网格微调**  
   按用户肉眼核对结果平移个别纵向/横向道路。

---

## 六、关键文件索引

| 文件 | 作用 |
| --- | --- |
| `backend/app/main.py` | FastAPI 路由、静态资源托管 |
| `backend/app/service.py` | 任务、计划、执行、重规划、管理员、Agent 痕迹 |
| `backend/app/parser.py` | 规则解析 + DeepSeek 解析 + 失败兜底 |
| `backend/app/planner.py` | 站点排序与计划生成 |
| `backend/app/graph.py` | 路网最短路径 |
| `backend/app/simulator.py` | 模拟车辆推进 |
| `backend/app/report.py` | 任务报告 |
| `backend/app/agents.py` | 三个 Agent 定义与痕迹记录 |
| `backend/app/mapdata.py` | 地图加载、校验、公共字段序列化 |
| `backend/data/campus_map.json` | 地图、路网、底图字段 |
| `backend/tests/parse_cases.json` | 20 条解析样例 |
| `backend/tests/eval_parse.py` | 解析评测脚本 |
| `backend/tests/test_api_flow.py` | HTTP 链路测试 |
| `backend/tests/smoke_flow.py` | service 冒烟流程 |
| `frontend/src/components/CampusMap.tsx` | 底图、路网、站点、车辆渲染 |
| `frontend/src/components/TaskComposer.tsx` | 任务创建与逐项补问 |
| `frontend/src/components/ExecutionPanel.tsx` | 执行控制与 Agent 时间线 |
| `frontend/src/components/AdminView.tsx` | 管理员视图 |
| `frontend/src/components/ReportView.tsx` | 任务报告 |
| `docs/ui-preview.html` | 界面静态预览 |
| `docs/campus-map-preview.svg` | 地图预览 |

---

## 七、本机运行与验证

### 运行

```powershell
cd D:\CampusPilot
.\start.bat
```

浏览器访问 `http://127.0.0.1:8000/`。停止服务按 `Ctrl+C`。

> PowerShell 必须用 `.\start.bat`；直接输入 `start.bat` 会报“找不到命令”。

### 验证

```bat
cd backend
python -m py_compile app\*.py tests\*.py
python tests\smoke_flow.py
python -m pytest tests -q
python tests\eval_parse.py --require 18
python tests\eval_parse.py --parser auto
```

前端修改后：

```bat
cd frontend
npm run build
```

### 模型配置

根目录 `.env`：

```dotenv
CAMPUSPILOT_LLM_BASE_URL=https://api.deepseek.com/v1
CAMPUSPILOT_LLM_API_KEY=你的key
CAMPUSPILOT_LLM_MODEL=deepseek-chat
```

`.env` 已在 `.gitignore` 中，不会提交。

---

## 八、一句话总结

第一阶段的业务闭环已经打通：**解析 → 补问 → 计划 → 执行 → 异常重规划 → 报告 → 管理员接单/备注**，并接入真实 DeepSeek、留下可核查的 Agent 协作痕迹；**唯一剩余的功能缺口是照片视觉异常分析**，交付侧还缺 Git 首次提交和 docs 证据。