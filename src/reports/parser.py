from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID

from src.domain.reports import (ParsedReport, REPORT_CHUNK_SIZE, ReportChunk, ReportFileError,
                                ReportReadError, ReportSuffix)
from src.reports.storage import report_path, validate_format


def parse_report(root: Path, report_id: UUID, suffix: ReportSuffix, expected_sha256: str,
                 start_chunk: int, limit: int, *, max_bytes: int, max_text_chars: int) -> ParsedReport:
    path = report_path(root, report_id, suffix)
    try:
        validate_format(path, suffix, max_bytes)
        with path.open("rb") as handle:
            content = handle.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ReportFileError("报告文件超过大小限制。")
        if sha256(content).hexdigest() != expected_sha256:
            raise ReportReadError("报告内容与上传时的摘要不一致，停止读取。")
        if suffix in {".txt", ".md"}:
            text = content.decode("utf-8-sig").strip()
            parser = "utf8-text"
        else:
            from markitdown import MarkItDown

            text = MarkItDown(enable_plugins=False).convert_stream(BytesIO(content), file_extension=suffix).text_content.strip()
            parser = "markitdown"
        if not text:
            raise ReportReadError("未提取到可读文字；扫描件或图片报告需要 OCR，当前阶段尚未接入。")
        truncated = len(text) > max_text_chars
        text = text[:max_text_chars]
        total = (len(text) + REPORT_CHUNK_SIZE - 1) // REPORT_CHUNK_SIZE
        if start_chunk > total:
            raise ReportReadError("请求的报告片段超过范围。")
        end = min(total, start_chunk - 1 + limit)
        chunks = [ReportChunk(number=number, text=text[(number - 1) * REPORT_CHUNK_SIZE:number * REPORT_CHUNK_SIZE],
                              citation=f"[报告 {report_id}#片段{number}]") for number in range(start_chunk, end + 1)]
        return ParsedReport(report_id=report_id, sha256=expected_sha256, parser=parser, chunks=chunks,
                            total_chunks=total, has_more=end < total, truncated=truncated)
    except (ReportReadError, ReportFileError):
        raise
    except FileNotFoundError as exc:
        raise ReportReadError("报告文件已不可用，请重新上传。") from exc
    except Exception as exc:
        raise ReportReadError("报告解析失败，请检查文件是否损坏、加密或为不受支持的扫描件。") from exc
