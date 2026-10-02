"""配件成本核算模块的异常类型。"""


class PartsCostError(Exception):
    """模块内所有业务异常的基类。"""


class UnknownPartError(PartsCostError):
    """零件不存在。"""


class UnknownEventError(PartsCostError):
    """库存事件不存在或已作废。"""


class UnknownWorkOrderError(PartsCostError):
    """工单不存在。"""


class NegativeStockError(PartsCostError):
    """操作会导致某个业务时点的结存数量为负，整笔拒绝。"""


class ReturnExceedsIssueError(PartsCostError):
    """同一次领用的累计退回数量超过领用数量。"""


class ReturnBeforeIssueError(PartsCostError):
    """退回的业务时间早于原领用的业务时间。"""


class InvalidQuantityError(PartsCostError):
    """数量不是正整数。"""


class InvalidAmountError(PartsCostError):
    """金额不合法（如单价为负、收入非正）。"""
