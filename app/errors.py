"""领域异常：携带可直接展示的中文原因。"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则被违反（入参非法、状态不允许等），HTTP 400。"""


class NotFoundError(Exception):
    """资源不存在，HTTP 404。"""
