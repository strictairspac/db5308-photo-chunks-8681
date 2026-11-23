"""外包修复站交接服务的行为测试（SQLite 内存库，与生产 MySQL 解耦）。

覆盖业务要求：
1. 签字并发时，所有已签字报告都能被领到；
2. 同一底片按签字顺序交接，前一份未签收则跳过、先发别的底片；
3. 等不到回执可重新派发，沿用原交接编号（dispatch_no == handoff_no）；
4. 过期回执作废；已生效回执重试不重复签收（幂等）；
5. 修复站停机期间照常签字，恢复后能领到停机期间的报告。
"""
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

os.environ.setdefault('AUTH_KEY', 'test-key')
# 用独立的内存 SQLite 接管 settings 在导入期创建的默认引擎（见 settings 的测试分支）。
os.environ.setdefault('DB_DRIVER', 'sqlite')
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from src.config import settings  # noqa: E402
from src.constants.workflow import HandoffState as H  # noqa: E402
from src.models.tables import audit, evidence, handoffs, runs, schema, users  # noqa: E402
from src.services import handoff as svc  # noqa: E402
from src.services import workflow  # noqa: E402
from src.services.security import hash_password  # noqa: E402
from src.utils import time as timelib  # noqa: E402


@pytest.fixture()
def mem(monkeypatch):
    engine = create_engine(
        'sqlite://',
        connect_args={'check_same_thread': False},
        poolclass=StaticPool)
    schema.create_all(engine)
    # settings 导入期已建默认引擎，这里统一替换为内存库。
    monkeypatch.setattr(settings, 'engine', engine)
    monkeypatch.setattr(workflow, 'engine', engine)
    monkeypatch.setattr(svc, 'engine', engine)
    with engine.begin() as conn:
        conn.execute(users.insert().values(
            username='reviewer', role='reviewer',
            password_hash=hash_password('pw')))
    return engine


def _report(asset):
    return {
        'summary': f'{asset} ok',
        'metrics': {},
        'sections': [
            {'title': '底片快照清单',
             'rows': [{'快照': 'S1', '底片文件': asset}]},
        ],
    }


def _sign(conn, asset, *, reviewer='reviewer', note='ok'):
    """模拟一次完整的 run -> evidence -> 复核签字。"""
    run_id = conn.execute(runs.insert().values(
        case_id=1, status='complete', owner='op',
        created_at=timelib.utc_time())).inserted_primary_key[0]
    evidence_id = conn.execute(evidence.insert().values(
        run_id=run_id, report=_report(asset), digest='d' + asset,
        reviewer=None, review_note=None, reviewed_at=None)
    ).inserted_primary_key[0]
    conn.execute(evidence.update().where(evidence.c.id == evidence_id).values(
        reviewer=reviewer, review_note=note,
        reviewed_at=timelib.utc_time()))
    return evidence_id


def _ids(items):
    return [i['evidence_id'] for i in items]


def test_provision_and_poll_delivers_all_signed_reports(mem):
    """要求 1/5：多份已签字报告最终都能领到（同底片需逐份签收解锁）。"""
    with mem.begin() as conn:
        e1 = _sign(conn, 'A.dpx')
        e2 = _sign(conn, 'B.dpx')
        e3 = _sign(conn, 'A.dpx')
    # 尚未 poll 时没有交接单；poll 内部按需补齐（模拟修复站恢复后领取）。
    first = svc.poll(10)
    # 首轮：A 的队首 e1 与 B 的 e1 可领；A 的第二份 e3 被队首阻塞。
    assert set(_ids(first)) == {e1, e2}

    # 签收 A 队首后，e3 解锁并被领到。
    a_head = next(i for i in first if i['evidence_id'] == e1)
    svc.acknowledge(a_head['handoff_no'], 'rcpt')
    second = svc.poll(10)
    assert _ids(second) == [e3]
    all_items = first + second
    assert {i['evidence_id'] for i in all_items} == {e1, e2, e3}
    assert all(i['state'] == H.DISPATCHED for i in all_items)
    assert all(i['dispatch_no'] == i['handoff_no'] for i in all_items)


