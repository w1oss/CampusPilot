# CampusPilot 代码生成、重构与排错记录

> 记录周期：2026-11-21 ～ 2026-11-24  
> 对应仓库：`https://github.com/w1oss/CampusPilot`  
> 说明：本文记录代码生成、重构、排错的真实过程；每个问题包含现象、定位、修复与验证。所有验证命令均在本地执行，结果以实际输出为准。

---

## 1. 代码生成与修改清单

### 1.1 后端

| 文件 | 类型 | 目的 |
| --- | --- | --- |
| `backend/app/agents.py` | 新增 | 三个 Agent 定义、调用痕迹记录与汇总 |
| `backend/app/db.py` | 修改 | 新增 `agent_traces`、`task_assignments`、`task_notes`；场景/语义/自动恢复列迁移 |
| `backend/app/service.py` | 修改 | 接单、备注、Agent 痕迹、自动恢复、语义调度适配 |
| `backend/app/main.py` | 修改 | 解析/异常分析痕迹、接单/备注路由、静态资源托管 |
| `backend/app/mapdata.py` | 修改 | `to_public_dict()` 返回 `background_image` |
| `backend/app/parser.py` | 修改 | 缺取货点追问、未知地点提示、LLM 白名单外检测 |
| `backend/app/schemas.py` | 修改 | 接单/备注请求模型 |
| `backend/app/config.py` | 修改 | `.env` 自动加载、语义桥配置 |
| `backend/app/semantic_bridge.py` | 队友合并 | 上游语义接入、安全停车、决策 |
| `backend/app/scene_sim.py` | 队友合并 | 场景障碍与持续观测 |
| `backend/mock_upstream.py` | 队友合并 | 模拟上游消息发送 |

### 1.2 前端

| 文件 | 类型 | 目的 |
| --- | --- | --- |
| `frontend/src/components/AdminView.tsx` | 新增 | 管理员任务列表、接单、状态、备注 |
| `frontend/src/components/TaskComposer.tsx` | 修改 | 逐项补问、地点分类下拉 |
| `frontend/src/components/CampusMap.tsx` | 修改 | 底图渲染、80 地点分类、服务点悬停、语义状态 |
| `frontend/src/components/ExecutionPanel.tsx` | 修改 | Agent 时间线、语义面板、异常上报 |
| `frontend/src/components/ReportView.tsx` | 修改 | Agent 统计与时间线 |
| `frontend/src/App.tsx` | 修改 | 管理员视图导航、语义面板接线 |
| `frontend/src/types.ts` | 修改 | Agent/任务分配/地点分类/语义类型 |
| `frontend/src/api.ts` | 修改 | 接单、备注、语义事件接口 |
| `frontend/src/styles.css` | 修改 | 补问卡片、Agent 时间线、管理员视图样式 |

### 1.3 测试与文档

| 文件 | 类型 | 目的 |
| --- | --- | --- |
| `backend/tests/test_api_flow.py` | 新增 | ASGI 全链路测试 |
| `backend/tests/parse_cases.json` | 新增 | 20 条人工标注解析样例 |
| `backend/tests/eval_parse.py` | 新增 | 解析评测脚本 |
| `backend/tests/fixtures/campus_map_v4.json` | 新增 | 固定旧地图，供依赖旧边 ID 的测试使用 |
| `docs/project-summary.md` | 新增 | 项目工作总结 |
| `docs/competition-overview.md` | 新增 | 比赛作品介绍 |
| `docs/ppt-outline.md` | 新增 | 13 页 PPT 大纲 |
| `docs/ai-interaction-log.md` | 新增 | AI 交互过程记录 |
| `docs/code-generation-refactor-debug-log.md` | 新增 | 本文 |

---

## 2. 重构记录

### 2.1 前端入口统一

**背景**：`index.html` 按静态页面加载 `/vendor/react` + `/static/main.js`，`main.tsx` 曾被改成 ESM 写法，两套链路冲突。

**重构**：

- 统一为 Babel 编译 `src/` → `frontend/static/`；
- 浏览器加载 React UMD；
- FastAPI 托管 `/`、`/static`、`/vendor`；
- `main.tsx` 使用 `window.React` 与 `ReactDOM.createRoot`。

**验证**：`tsc --noEmit`、Babel 构建、ASGI 静态资源 200。

### 2.2 地图数据生成管线

**重构前**：手工维护地图 JSON，点对点连接，容易穿楼。

**重构后**：

- `gen_full_map.py` 读取队友 `structure.json`；
- 41 栋建筑 + 39 个服务点 + 6 个校门全部接入网格；
- 维护旧 6 个地点 ID，保证解析样例与测试兼容；
- 输出 217 节点 / 261 段路网，含 `category` 分类。

**验证**：80 个地点两两可达、旧 ID 保留、图书馆单入口。

### 2.3 Agent 痕迹

**重构前**：三个 Agent 只存在于文档描述。

**重构后**：

- `agents.py` 统一记录；
- 每次调用写 `agent_traces`；
- 前端时间线 + 报告统计；
- 解析、创建、计划、状态、重规划、接单、备注全部留痕。

