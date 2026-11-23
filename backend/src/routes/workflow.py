from fastapi import APIRouter, Depends
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download
from src.models.tables import audit, cases, evidence, runs
from src.schemas.workflow import (CaseForm, LoginForm, PollForm, ReceiptForm,
                                  ReviewForm)
from src.services import domain, handoff, workflow
from src.services.security import current_user, login, operator, reviewer

router = APIRouter()


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return workflow.recent(runs)


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return workflow.enqueue(case_id, actor)


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.recent(evidence)


@router.get('/evidence/{identity}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.post('/evidence/{identity}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/handoffs')
def list_handoffs(_=Depends(current_user)):
    return handoff.list_handoffs()


@router.post('/repair/poll')
def repair_poll(form: PollForm, _=Depends(current_user)):
    """外包修复站定时领取已签字验收报告。"""
    items = handoff.poll(form.limit)
    return {'count': len(items), 'items': items}


@router.post('/repair/receipt')
def repair_receipt(form: ReceiptForm, _=Depends(current_user)):
    """修复站回传签收回执；过期回执作废，重复回执不重复签收。"""
    item, replayed = handoff.acknowledge(form.dispatch_no, form.receipt_no)
    return {'replayed': replayed, 'item': item}


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)
