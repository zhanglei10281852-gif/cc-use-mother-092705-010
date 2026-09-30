# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

## 运行环境

- Python 3.11 或更高版本
- SQLite 3（使用 Python 标准库）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据文件位于 `data/compute-operations.db`，可以复制 `.env.example` 后调整本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康接口为 `GET /api/system/health`。所有状态变化都写入 SQLite，并由应用内事务保证关联记录的一致性。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入和现有生态计算接口。

## 编译检查

```bash
python -m compileall -q app tests
```

## 本地验收

```bash
python -m app.cli check-db
python -m app.cli smoke
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由。项目不依赖外部数据库、消息队列或网络服务。

## 保护行动材料交接

针对园林、志愿者与科研团队各自保存巡护记录、月底汇总难以追溯证据交接的问题，服务提供 `/api/handover` 接口，围绕一次保护行动组织跨部门交接：

- **按行动建档**：登记负责人、截止日、移交方/接收方部门与材料清单（`POST /api/handover/actions`，可后续追加部门与材料）。只有参与部门能在对应方向上交接或确认，责任边界在每一步校验。
- **分批交接**：移交方按批次提交材料定位信息（`content_ref`、SHA-256、敏感标记）。同一行动内同一移交/接收方、内容摘要完全相同的重复上传直接返回原批次（HTTP 200，带幂等时间线），不生成新批次、不重置确认。
- **逐项确认与异议**：接收方必须逐份材料确认完整性；可提出缺件（`missing`）、敏感内容（`sensitive`）或其他异议。批次在全部材料确认且无未决异议时自动进入已确认状态。
- **修订即新版本**：补件/修订为材料生成全局递增的新版本并挂接 `supersedes_version_id`，旧版本不删除；新版本必须重新确认，旧版本的确认在 `handover_confirmations` 与版本链查询中永久可引用。
- **截止日规则**：截止日前负责人可重开已完成批次（须填原因）；逾期后禁止发起新批次与重开，只允许对既有批次补件且必须登记原因，批次记录提交时是否已逾期。
- **结案与追溯**：批次与行动分别结案；`GET /api/handover/actions/{id}`、`GET /api/handover/batches/{id}` 与 `GET /api/handover/actions/{id}/materials/{code}/versions` 清楚展示责任部门、版本关系、每次确认（含被新版本取代的历史确认）、异议处理与完整时间线。

完整流程（建档 → 分批交接 → 重复上传幂等 → 逐项确认 → 敏感/缺件异议 → 补件修订 → 负责人重开 → 逾期补件规则 → 批次与行动结案）可用一条命令在本地 API 走完并打印结构化结果：

```bash
python -m app.cli handover-demo
```

