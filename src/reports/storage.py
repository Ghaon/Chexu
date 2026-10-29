from hashlib import sha256
from pathlib import Path
import tempfile
from typing import BinaryIO
from uuid import UUID
from zipfile import BadZipFile, ZipFile

from src.domain.reports import ReportFileError, ReportSuffix, ReportTooLarge


def prepare_storage(root: Path) -> Path:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / ".tmp").mkdir(exist_ok=True)
    return root


def report_path(root: Path, report_id: UUID, suffix: ReportSuffix) -> Path:
    if suffix not in {".pdf", ".docx", ".txt", ".md"}:
        raise ReportFileError("不支持的报告格式。")
    path = root / f"{report_id}{suffix}"
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or path.is_symlink():
        raise ReportFileError("报告文件路径无效。")
    return path


def validate_filename(filename: str | None) -> tuple[str, ReportSuffix]:
    if (not filename or len(filename) > 200 or any(ord(c) < 32 for c in filename)
            or any(c in filename for c in ("/", "\\", ":"))):
        raise ReportFileError("请提供不含目录的有效文件名，最长 200 个字符。")
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".docx", ".txt", ".md"}:
        raise ReportFileError("仅支持 PDF、DOCX、UTF-8 TXT 或 Markdown 报告。")
    return filename, suffix


def validate_format(path: Path, suffix: ReportSuffix, max_bytes: int) -> None:
    size = path.stat().st_size
    if size > max_bytes:
        raise ReportTooLarge("报告文件超过大小限制。")
    if size == 0:
        raise ReportFileError("报告文件不能为空。")
    with path.open("rb") as handle:
        prefix = handle.read(8)
    if suffix == ".pdf" and not prefix.startswith(b"%PDF-"):
        raise ReportFileError("文件内容不是有效的 PDF 报告。")
    if suffix == ".docx":
        try:
            with ZipFile(path) as archive:
                entries = archive.infolist()
                if "word/document.xml" not in archive.namelist():
                    raise ReportFileError("文件内容不是有效的 DOCX 报告。")
                if (len(entries) > 1000 or sum(item.file_size for item in entries) > 30 * 1024 * 1024
                        or any(item.flag_bits & 1 for item in entries)):
                    raise ReportFileError("DOCX 内容过大、文件过多或已加密，无法读取。")
        except BadZipFile as exc:
            raise ReportFileError("文件内容不是有效的 DOCX 报告。") from exc
    if suffix in {".txt", ".md"}:
        try:
            content = path.read_bytes().decode("utf-8-sig")
        except UnicodeError as exc:
            raise ReportFileError("文本报告必须使用 UTF-8 编码。") from exc
        if not content.strip() or "\x00" in content:
            raise ReportFileError("文本报告为空或包含无效字符。")


def stage_upload(root: Path, source: BinaryIO, suffix: ReportSuffix, max_bytes: int) -> tuple[Path, int, str]:
    root = prepare_storage(root)
    temporary = None
    try:
        digest, size = sha256(), 0
        with tempfile.NamedTemporaryFile(dir=root / ".tmp", suffix=suffix, delete=False) as output:
            temporary = Path(output.name)
            while chunk := source.read(65536):
                size += len(chunk)
                if size > max_bytes:
                    raise ReportTooLarge("报告文件超过大小限制。")
                output.write(chunk)
                digest.update(chunk)
        validate_format(temporary, suffix, max_bytes)
        return temporary, size, digest.hexdigest()
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