### 2.4 管理员数据模型

新增 `task_assignments` 与 `task_notes`，避免修改任务主表；接单人与备注进入任务时间线。

### 2.5 测试与生产地图解耦

队友两个测试依赖旧地图边 ID（`e0026`、`e0074`、`g42`）。地图扩展到 80 地点后边 ID 变化，测试失败。

**重构**：新增 `tests/fixtures/campus_map_v4.json`，测试加载固定夹具，不随生产地图漂移。

---

## 3. 排错记录

### 问题 1：前端入口链路冲突

**现象**：页面空白或资源 404；`index.html` 引用 `/static/main.js`，但源码写法不一致。

**定位**：`index.html`、`main.tsx`、`package.json`、Babel 配置不统一。

**修复**：

- `index.html` 加载 `/vendor/react.production.min.js`、`/vendor/react-dom.production.min.js`、`/static/main.js`；
- `main.tsx` 使用 `window.React` / `window.ReactDOM`；
- `tsconfig` 使用 `jsx: react`；
- FastAPI 托管 `/static`、`/vendor`。

**验证**：`/`、`/static/main.js`、`/vendor/react.production.min.js` 返回 200。

---

### 问题 2：esbuild 服务被停止

**现象**：`vite build` 报 `The service was stopped`。

**定位**：Windows 环境下 esbuild 二进制被拦截；项目实际已切换 Babel 构建。

**修复**：改用 `@babel/cli`：

```bat
node node_modules/@babel/cli/bin/babel.js src --out-dir static --extensions ".ts,.tsx" --copy-files --out-file-extension .js
```

**验证**：11 个文件编译成功。

---

### 问题 3：httpx 0.28 与 starlette TestClient 不兼容

**现象**：`Client.__init__() got an unexpected keyword argument 'app'`。

**定位**：本地 httpx 0.28 移除了 TestClient 依赖的 `app=` 参数，starlette 版本较旧。

**修复**：测试改用 `httpx.AsyncClient` + `httpx.ASGITransport`，并在 Windows 设置 `WindowsSelectorEventLoopPolicy`。

**验证**：`test_api_flow.py` 3 项通过。

---

### 问题 4：pytest 输出捕获不稳定

**现象**：pytest 命令被移到后台，stdout 为空，无法直接看到结果。

**定位**：沙箱对长时间命令的输出捕获不稳定，不是测试失败。

**修复/绕行**：

- 使用 `--tb=short`、`--lf`；
- 关键链路用独立脚本（`check_api.py`、`check_admin.py`）验证；
- 全量测试通过后台输出状态确认。

---

### 问题 5：地图 polyline 越界

**现象**：`MapData` 校验报 `polyline out of bounds`。

**定位**：画布 860×900 时，部分东侧道路坐标超过 860。

**修复**：生成时过滤画布外路段；或扩大/裁剪 bbox。

**验证**：地图加载通过。

---

### 问题 6：缝合边未加入图，地点不可达

**现象**：缝合了分量，但 `cs_building → xueyi_building` 仍不可达。

**定位**：只记录了 seam 距离，没有把 seam 加进 `segments/adj`。

**修复**：缝合时同时添加边：

```python
adj[a].add(b)
adj[b].add(a)
segments.append((a, b, "缺失路段补齐"))
```

**验证**：所有地点连通。

---

### 问题 7：图书馆双入口

**现象**：封图书馆入口后仍可达，单入口测试失败。

**定位**：`e07`、`e08` 也直接连接 `library`。

**修复**：改为连接 `j_center`（图书馆北路口），只保留 `e02` 作为图书馆入口。

**验证**：封 `e02` 后图书馆不可达。

---

### 问题 8：接口漏返回 `background_image`

**现象**：页面只有地点和路网，没有校园底图。

**定位**：`campus_map.json` 有 `background_image`，但 `MapData.to_public_dict()` 没返回该字段。

**修复**：

```python
if self.raw.get("background_image"):
    payload["background_image"] = self.raw["background_image"]
```

**验证**：`/api/map` 返回 `/vendor/campus-base.jpg`，图片 200。

---

### 问题 9：白名单外配送点被静默丢弃

**现象**：输入“送到科研楼和体育馆”，体育馆消失且无提示。

**定位**：解析器只匹配白名单，未检测未匹配地点。

**修复**：新增 `_unknown_delivery_locations()`，在规则与 LLM 解析后补充 `unknown_location:*`。

**验证**：20 条样例中未知地点用例通过。

---

### 问题 10：缺取货点自动猜

**现象**：“把实验室的材料送到科研楼”被自动猜成“从实验室取”。

**定位**：`rule_parse` 在没有明确取货点时自动取第一个地点。

**修复**：去掉自动猜，改为 `missing_fields` 加 `pickup_location`。

**验证**：`case04` 通过（允许“正确解析”或“正确追问”）。

---

### 问题 11：合并队友分支冲突

**现象**：合并 `origin/zzz` 与 `origin/main` 时，`db.py`、`service.py` 冲突。

**定位**：

