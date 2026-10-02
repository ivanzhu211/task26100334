"""异常类型定义。"""


class PartCostError(Exception):
    """本模块所有异常的基类。"""


class ValidationError(PartCostError):
    """业务规则校验失败（如会导致负库存、退回超量等）。整笔操作被拒绝。"""


class NotFoundError(PartCostError):
    """引用的对象不存在（如事件、工单）。"""
