"""外包修复站交接服务。

职责：
- 复核员签字后，为验收报告生成按底片排序的交接单；
- 修复站轮询领取：同一底片严格按签字顺序交接，前一份未签收则跳过该底片、
  改发其他底片；
- 派发后等待回执，超时未收到则原派发过期，可重新派发并沿用原交接编号；
- 回执幂等：过期回执作废，已签收的交接单重试不会重复签收。

修复站停机不影响库内签字：交接单在领取时按需补齐，恢复轮询即可领到
停机期间积累的全部已签字报告。
"""
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.config.settings import engine
from src.constants.workflow import HandoffState as H
from src.constants.workflow import RECEIPT_TTL_SECONDS
from src.models.tables import evidence, handoffs
from src.services.db import found, log
from src.utils.time import utc_time

# 同一底片并发签字时序号可能撞唯一约束，命中后用锁读重新取号的最大次数。
_SEQ_RETRY = 5


def _asset_of(report):
    """从验收报告中定位还原快照对应的底片文件名。"""
    for section in report.get('sections', []):
        if section.get('title') != '底片快照清单':
            continue
        rows = section.get('rows', [])
        for row in rows:
            if row.get('底片文件'):
                return str(row['底片文件'])[:120]
    raise HTTPException(422, '验收报告缺少底片文件信息，无法建立交接单')


def _insert_handoff(conn, evidence_id, asset, now):
    """为一份报告插入交接单，并发签字时靠唯一约束 + 锁读重试取号。

    MySQL/InnoDB 下 FOR UPDATE 是当前读，能看到其它事务已提交的最大序号并加锁；
    SQLite 忽略 FOR UPDATE，但库级写锁使写入天然串行。
    """
    for _ in range(_SEQ_RETRY):
        try:
            with conn.begin_nested():  # 保存点：撞号只回滚这一条插入
                current = conn.execute(
                    select(func.coalesce(func.max(handoffs.c.seq), 0))
                    .where(handoffs.c.asset == asset)
                    .with_for_update()).scalar_one()
                conn.execute(handoffs.insert().values(
                    handoff_no=uuid.uuid4().hex,
                    evidence_id=evidence_id,
                    asset=asset,
                    seq=current + 1,
                    state=H.READY,
                    attempts=0,
                    created_at=now))
            return True
        except IntegrityError:
            # 同底片并发取到相同序号，或该报告已被其它事务建单，重试即可。
            continue
    raise HTTPException(409, '交接单生成冲突，请稍后重试')


def provision(conn):
    """为所有已签字但尚无交接单的验收报告补建交接单。

    报告按签字时间（次按证据编号）遍历；同一底片的连续序号由
    `_insert_handoff` 在并发下安全分配，不同底片各自计数。
    """
    rows = conn.execute(
        select(evidence.c.id, evidence.c.report)
        .where(evidence.c.reviewed_at.is_not(None))
        .order_by(evidence.c.reviewed_at, evidence.c.id)
    ).mappings().all()
    if not rows:
        return 0

    existing = {row['evidence_id'] for row in conn.execute(
        select(handoffs.c.evidence_id)).mappings()}
    now = utc_time()
    created = 0
    for row in rows:
        if row['id'] in existing:
            continue
        asset = _asset_of(row['report'])
        if _insert_handoff(conn, row['id'], asset, now):
            created += 1
    return created


def _sweep_expired(conn, now):
    """超过回执窗口仍在派发中的交接单标记为过期，允许重新派发。"""
    stale = conn.execute(
        select(handoffs.c.id).where(
            handoffs.c.state == H.DISPATCHED,
            handoffs.c.receipt_expires_at.is_not(None),
            handoffs.c.receipt_expires_at < now)
    ).mappings().all()
    for row in stale:
        conn.execute(handoffs.update().where(handoffs.c.id == row['id']).values(
            state=H.EXPIRED, dispatch_no=None, dispatched_at=None,
            receipt_expires_at=None))
        log(conn, 'repair-station', 'handoff_expired', row['id'])


def _pick_heads(active):
    """按底片分组，只保留每个底片未签收交接单中的队首（最小序号）。

    active 已按 (asset, seq) 排序。每个底片的第一行即队首：
    - 队首在派发中（等回执）：该底片整组阻塞，本次跳过；
    - 队首可领（ready/expired）：只有它能被派出，同底片其余报告须排队。
    """
    heads = []
    seen = set()
    for row in active:
        if row['asset'] in seen:
            continue
        seen.add(row['asset'])
        if row['state'] == H.DISPATCHED:
            continue
        heads.append(row)
    return heads