def test_same_asset_follows_signature_order_and_blocks_behind_head(mem):
    """要求 2：同一底片按序号交接；队首在途时跳过该底片，先发别的底片。"""
    with mem.begin() as conn:
        a1 = _sign(conn, 'A.dpx')
        b1 = _sign(conn, 'B.dpx')
        a2 = _sign(conn, 'A.dpx')

    first = svc.poll(10)
    # A 的队首与 B 被派出，但 A 的第二份因队首未签收而不能派出。
    assert set(_ids(first)) == {a1, b1}
    got = {i['evidence_id']: i for i in first}
    assert got[a1]['seq'] == 1
    assert got[b1]['seq'] == 1

    # 再次轮询：A 队首仍在途，A2 被阻塞，没有可发的新单。
    assert svc.poll(10) == []

    # A 队首签收后，A2 才允许交接。
    svc.acknowledge(got[a1]['handoff_no'], 'rcpt-a1')
    second = svc.poll(10)
    assert _ids(second) == [a2]
    assert second[0]['seq'] == 2


def test_redispatch_keeps_original_handoff_number(mem):
    """要求 3：等不到回执可重新派发，沿用原交接编号。"""
    with mem.begin() as conn:
        _sign(conn, 'A.dpx')
    item = svc.poll(1)[0]
    original_no = item['handoff_no']
    assert item['attempts'] == 1

    # 回执窗口过期后重新派发：编号不变，尝试次数累加。
    with mem.begin() as conn:
        conn.execute(handoffs.update().values(
            state=H.DISPATCHED, receipt_expires_at=timelib.utc_time(-1)))
    again = svc.poll(1)[0]
    assert again['handoff_no'] == original_no
    assert again['dispatch_no'] == original_no
    assert again['attempts'] == 2
    assert again['state'] == H.DISPATCHED


def test_expired_receipt_is_rejected_and_can_redeliver(mem):
    """要求 4：过期回执作废，之后仍可重新领取并正常签收。"""
    with mem.begin() as conn:
        _sign(conn, 'A.dpx')
    item = svc.poll(1)[0]
    no = item['handoff_no']

    # 让窗口过期，再补送一个迟到回执 -> 410 作废。
    with mem.begin() as conn:
        conn.execute(handoffs.update().values(
            receipt_expires_at=timelib.utc_time(-1)))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        svc.acknowledge(no, 'late-receipt')
    assert exc.value.status_code == 410

    with mem.begin() as conn:
        row = conn.execute(select(handoffs).where(
            handoffs.c.handoff_no == no)).mappings().one()
        assert row['state'] == H.EXPIRED
        assert row['receipt_no'] is None

    # 重新派发后用新回执签收成功。
    redelivered = svc.poll(1)[0]
    assert redelivered['handoff_no'] == no
    item2, replayed = svc.acknowledge(no, 'fresh-receipt')
    assert replayed is False
    assert item2['state'] == H.DELIVERED
    assert item2['receipt_no'] == 'fresh-receipt'


def test_valid_receipt_retry_is_idempotent(mem):
    """要求 4：已生效回执的重试不重复签收，标记为 replay。"""
    with mem.begin() as conn:
        _sign(conn, 'A.dpx')
    item = svc.poll(1)[0]
    no = item['handoff_no']

    first, replayed = svc.acknowledge(no, 'rcpt-1')
    assert first['state'] == H.DELIVERED and replayed is False

    # 修复站未收到响应而重发同一回执：返回同单，不产生二次签收。
    again, replayed2 = svc.acknowledge(no, 'rcpt-1')
    assert replayed2 is True
    assert again['state'] == H.DELIVERED
    with mem.begin() as conn:
        delivered = conn.execute(select(handoffs).where(
            handoffs.c.state == H.DELIVERED)).mappings().all()
        assert len(delivered) == 1
        assert delivered[0]['receipt_no'] == 'rcpt-1'


def test_outage_then_recovery_drains_backlog(mem):
    """要求 5：停机期间库内照常签字，恢复后一次领走全部积压。"""
    with mem.begin() as conn:
        ids = [_sign(conn, f'asset-{i}.dpx') for i in range(5)]
    # 停机期间没有任何 poll；恢复后逐批领取直到取空。
    collected = []
    for _ in range(10):
        batch = svc.poll(2)
        if not batch:
            break
        collected.extend(_ids(batch))
    assert sorted(collected) == sorted(ids)


def test_unknown_dispatch_number_404(mem):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        svc.acknowledge('missing-no', 'x')
    assert exc.value.status_code == 404


