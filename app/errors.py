from flask import jsonify


MESSAGES = {
    "EMPTY_FILE": "文件为空",
    "BLANK_FILE": "文件只有空白",
    "HEADER_ONLY": "文件只有表头",
    "HEADER_MISMATCH": "表头与本项目字段不一致",
    "UNCLOSED_QUOTE": "引号未闭合，文件已拒收",
    "TOO_MANY_ROWS": "数据行超过上限",
    "FILE_TOO_LARGE": "文件超过大小上限",
    "KEY_MISSING": "主键为空",
    "CONFLICT_DUPLICATE": "同一键存在冲突内容",
    "SAMPLE_UNKNOWN": "样例不存在",
    "STAGE_UNKNOWN": "阶段不存在",
    "RUN_NOT_FOUND": "批次不存在",
    "STALE_REVIEW": "批次已被更新，请刷新后复核",
    "GATE_BLOCKED": "存在阻断项，不能签发",
    "NOT_APPROVED": "批次尚未签发",
    "BAD_REQUEST": "请求内容不完整",
    "ORDER_NOT_FOUND": "订单不存在",
    "ORDER_HELD": "订单仍被坏行或复核暂缓，不能签发",
    "BATCH_HELD": "存在无法归属的坏行，整批暂缓",
    "ORDER_BLOCKED": "订单存在计算阻断项，不能签发",
    "ORDER_ALREADY_ISSUED": "订单已签发，金额与凭证保持不变",
    "STALE_AMOUNT": "待签金额已因补正失效，请按重算金额复核",
    "CORRECTION_FAILED": "补正未通过校验，请修正后重试",
    "CORRECTION_TARGET_NOT_FOUND": "找不到要补正的坏行",
    "AMOUNT_UNAVAILABLE": "订单金额尚不完整，不能签发",
    "IDEMPOTENCY_CONFLICT": "幂等键对应另一张订单",
}


class DomainIssue(Exception):
    def __init__(self, code: str, status: int = 400, detail: str | None = None, extra: dict | None = None):
        self.code = code
        self.status = status
        self.detail = detail
        self.extra = extra or {}
        super().__init__(code)


def error_response(exc: DomainIssue):
    body = dict(error=exc.detail or MESSAGES.get(exc.code, "请求无法处理"), code=exc.code)
    if exc.extra:
        body.update(exc.extra)
    return jsonify(body), exc.status
