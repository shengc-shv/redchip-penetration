"""阶段二（预留，未启用）：美股 20-F 抓取。

港股模块验收通过后再启用。届时的实现要点（已验证的调用方式）：

.. code-block:: python

    from edgar import Company, set_identity

    set_identity(os.environ["SEC_IDENTITY"])  # "project-name email@example.com"
    company = Company("BABA")
    filings = company.get_filings(form="20-F")
    obj = filings[0].obj()
    major_shareholders = obj.major_shareholders

SEC EDGAR 速率限制约 10 请求/秒，User-Agent 必须携带应用名与联系邮箱。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UsDocument:
    """美股披露文件占位结构。"""

    ticker: str
    form: str = "20-F"
    url: str = ""
    published_at: str = ""


def is_enabled() -> bool:
    """美股模块是否已启用。

    Returns:
        bool: 恒为 False，待港股模块验收后放开。
    """
    return False


def fetch_us_document(ticker: str, data_dir: str) -> UsDocument:
    """抓取美股 20-F 年报。

    Args:
        ticker: 美股代码。
        data_dir: 缓存目录。

    Returns:
        UsDocument: 文档信息。

    Raises:
        NotImplementedError: 阶段二尚未实现。
    """
    raise NotImplementedError("美股模块为阶段二内容，待港股模块验收后实现（见模块 docstring）")
