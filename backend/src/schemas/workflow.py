from typing import Any

from pydantic import BaseModel, Field


class LoginForm(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class CaseForm(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]


class ReviewForm(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class PollForm(BaseModel):
    # 单次领取上限，修复站按自身处理能力申请；缺省给一份。
    limit: int = Field(default=1, ge=1, le=50)


class ReceiptForm(BaseModel):
    dispatch_no: str = Field(min_length=1, max_length=40)
    receipt_no: str = Field(min_length=1, max_length=64)
