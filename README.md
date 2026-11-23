# 光坞影像·底片块库验收

以 SHA-256 内容寻址去重，从选中快照复原并列出损坏或缺失块。面向演示数据和本地复现，场景、操作剧本和业务边界见 [BUSINESS.md](BUSINESS.md)。

## 业务模块

- 底片快照
- 内容块目录
- 还原查验
- 留存清单
- 修复站交接

## 本地运行

```sh
cp -n .env.example .env
docker compose up -d --build --wait
```

- 工作台 `http://127.0.0.1:18297`
- FastAPI 文档 `http://127.0.0.1:19297/docs`
- MySQL 8.0 端口 `127.0.0.1:17797`，账号 `app`，演示库 `workflow`
- `docker compose exec -T backend python -m scripts.seed_demo` 手动重跑演示数据
- `docker compose logs -f backend worker` 查看运行情况
- `docker compose down` 停容器并保留演示卷

### 演示账号

统一密码 `Demo-Walkthrough-2026`。

| 账号 | 角色 | 能执行的操作 |
|---|---|---|
| `operator` | 经办员 | 读取档案、创建实验输入、提交执行、导出结果 |
| `reviewer` | 复核员 | 读取、导出和签署结果 |
| `viewer` | 观察员 | 读取、导出已有结果 |

`SEED_DEMO=true` 启动时生成账号与一个业务样例。工作台新建档案会预填样例。可编辑表单中的数值、文本和结构化 JSON 后提交。业务说明中的原始文件是上游示例，演示数据通过表单录入。角色枚举位于后端 `src/constants/workflow.py` 与前端 `src/constants/workflow.js`。执行状态枚举也位于这两个文件，使用于队列、列表和执行详情。证据校验码是报告 JSON 按 UTF-8、键排序且省略多余空格序列化后的 SHA-256。

## 外包修复站交接

复核员在「留存清单」签署验收报告后，系统自动生成修复站交接单，修复站通过接口定时领取，全程在数据库内完成，不依赖 Redis 常驻。

- `POST /repair/poll`：领取已签字报告，请求体 `{"limit": 5}`。同一底片严格按签字顺序交接；队首交接单尚未签收（仍在等回执）时跳过该底片，改发其他底片。返回 `count` 与 `items`，空批为 `{"count": 0, "items": []}`。
- `POST /repair/receipt`：回传签收回执，请求体 `{"dispatch_no": "...", "receipt_no": "..."}`。`dispatch_no` 即交接编号，重新派发时保持不变。
- `GET /handoffs`：查看交接单状态（待领取 `ready`、派发中 `dispatched`、已签收 `delivered`、已过期 `expired`）。

交接规则：派发后进入回执窗口（`RECEIPT_TTL_SECONDS`，默认 900 秒）。窗口内有效回执签收成功；修复站重发同一回执时返回 `replayed: true` 且不会重复签收。超过窗口未收到回执，原派发转为过期，下一次轮询自动重新派发并沿用原交接编号，`attempts` 递增；迟到的旧回执返回 `410` 作废。修复站停机期间库内照常签字，交接单在领取时按需补齐，恢复轮询即可领走全部积压报告。交接单表为 `repair_handoffs`，按 `(asset, seq)` 维护同一底片的签字顺序。

## 工程结构

| 服务 | 实现 |
|---|---|
| frontend | Nginx 与原生 HTML、CSS、JavaScript |
| backend | Python 3.11、FastAPI、SQLAlchemy |
| worker | Python 持久队列执行与 Redis 唤醒信号 |
| db | MySQL 8.0，持久卷保存档案、执行、证据及审计 |
| redis | Redis 7，追加日志卷保存队列唤醒信号 |

编辑 `backend/src/services/domain.py` 可修改试算方法与业务表单说明。API 路由、数据定义和页面位于 `backend/src/routes/`、`backend/src/models/` 与 `frontend/src/pages/`。开发时更新源文件后重新构建对应服务。

`.env.example` 提供 `FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT`、`DB_PASSWORD`、`AUTH_KEY` 、`SEED_DEMO` 与 `NETWORK_SUBNET`。修改 `NETWORK_SUBNET` 可避开本机现有容器网段。所有发布的端口绑定本机回环地址。接入独立部署前需配置自己的凭据并按业务范围做独立验收。

[MIT License](LICENSE)
