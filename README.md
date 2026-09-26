# 深海观测网 · 海底电缆永久故障归因服务

多轮继电器切换试验下，根据各传感器通电/断电读数，从**所有永久故障电缆组合**中
找出能同时解释全部轮次读数的故障集，避免仅凭单轮读数局部判断而遗漏环网替代通路。

## 业务规则

- 录入 **5~10 个节点**、**唯一电源节点**、**6~14 条电缆**（每条带修复风险）、
  **2~7 轮试验**（每轮给出闭合电缆与全部节点的通电/断电读数）。
- 电缆在某轮真正导通，当且仅当：**本轮继电器闭合** 且 **非永久故障**。
- 节点通电 ⇔ 在本轮导通子图中与电源节点连通（无向图 DFS/BFS）。
- 服务端**枚举全部 2^E 个永久故障组合**（E≤14，至多 16384 个），
  **逐轮**计算电源可达集并与读数比对，全部吻合才采用。
- 择优依次按：
  1. 故障电缆**数量最少**；
  2. **修复风险总和最低**；
  3. 按电缆**录入顺序展开的故障编号序列字典序最小**（如 `(2,5) < (3,4)`）。
- 若没有任何组合能同时解释全部轮次，明确返回
  **「这些试验读数不能由同一组永久故障同时解释」**；
  修改草稿后修订号 +1，**旧结论立即作废、不保留**，须重新发起归因。

## 技术栈

- 后端：FastAPI + Pydantic（独立 `api` 服务）
- 前端：原生 HTML/CSS/JS 单页（`web/index.html`），由独立 Nginx `web` 服务
  托管并把 `/api/` 反代到后端；无构建步骤
- 测试：pytest（引擎单测 + API 集成 + 规模上限）
- 冒烟：`scripts/smoke.py` 用标准库对运行中的服务跑完整业务链路

## 快速开始（Docker Compose）

```bash
# 可选：配置宿主机端口（默认 web=8080、api=8000）
cp .env.example .env   # 按需修改 WEB_HOST_PORT / API_HOST_PORT

# 启动 Web（Nginx）+ API（FastAPI）
docker compose up --build -d
# 打开 http://localhost:8080
```

两个服务各自带容器健康检查：

- `api`：`curl /api/health`
- `web`：`/health`（Nginx 反代 API 健康端点，同时验证静态服务与反代链路）

### 一次性验收服务 verify

`verify` 服务完成「构建 → 代码测试 → 经 Web 反代完成 API 业务冒烟」后自行退出，
以退出码报告验收结果（0 通过，非 0 失败）：

```bash
docker compose up --build --abort-on-container-exit --exit-code-from verify verify
echo $?   # 0 即验收通过
```

它会等待 `web` 与 `api` 健康检查均通过后再执行。

## 本地开发（无 Docker）

```bash
pip install -r requirements.txt
python -m pytest -q
uvicorn app.main:app --host 0.0.0.0 --port 8000
BASE_URL=http://127.0.0.1:8000 python scripts/smoke.py
```

## API 一览

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /health` | Web 健康检查 |
| `GET /api/health` | API 健康检查 |
| `POST /api/drafts` | 创建草稿（严格校验所有业务约束） |
| `GET /api/drafts/{id}` | 查看草稿（含修订号与当前结论） |
| `PUT /api/drafts/{id}` | 修改草稿：修订号 +1 并清除旧结论 |
| `POST /api/drafts/{id}/diagnose` | 对当前草稿发起归因并保存结论 |
| `GET /api/drafts/{id}/diagnosis` | 查询最近一次归因（无结论时 409） |
| `POST /api/diagnose` | 无状态归因（直接提交、直接计算） |

归因响应包含：

- `feasible` / `message`：是否存在共同解释及中文说明；
- `fault_cables`：故障电缆（录入编号 + 名称）、`fault_count`、`total_repair_risk`；
- `candidate_count`：与全部轮次读数吻合的可行组合总数（全量枚举统计）；
- `rounds[]`：每轮闭合电缆、**电源可达传感器**、期望通电/断电集合、
  是否吻合、以及两类冲突明细（应通电却失电 / 应断电却带电）。

## 目录结构

```
app/               FastAPI 应用
  engine.py        故障组合枚举、逐轮可达性、三级择优（核心引擎）
  schemas.py       Pydantic 模型与跨字段一致性校验
  storage.py       内存草稿存储（修订号 + 改稿作废结论）
  routers.py       业务 API
  main.py          应用入口（/health、/api/health；本地开发时顺带托管页面）
web/               单页录入界面
  index.html
  nginx.conf       Web 容器站点配置（静态托管 + /api 反代 + /health）
tests/             pytest 测试（17 项）
scripts/           smoke.py 业务冒烟；verify.sh 验收入口
Dockerfile         API 镜像（FastAPI/uvicorn，同时供 verify 使用）
Dockerfile.web     Web 镜像（Nginx）
docker-compose.yml api + web（各带健康检查、宿主机端口可配）+ verify（一次性验收）
```