- `db.py`：`zzz` 用通用 `_add_missing`，`origin/main` 用 `_migrate`；
- `service.py`：`zzz` 有 `event_type`/`apply_dispatch_status`，`origin/main` 有 `_observe_and_maybe_restore`。

**修复**：

- 保留 `_add_missing`，补 `observe_count`、`last_observed_at` 两列；
- 保留 `event_type` + `authority`；
- 保留 `apply_dispatch_status`、`_edges_near_node`、`_observe_and_maybe_restore`；
- `sim_tick` 保留新 `advance` 调用，并追加观察/恢复逻辑。

**验证**：合并后全量测试通过。

---

### 问题 12：合并后缩进错误

**现象**：`IndentationError: unexpected indent`（`db.py:262`、`service.py:1201`）。

**定位**：冲突解决时首行缩进被替换掉。

**修复**：补回 8 空格缩进。

**验证**：`py_compile` 通过。

---

### 问题 13：旧地图边 ID 导致测试失败

**现象**：`test_admin_recheck`、`test_visibility_policy` 失败，`e0074`/`e0026`/`g42` 指向错误道路。

**定位**：测试硬编码旧地图 ID，生产地图已扩展到 80 地点。

**修复**：从 `7f8236d` 导出旧地图为 `tests/fixtures/campus_map_v4.json`，测试加载固定夹具。

**验证**：两个测试文件 20 项通过。

---

### 问题 14：上传提交误带缓存与数据库

**现象**：`origin/main` 的 `98695ff` 包含 `__pycache__/*.pyc`、`backend/var/campuspilot.db`。

**定位**：网页上传文件时未过滤。

**修复**：

```bat
git rm --cached -r backend/app/__pycache__ backend/var/campuspilot.db
```

`.gitignore` 已覆盖 `__pycache__/` 与 `backend/var/`。

**验证**：后续提交不再包含这些文件。

---

### 问题 15：GitHub HTTPS 推送失败

**现象**：`Invalid username or token. Password authentication is not supported`。

**定位**：GitHub 不再支持账号密码；本机有 SSH key 但未添加到 GitHub。

**修复**：

- 读取 `~/.ssh/id_rsa.pub`（公钥）；
- 用户在 GitHub Settings → SSH keys 添加；
- 远程切换为 `git@github.com:w1oss/CampusPilot.git`；
- `git push -u origin main`。

**验证**：推送成功。

---

### 问题 16：PowerShell 直接执行 start.bat 报错

**现象**：`start.bat : 无法将“start.bat”项识别为 cmdlet`。

**定位**：PowerShell 不从当前目录查找命令。

**修复/说明**：使用 `.\start.bat`，或双击，或 CMD 中执行。

**验证**：用户本机可启动。

---

### 问题 17：地图扩展后 20 条样例失败

**现象**：“体育馆”从白名单外变成合法地点，`case07` 期望 `unknown_location:体育馆` 失败。

**定位**：样例过时，不是解析器错误。

**修复**：把样例改为真正校外的“五道口”，期望 `unknown_location:五道口`。

**验证**：规则与模型评测恢复 20/20。

---

## 4. 验证命令与结果

```bat
cd backend
python -m py_compile app\*.py tests\*.py      # 通过
python tests\smoke_flow.py                    # SMOKE OK
python -m pytest tests -q                     # 全量通过
python tests\eval_parse.py                    # 20/20
python tests\eval_parse.py --parser auto      # 20/20（18 模型 + 2 兜底）

cd ..\frontend
node node_modules\typescript\bin\tsc --noEmit # 通过
npm run build / Babel 11 文件                 # 成功
```

Git：

```text
f8cf94b  fix: 合并后修复缩进并为旧地图测试添加夹具
0eed733  docs: 添加比赛作品介绍与 PPT 大纲
```

---

## 5. 排错经验总结

1. **先定位层级**：是数据、接口、构建还是浏览器缓存；
2. **用可观测证据**：`/api/map` 返回、静态资源状态码、Agent 痕迹、测试输出；
3. **不重复试错**：同一命令连续失败时，换验证方式（ASGI、独立脚本、夹具）；
4. **地图数据要可追溯**：生成脚本 + 校验脚本 + 预览文件同时保留；
5. **合并冲突保留双方语义**：先读冲突上下文，再决定保留哪边，不盲目选 ours/theirs；
6. **测试与生产数据解耦**：依赖旧 ID 的测试使用固定夹具；
7. **提交前检查敏感文件**：`.env`、数据库、缓存、上传内容。

---

## 6. 证明性材料

| 材料 | 路径 |
| --- | --- |
| AI 交互记录 | `docs/ai-interaction-log.md` |
| 项目总结 | `docs/project-summary.md` |
| 比赛介绍 | `docs/competition-overview.md` |
| PPT 大纲 | `docs/ppt-outline.md` |
| 解析样例 | `backend/tests/parse_cases.json` |
| 链路测试 | `backend/tests/test_api_flow.py` |
| 地图夹具 | `backend/tests/fixtures/campus_map_v4.json` |

> 备注：本文记录真实开发过程；未运行或未完成的检查已在文中明确标注。