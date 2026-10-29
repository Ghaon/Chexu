import argparse
from pathlib import Path
from typing import Annotated
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from src.domain.reports import ParsedReport, ReportError, ReportSuffix
from src.reports.parser import parse_report


def create_server(root: Path, *, max_bytes: int, max_text_chars: int) -> MCPServer:
    server = MCPServer("chexu-report-parser")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                            idempotent_hint=True, open_world_hint=False))
    def read_report(report_id: UUID, suffix: ReportSuffix,
                    expected_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
                    start_chunk: Annotated[int, Field(ge=1)] = 1,
                    limit: Annotated[int, Field(ge=1, le=5)] = 3) -> ParsedReport:
        """读取指定报告编号的本地资料，返回原文片段及可引用的片段标识。"""
        try:
            return parse_report(root, report_id, suffix, expected_sha256, start_chunk, limit,
                                max_bytes=max_bytes, max_text_chars=max_text_chars)
        except ReportError as exc:
            raise ToolError(str(exc)) from exc

    return server


def main() -> None:
    arguments = argparse.ArgumentParser(description="Chexu local vehicle report MCP server")
    arguments.add_argument("--root", required=True, type=Path)
    arguments.add_argument("--max-bytes", type=int, default=10 * 1024 * 1024)
    arguments.add_argument("--max-text-chars", type=int, default=60000)
    options = arguments.parse_args()
    create_server(options.root.resolve(), max_bytes=options.max_bytes,
                  max_text_chars=options.max_text_chars).run(transport="stdio")


if __name__ == "__main__":
    main()
