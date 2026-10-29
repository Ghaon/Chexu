from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from src.agent.config import PROJECT_ROOT
from src.domain.reports import ReportFileError, ReportReadError, ReportTooLarge
from src.reports.parser import parse_report
from src.reports.storage import report_path, stage_upload, validate_filename


class ReportFileTests(unittest.TestCase):
    def setUp(self):
        cache = PROJECT_ROOT / ".cache/tmp"
        cache.mkdir(parents=True, exist_ok=True)
        self.temporary = TemporaryDirectory(dir=cache)
        self.root = Path(self.temporary.name)
        self.assertTrue(self.root.resolve().is_relative_to(cache.resolve()))
        self.addCleanup(self.temporary.cleanup)

    def test_upload_checks_real_format_size_and_cleans_failed_staging_files(self):
        with self.assertRaises(ReportTooLarge):
            stage_upload(self.root, BytesIO(b"x" * 1025), ".txt", 1024)
        with self.assertRaises(ReportFileError):
            stage_upload(self.root, BytesIO(b"not a pdf"), ".pdf", 1024)
        with self.assertRaises(ReportFileError):
            stage_upload(self.root, BytesIO(b"\xff\xfe"), ".txt", 1024)
        self.assertEqual(list((self.root / ".tmp").iterdir()), [])

    def test_filenames_and_storage_keys_cannot_choose_arbitrary_paths(self):
        for name in ("../report.txt", "E:\\secret.txt", "report.exe", "bad\x00.txt"):
            with self.subTest(name=name), self.assertRaises(ReportFileError):
                validate_filename(name)
        self.assertEqual(validate_filename("检测报告.PDF"), ("检测报告.PDF", ".pdf"))
        with self.assertRaises(ReportFileError):
            report_path(self.root, uuid4(), "../secret")

    def test_chunks_have_stable_citations_and_do_not_hide_truncation(self):
        report_id = uuid4()
        content = ("检测记录。" * 1000).encode("utf-8")
        path = report_path(self.root, report_id, ".txt")
        path.write_bytes(content)
        result = parse_report(self.root, report_id, ".txt", sha256(content).hexdigest(), 1, 1,
                               max_bytes=20000, max_text_chars=3200)
        self.assertEqual((result.total_chunks, result.has_more, result.truncated), (2, True, True))
        self.assertEqual(result.chunks[0].citation, f"[报告 {report_id}#片段1]")
        self.assertEqual(len(result.chunks[0].text), 1600)

    def test_changed_content_and_out_of_range_reads_are_rejected(self):
        report_id = uuid4()
        path = report_path(self.root, report_id, ".txt")
        path.write_text("原文内容", encoding="utf-8")
        digest = sha256(path.read_bytes()).hexdigest()
        path.write_text("被替换的内容", encoding="utf-8")
        with self.assertRaisesRegex(ReportReadError, "摘要不一致"):
            parse_report(self.root, report_id, ".txt", digest, 1, 3, max_bytes=1024, max_text_chars=1600)
        with self.assertRaisesRegex(ReportReadError, "超过范围"):
            parse_report(self.root, report_id, ".txt", sha256(path.read_bytes()).hexdigest(), 2, 3,
                         max_bytes=1024, max_text_chars=1600)


if __name__ == "__main__":
    unittest.main()
