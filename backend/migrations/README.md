# 初始结构

`src/models/tables.py` 定义初始关系结构。API 启动时显式创建演示库的表；此版本只包含初始 schema。需要在已有部署升级表结构时，应单独引入版本化迁移。

## 交接表 `repair_handoffs`

外包修复站交接单随签字流程引入。全新演示库由 `schema.create_all` 直接建表；已有的生产库需要补一张表：

```sql
CREATE TABLE repair_handoffs (
  id INTEGER PRIMARY KEY AUTO_INCREMENT,
  handoff_no VARCHAR(40) NOT NULL UNIQUE,
  evidence_id INTEGER NOT NULL UNIQUE,
  asset VARCHAR(120) NOT NULL,
  seq INTEGER NOT NULL,
  state VARCHAR(20) NOT NULL,
  dispatch_no VARCHAR(40),
  dispatched_at VARCHAR(32),
  receipt_expires_at VARCHAR(32),
  delivered_at VARCHAR(32),
  receipt_no VARCHAR(64),
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at VARCHAR(32) NOT NULL,
  CONSTRAINT uq_repair_handoffs_asset_seq UNIQUE (asset, seq),
  CONSTRAINT fk_repair_handoffs_evidence FOREIGN KEY (evidence_id)
    REFERENCES business_evidence (id),
  INDEX ix_repair_handoffs_state (state),
  INDEX ix_repair_handoffs_pick (state, asset, seq)
);
```

`state` 取值为 `ready`、`dispatched`、`delivered`、`expired`，枚举定义在 `src/constants/workflow.py`。
