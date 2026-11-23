from enum import StrEnum


class Role(StrEnum):
    OPERATOR = 'operator'
    REVIEWER = 'reviewer'
    VIEWER = 'viewer'


class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'


class HandoffState(StrEnum):
    """交接单生命周期。

    ready      已生成、等待修复站领取
    dispatched 已被修复站领走，等待回执
    delivered  收到有效回执，签收完成
    expired    回执窗口已过期，等待重新派发
    """

    READY = 'ready'
    DISPATCHED = 'dispatched'
    DELIVERED = 'delivered'
    EXPIRED = 'expired'


# 回执有效窗口（秒）：超过该时长仍无回执，原派发过期，可重新派发。
RECEIPT_TTL_SECONDS = 900
