import asyncio
from collections.abc import Callable
import logging
import os
from pathlib import Path
import sys
from uuid import UUID

import anyio
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.agent.config import PROJECT_ROOT
from src.domain.execution import ExecutionCancelled
from src.domain.reports import ParsedReport, ReportReadError, ReportSuffix
from src.reports.storage import prepare_storage


logger = logging.getLogger(__name__)


async def _read_report(root: Path, report_id: UUID, suffix: ReportSuffix, digest: str,
                       start_chunk: int, limit: int, max_bytes: int, max_text_chars: int,
                       timeout_seconds: float, cancel_check: Callable[[], bool] | None) -> ParsedReport:
    root = prepare_storage(root)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src.mcp.report_server", "--root", str(root), "--max-bytes", str(max_bytes),
              "--max-text-chars", str(max_text_chars)], cwd=PROJECT_ROOT,
        env={"PYTHONIOENCODING": "utf-8", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
             "TMP": str(root / ".tmp"), "TEMP": str(root / ".tmp")},
    )
    cancelled = False
    result = None

    with anyio.fail_after(timeout_seconds):
        async with anyio.create_task_group() as group:
            async def watch_cancellation():
                nonlocal cancelled
                while True:
                    await anyio.sleep(0.25)
                    if await anyio.to_thread.run_sync(cancel_check):
                        cancelled = True
                        group.cancel_scope.cancel()
                        return

            if cancel_check is not None:
                group.start_soon(watch_cancellation)
            # 标准输出只走 MCP；服务端诊断不混进模型工具结果。
            with open(os.devnull, "w") as errors:
                transport = stdio_client(parameters, errlog=errors)
                async with Client(transport, read_timeout_seconds=timeout_seconds, cache=None) as client:
                    catalog = await client.list_tools()
                    tools = {tool.name: tool for tool in catalog.tools}
                    required = {"report_id", "suffix", "expected_sha256"}
                    if set(tools) != {"read_report"} or not required <= set(tools["read_report"].input_schema.get("properties", {})):
                        raise ReportReadError("本地报告 MCP 的工具定义与预期不一致。")
                    response = await client.call_tool("read_report", {
                        "report_id": str(report_id), "suffix": suffix, "expected_sha256": digest,
                        "start_chunk": start_chunk, "limit": limit,
                    })
                    if response.is_error:
                        # 不把进程输出或原始服务端异常作为业务错误公开。
                        text = " ".join(getattr(block, "text", "") for block in response.content)
                        if "需要 OCR" in text:
                            raise ReportReadError("报告未提取到可读文字，可能是扫描件；当前尚未接入 OCR。")
                        if "摘要不一致" in text:
                            raise ReportReadError("报告内容校验失败，停止读取。")
                        if "超过范围" in text:
                            raise ReportReadError("请求的报告片段超过范围。")
                        raise ReportReadError("报告无法解析，请检查文件内容、编码或加密状态。")
                    result = ParsedReport.model_validate(response.structured_content)
            group.cancel_scope.cancel()
    if cancelled:
        raise ExecutionCancelled("报告读取已取消。")
    if result is None or result.report_id != report_id or result.sha256 != digest:
        raise ReportReadError("报告 MCP 返回的资料标识不一致。")
    return result


def read_report_via_mcp(*, root: Path, report_id: UUID, suffix: ReportSuffix, digest: str,
                        start_chunk: int, limit: int, max_bytes: int, max_text_chars: int,
                        timeout_seconds: float, cancel_check: Callable[[], bool] | None = None) -> ParsedReport:
    if timeout_seconds < 0.1:
        raise ReportReadError("剩余解析时间不足，报告读取已超时。")
    if cancel_check is not None and cancel_check():
        raise ExecutionCancelled("报告读取已取消。")
    try:
        return asyncio.run(_read_report(root, report_id, suffix, digest, start_chunk, limit,
                                        max_bytes, max_text_chars, timeout_seconds, cancel_check))
    except ExecutionCancelled:
        raise
    except TimeoutError as exc:
        raise ReportReadError("本地报告解析超时，请缩小文件或稍后重试。") from exc
    except Exception as exc:
        def find_report_error(error):
            if isinstance(error, ReportReadError):
                return error
            if isinstance(error, BaseExceptionGroup):
                for child in error.exceptions:
                    found = find_report_error(child)
                    if found is not None:
                        return found
            return None

        known = find_report_error(exc)
        if known is not None:
            raise known from exc
        logger.warning("Local report MCP call failed (%s).", type(exc).__name__)
        raise ReportReadError("本地报告服务读取失败，请检查文件或服务状态。") from exc