def test_provision_allocates_unique_contiguous_seq(mem):
    """同一底片多份报告拿到互不重复且连续的序号，不同底片各自从 1 计数。"""
    with mem.begin() as conn:
        c_ids = [_sign(conn, 'C.dpx') for _ in range(3)]
        b_ids = [_sign(conn, 'D.dpx') for _ in range(2)]
    # 分多次事务补建（模拟报告陆续签字、修复站多次轮询）。
    for eid in c_ids + b_ids:
        with mem.begin() as conn:
            svc.provision(conn)

    with mem.begin() as conn:
        c_seq = conn.execute(select(handoffs.c.seq).where(
            handoffs.c.asset == 'C.dpx').order_by(handoffs.c.seq)).scalars().all()
        d_seq = conn.execute(select(handoffs.c.seq).where(
            handoffs.c.asset == 'D.dpx').order_by(handoffs.c.seq)).scalars().all()
    assert c_seq == [1, 2, 3]
    assert d_seq == [1, 2]


def test_insert_retries_on_seq_collision(mem, monkeypatch):
    """同底片已占用 seq 时，_insert_handoff 取 max(seq)+1 分配下一个序号。"""
    with mem.begin() as conn:
        eid = _sign(conn, 'E.dpx')
        # 预置一条同底片 seq=1 的交接单（来自其它报告）。
        with conn.begin_nested():
            conn.execute(handoffs.insert().values(
                handoff_no='pre-seeded', evidence_id=99999, asset='E.dpx',
                seq=1, state=H.READY, attempts=0, created_at=timelib.utc_time()))

    import src.services.handoff as mod
    with mem.begin() as conn:
        ok = mod._insert_handoff(conn, eid, 'E.dpx', timelib.utc_time())
    assert ok is True
    with mem.begin() as conn:
        row = conn.execute(select(handoffs).where(
            handoffs.c.evidence_id == eid)).mappings().one()
    assert row['seq'] == 2 and row['asset'] == 'E.dpx'


def test_insert_recovers_from_integrity_error(mem, monkeypatch):
    """模拟并发取号撞唯一约束：首次插入抛 IntegrityError，重试后成功。"""
    from sqlalchemy import engine as sa_engine
    from sqlalchemy.exc import IntegrityError
    import src.services.handoff as mod
    with mem.begin() as conn:
        eid = _sign(conn, 'F.dpx')

    real_execute = sa_engine.Connection.execute
    state = {'failed': False}

    def flaky_execute(self, statement, *args, **kwargs):
        if (not state['failed']) and 'INSERT INTO repair_handoffs' in str(statement.compile()):
            state['failed'] = True
            raise IntegrityError('simulated unique (asset, seq) collision',
                                 params=None, orig=Exception('unique'))
        return real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(sa_engine.Connection, 'execute', flaky_execute)
    with mem.begin() as conn:
        ok = mod._insert_handoff(conn, eid, 'F.dpx', timelib.utc_time())
    assert ok is True and state['failed'] is True
    with mem.begin() as conn:
        row = conn.execute(select(handoffs).where(
            handoffs.c.evidence_id == eid)).mappings().one()
    assert row['seq'] == 1


def test_insert_gives_up_after_repeated_collisions(mem, monkeypatch):
    """持续撞号超过重试上限时返回冲突错误，而不是无限循环。"""
    from fastapi import HTTPException
    from sqlalchemy import engine as sa_engine
    from sqlalchemy.exc import IntegrityError
    import src.services.handoff as mod
    with mem.begin() as conn:
        eid = _sign(conn, 'G.dpx')

    real_execute = sa_engine.Connection.execute

    def always_fail(self, statement, *args, **kwargs):
        if 'INSERT INTO repair_handoffs' in str(statement.compile()):
            raise IntegrityError('persistent collision', params=None,
                                 orig=Exception('unique'))
        return real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(sa_engine.Connection, 'execute', always_fail)
    with mem.begin() as conn:
        with pytest.raises(HTTPException) as exc:
            mod._insert_handoff(conn, eid, 'G.dpx', timelib.utc_time())
    assert exc.value.status_code == 409


def test_review_creates_handoff_and_audit(mem):
    """走真实 review() 签字路径：签字即入交接池并写审计。"""
    with mem.begin() as conn:
        run_id = conn.execute(runs.insert().values(
            case_id=1, status='complete', owner='op',
            created_at=timelib.utc_time())).inserted_primary_key[0]
        ev_id = conn.execute(evidence.insert().values(
            run_id=run_id, report=_report('Z.dpx'), digest='dZ')
        ).inserted_primary_key[0]
    workflow.review(ev_id, 'signed', 'reviewer')

    items = svc.poll(1)
    assert len(items) == 1 and items[0]['evidence_id'] == ev_id
    with mem.begin() as conn:
        actions = {r['action'] for r in conn.execute(
            select(audit.c.action)).mappings()}
    assert 'handoff_dispatch' in actions and 'review' in actions