def _payload(conn, handoff_row):
    ev = found(conn, evidence, handoff_row['evidence_id'])
    return {
        'handoff_no': handoff_row['handoff_no'],
        'dispatch_no': handoff_row['dispatch_no'],
        'asset': handoff_row['asset'],
        'seq': handoff_row['seq'],
        'state': handoff_row['state'],
        'attempts': handoff_row['attempts'],
        'dispatched_at': handoff_row['dispatched_at'],
        'receipt_expires_at': handoff_row['receipt_expires_at'],
        'delivered_at': handoff_row['delivered_at'],
        'receipt_no': handoff_row['receipt_no'],
        'evidence_id': ev['id'],
        'digest': ev['digest'],
        'reviewer': ev['reviewer'],
        'reviewed_at': ev['reviewed_at'],
        'report': ev['report'],
    }


def poll(limit):
    """修复站领单：返回本次成功派发的交接单（可能为空列表）。"""
    dispatched_rows = []
    with engine.begin() as conn:
        provision(conn)
        now = utc_time()
        _sweep_expired(conn, now)

        active = conn.execute(
            select(handoffs, evidence.c.reviewed_at.label('signed_at'))
            .select_from(handoffs.join(
                evidence, evidence.c.id == handoffs.c.evidence_id))
            .where(handoffs.c.state != H.DELIVERED)
            .order_by(handoffs.c.asset, handoffs.c.seq)
        ).mappings().all()
        candidates = _pick_heads(active)
        # 跨底片按报告签字先后派发；同时刻再按底片与序号保证确定性。
        candidates.sort(key=lambda r: (r['signed_at'], r['asset'], r['seq']))

        for candidate in candidates[:limit]:
            # 行锁兜底并发领取，被其他事务抢走则跳过。
            locked = conn.execute(
                select(handoffs).where(
                    handoffs.c.id == candidate['id'],
                    handoffs.c.state.in_([H.READY, H.EXPIRED]))
                .with_for_update(skip_locked=True)).mappings().first()
            if locked is None:
                continue
            expires = utc_time(RECEIPT_TTL_SECONDS)
            # 重新派发沿用原交接编号（handoff_no），只递增尝试次数。
            conn.execute(handoffs.update().where(handoffs.c.id == locked['id']).values(
                state=H.DISPATCHED,
                dispatch_no=locked['handoff_no'],
                dispatched_at=now,
                receipt_expires_at=expires,
                attempts=locked['attempts'] + 1))
            log(conn, 'repair-station', 'handoff_dispatch', locked['handoff_no'])
            refreshed = conn.execute(select(handoffs).where(
                handoffs.c.id == locked['id'])).mappings().one()
            dispatched_rows.append(_payload(conn, refreshed))
    return dispatched_rows


def _acknowledge(conn, dispatch_no, receipt_no, now):
    """在已开启的事务内完成签收，返回 (状态码, 载荷或错误信息)。

    用返回值而非异常控制流程，保证“过期作废”的状态更新能随事务正常提交。
    """
    row = conn.execute(
        select(handoffs).where(handoffs.c.handoff_no == dispatch_no)
        .with_for_update()).mappings().first()
    if row is None:
        return 404, '交接编号不存在'

    # 已生效的回执重试：原样返回，不重复签收。
    if row['state'] == H.DELIVERED:
        return 200, (_payload(conn, row), True)

    # 尚未派发：回执一律作废，状态无需改动。
    if row['state'] != H.DISPATCHED:
        return 410, '该交接单不在派发中，回执作废'

    if row['receipt_expires_at'] is None or now > row['receipt_expires_at']:
        # 迟到回执作废：把交接单转回过期，等待重新领取。
        conn.execute(handoffs.update().where(handoffs.c.id == row['id']).values(
            state=H.EXPIRED, dispatch_no=None, dispatched_at=None,
            receipt_expires_at=None))
        log(conn, 'repair-station', 'receipt_expired', row['handoff_no'])
        return 410, '回执窗口已过，该回执作废，请重新领取'

    conn.execute(handoffs.update().where(handoffs.c.id == row['id']).values(
        state=H.DELIVERED, delivered_at=now, receipt_no=receipt_no))
    log(conn, 'repair-station', 'handoff_delivered', row['handoff_no'])
    refreshed = conn.execute(select(handoffs).where(
        handoffs.c.id == row['id'])).mappings().one()
    return 200, (_payload(conn, refreshed), False)


def acknowledge(dispatch_no, receipt_no):
    """修复站回执签收。返回 (交接单, 是否为重放回执)，失败时抛 HTTPException。"""
    with engine.begin() as conn:
        status, result = _acknowledge(conn, dispatch_no, receipt_no, utc_time())
    if status != 200:
        raise HTTPException(status, result)
    return result


def list_handoffs():
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(
            select(handoffs).order_by(handoffs.c.asset, handoffs.c.seq)
            .limit(200)).mappings()]
