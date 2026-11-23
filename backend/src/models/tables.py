from sqlalchemy import (JSON, Column, ForeignKey, Index, Integer, MetaData,
                        String, Table, Text, UniqueConstraint)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
# 外包修复站交接单：一份已签字验收报告对应一张交接单，按底片维护签字顺序。
handoffs = Table('repair_handoffs', schema,
    Column('id', Integer, primary_key=True),
    Column('handoff_no', String(40), nullable=False, unique=True),
    Column('evidence_id', ForeignKey('business_evidence.id'), nullable=False, unique=True),
    Column('asset', String(120), nullable=False),
    Column('seq', Integer, nullable=False),
    Column('state', String(20), nullable=False, index=True),
    Column('dispatch_no', String(40)),
    Column('dispatched_at', String(32)),
    Column('receipt_expires_at', String(32)),
    Column('delivered_at', String(32)),
    Column('receipt_no', String(64)),
    Column('attempts', Integer, nullable=False, server_default='0'),
    Column('created_at', String(32), nullable=False),
    UniqueConstraint('asset', 'seq', name='uq_repair_handoffs_asset_seq'))
Index('ix_repair_handoffs_pick', 'state', 'asset', 'seq')
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))
