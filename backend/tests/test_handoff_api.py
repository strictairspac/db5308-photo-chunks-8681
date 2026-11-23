"""修复站交接 API 的端到端测试（FastAPI TestClient + 内存 SQLite）。"""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

os.environ.setdefault('AUTH_KEY', 'test-key')
os.environ.setdefault('DB_DRIVER', 'sqlite')
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from src.config import settings  # noqa: E402
from src.main import app  # noqa: E402
from src.models.tables import evidence, handoffs, runs, schema, users  # noqa: E402
from src.services import handoff as svc  # noqa: E402
from src.services import workflow  # noqa: E402
from src.services.security import hash_password  # noqa: E402
from src.utils import time as timelib  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    schema.create_all(engine)
    # 路由与安全模块在导入期就绑定了 settings.engine，需要把所有引用一并替换。
    from src.routes import workflow as routes
    from src.services import security
    monkeypatch.setattr(settings, 'engine', engine)
    monkeypatch.setattr(workflow, 'engine', engine)
    monkeypatch.setattr(svc, 'engine', engine)
    monkeypatch.setattr(routes, 'engine', engine)
    monkeypatch.setattr(security, 'engine', engine)
    with engine.begin() as conn:
        conn.execute(users.insert().values(username='reviewer', role='reviewer',
                                           password_hash=hash_password('pw')))
    # 不进入 with 上下文即不触发 lifespan，避免对测试库建表/播种。
    return TestClient(app), engine


def _token(client):
    resp = client.post('/login', json={'username': 'reviewer', 'password': 'pw'})
    assert resp.status_code == 200
    return resp.json()['token']


def _sign_report(engine, asset):
    with engine.begin() as conn:
        run_id = conn.execute(runs.insert().values(
            case_id=1, status='complete', owner='op',
            created_at=timelib.utc_time())).inserted_primary_key[0]
        ev_id = conn.execute(evidence.insert().values(
            run_id=run_id,
            report={'summary': 'x', 'metrics': {}, 'sections': [
                {'title': '底片快照清单', 'rows': [{'底片文件': asset}]}]},
            digest='dig', reviewer=None, review_note=None, reviewed_at=None)
        ).inserted_primary_key[0]
    return ev_id


def test_poll_and_receipt_roundtrip(client):
    http, engine = client
    headers = {'Authorization': f'Bearer {_token(http)}'}
    ev_id = _sign_report(engine, 'A.dpx')

    # 无交接单时返回空批。
    empty = http.post('/repair/poll', json={'limit': 5}, headers=headers)
    assert empty.status_code == 200 and empty.json()['count'] == 0

    # 签字（review）即建交接单。
    signed = http.post(f'/evidence/{ev_id}/review',
                       json={'note': '同意交接'}, headers=headers)
    assert signed.status_code == 200

    polled = http.post('/repair/poll', json={'limit': 5}, headers=headers)
    assert polled.status_code == 200
    item = polled.json()['items'][0]
    assert item['evidence_id'] == ev_id
    assert item['dispatch_no'] == item['handoff_no']
    no = item['dispatch_no']

    # 正常回执签收。
    ok = http.post('/repair/receipt',
                   json={'dispatch_no': no, 'receipt_no': 'R-1'}, headers=headers)
    assert ok.status_code == 200 and ok.json()['replayed'] is False
    assert ok.json()['item']['state'] == 'delivered'

    # 重放同一回执：幂等，不重复签收。
    replay = http.post('/repair/receipt',
                       json={'dispatch_no': no, 'receipt_no': 'R-1'}, headers=headers)
    assert replay.status_code == 200 and replay.json()['replayed'] is True


def test_expired_receipt_410_then_redeliver(client):
    http, engine = client
    headers = {'Authorization': f'Bearer {_token(http)}'}
    _sign_report(engine, 'A.dpx')
    http.post('/evidence/1/review', json={'note': 'go'}, headers=headers)
    no = http.post('/repair/poll', json={}, headers=headers).json()['items'][0]['handoff_no']

    with engine.begin() as conn:
        conn.execute(handoffs.update().values(
            receipt_expires_at=timelib.utc_time(-1)))
    late = http.post('/repair/receipt',
                     json={'dispatch_no': no, 'receipt_no': 'late'}, headers=headers)
    assert late.status_code == 410

    # 过期后可重新领取，编号沿用。
    again = http.post('/repair/poll', json={}, headers=headers).json()['items'][0]
    assert again['handoff_no'] == no and again['attempts'] == 2


def test_unknown_number_404_and_validation(client):
    http, _ = client
    headers = {'Authorization': f'Bearer {_token(http)}'}
    missing = http.post('/repair/receipt',
                        json={'dispatch_no': 'nope', 'receipt_no': 'r'}, headers=headers)
    assert missing.status_code == 404

    # limit 越界 -> 422 校验错误。
    bad = http.post('/repair/poll', json={'limit': 0}, headers=headers)
    assert bad.status_code == 422


def test_requires_authentication(client):
    http, _ = client
    assert http.post('/repair/poll', json={}).status_code == 401
    assert http.get('/handoffs').status_code == 401
