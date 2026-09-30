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

## 行动材料交接服务

面向园林、志愿者、科研等多部门协作的保护行动，提供“行动—参与部门—材料清单—交接批次—逐项确认”的完整交接链，接口前缀为 `/api/handover`：

- `POST /actions`：建立保护行动，登记参与部门（移交方/接收方/协办方，构成责任边界）、负责人、截止日和材料清单（每份材料绑定责任部门，可标记敏感/必备）。
- `POST /actions/{code}/batches`：分批交接（`kind=handover`）或补件（`kind=supplement`）。支持 `idempotency_key`；同一行动下内容指纹相同的重复上传直接返回原批次（响应中 `reused=true`），不再产生新版本。材料内容发生修订时自动生成新版本，并通过 `supersedes_version_id` 串成版本链；旧批次与旧确认始终引用原版本，历史不失效。
- `POST /batches/{id}/confirmations`：接收方逐项确认，结果为 `complete` / `missing`（缺件异议）/ `sensitive_objection`（敏感内容异议），异议必须填写说明；确认只追加不覆盖，每次确认连同版本号一起留痕。
- `POST /batches/{id}/reopen`：负责人在截止日前可重开未完成的交接；重开分界点之前的确认保留可查但不再参与完成判定。逾期后禁止重开，且只允许提交补件批次并强制填写 `overdue_reason`。
- `POST /actions/{code}/close`：仅负责人可结案；所有必备材料的当前版本均被接收方确认完整后才允许结案。旧版本上的异议若已被后续补件版本的完整确认覆盖，计为已解决（`resolved_objection_count`），异议原文仍保留。
- `GET /actions/{code}`：返回责任边界（部门与角色）、材料版本链、各批次及其逐项确认历史、事件流和结案条件，可清楚复核“谁在什么时候交接/确认了哪份证据的哪个版本”。

端到端演示（创建、分批交接、异议、重开、补件、结案并打印责任边界、版本关系与每次确认）：

```bash
python -m app.cli handover-demo
```

