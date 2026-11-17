# 分布式网页抓取调度平台

面向分布式网页抓取任务调度、节点协同、请求分配、反爬策略编排、数据校验入库与抓取质量大盘的采集工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集节点 | `worker` | 工作节点 | 节点编号、主机地址、并发槽位数 |
| 请求分配 | `dispatch` | 分配策略 | 策略编号、目标域名、分配算法 |
| 抓取作业 | `crawljob` | 作业定义 | 作业编号、目标站点、起始URL |
| URL过滤 | `urlfilter` | 过滤规则 | 规则编号、包含模式、排除模式 |
| DNS解析 | `dnsresolver` | 解析配置 | 配置编号、域名模式、DNS服务器 |
| 内容提取 | `contentextract` | 提取器 | 提取器编号、内容类型、正文选择器 |
| 存储策略 | `storagestrategy` | 存储方案 | 方案编号、存储类型、连接参数 |
| 渲染任务 | `rendertask` | 渲染作业 | 作业编号、目标URL、浏览器版本 |
| 采集质量 | `qualityreport` | 质量报告 | 报告编号、作业编号、总URL数 |
| 带宽控制 | `bandwidth` | 带宽策略 | 策略编号、节点标识、下载带宽上限 |
| 重试队列 | `retryqueue` | 重试条目 | 条目编号、失败URL、失败原因码 |
| 翻页管理 | `pagemarker` | 翻页规则 | 规则编号、目标站点、翻页类型 |
| 增量标记 | `incrementmark` | 增量水位 | 水位编号、目标源、增量字段名 |
| 配置同步 | `configsync` | 同步任务 | 任务编号、源节点、目标节点 |
| 采集API | `scrapeapi` | API定义 | API编号、API名称、请求路径 |
| 数据抽样 | `datasample` | 抽样方案 | 方案编号、数据源、抽样方式 |
| 采集告警 | `alertpolicy` | 告警策略 | 策略编号、监控指标、触发条件 |

## 重试队列数据流

重试链路跨「重试队列 → 结果记录 → 网页执行侧（渲染任务）→ 抓取作业」四个位置，
状态只在 `backend/app/services/retryqueue.py` 里流转：

```text
草稿 ──加入队列──> 等待重试 ──发起重试──> 重试中 ──重试成功──> 重试成功 ─┐
                                            │                          │
                                            └──标记最终失败──> 最终失败 ─┴─重新加入队列─> 等待重试
```

- 只有「重试中」能收敛为「重试成功」或「最终失败」；加入队列 / 发起重试不会跨级直达终态。
- 退避级别到上限（5）后，下一次发起重试在同一步内收敛为最终失败。
- 每次状态变化追加一条尝试记录 `retryattempt`，可按条目回溯；`重试结果 retryresult`
  每个尝试一条，迟到的旧尝试或终态后的相反回调最多再保留一份「版本冲突」结果。
- 同一重试结果连续提交只生效一次；并发回调共用 `store.lock`，只留一个终态。
- 最终失败清单 `finalfailure` 与成功结果按失败URL互斥；`POST /api/retryqueue/resync`
  或 `POST /api/rendertask/sync` 会把结果回写到网页执行侧失败URL集合与抓取作业清单。

主要接口：

| 接口 | 说明 |
| --- | --- |
| `GET /api/retryqueue` | 按条目编号/失败URL/状态/作业编号分页筛选 |
| `GET /api/retryqueue/stats` | 条目总数、等待重试、重试成功率、数据集规模 |
| `GET /api/retryqueue/{id}` | 条目明细（含尝试记录与重试结果） |
| `GET /api/retryqueue/{id}/attempts` | 单条目尝试记录，回溯每次状态变化 |
| `GET /api/retryqueue/results` | 重试结果数据集（含冲突版本） |
| `GET /api/retryqueue/final-failures` | 最终失败清单 |
| `POST /api/retryqueue/bulk` | 一批失败URL整组提交，逐条返回尝试与最终处置 |
| `POST /api/retryqueue/drain` | 并发执行重试队列，同一条目只推进一次 |
| `POST /api/retryqueue/{id}/actions` | 状态动作，可带 `attempt` / `result_id` 做幂等与迟到判定 |
| `GET /api/rendertask/failures` | 网页执行侧失败URL集合 |
| `GET /api/rendertask/retry-results` | 网页执行侧读取重试结果 |
| `POST /api/rendertask/sync` | 把重试结果同步到网页执行侧清单与作业清单 |

## 测试

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.tsx`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message, entry }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
- 动作接口的请求体是 `{ "values": { "action": "..." } }`；静态路由（如 `/export`、
  `/stats`）必须注册在 `/{entry_id}` 之前。

领域：数据工程
子方向：数据采集、爬虫与RPA
