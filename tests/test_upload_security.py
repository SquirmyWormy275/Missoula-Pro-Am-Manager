"""Tests for services/upload_security.py — magic-byte validation, UUID filenames, scan hook."""
from __future__ import annotations

import io
import os
import re
import tempfile
import uuid
from unittest.mock import MagicMock, patch

import pytest
from werkzeug.datastructures import FileStorage

from services.upload_security import (
    UploadValidationResult,
    malware_scan,
    save_upload,
    validate_excel_upload,
)

# ---------------------------------------------------------------------------
# Constants (mirrors the module's private magic bytes)
# ---------------------------------------------------------------------------
XLSX_MAGIC = b"PK\x03\x04"
XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ALLOWED = {"xlsx", "xls"}

# Hex UUID pattern (32 hex chars)
_UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_file(filename: str, content: bytes) -> FileStorage:
    """Build a minimal FileStorage with the given filename and byte content."""
    stream = io.BytesIO(content)
    return FileStorage(stream=stream, filename=filename)


# ---------------------------------------------------------------------------
# validate_excel_upload — valid inputs
# ---------------------------------------------------------------------------
class TestValidXlsx:
    """Valid .xlsx uploads should pass magic-byte check."""

    def test_valid_xlsx_minimal(self):
        """Bare XLSX magic bytes (4 bytes) pass validation."""
        fs = _make_file("report.xlsx", XLSX_MAGIC)
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is True
        assert result.extension == "xlsx"
        assert result.error == ""

    def test_valid_xlsx_with_trailing_data(self):
        """Real-world XLSX files have data after the magic header."""
        content = XLSX_MAGIC + b"\x00" * 1024
        fs = _make_file("data.xlsx", content)
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is True
        assert result.extension == "xlsx"

    def test_stream_rewound_after_validation(self):
        """Stream position must be reset to 0 after sniffing magic bytes."""
        content = XLSX_MAGIC + b"extra payload"
        fs = _make_file("file.xlsx", content)
        validate_excel_upload(fs, ALLOWED)
        assert fs.stream.read() == content


class TestValidXls:
    """Valid .xls uploads should pass magic-byte check."""

    def test_valid_xls_minimal(self):
        fs = _make_file("legacy.xls", XLS_MAGIC)
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is True
        assert result.extension == "xls"

    def test_valid_xls_with_trailing_data(self):
        content = XLS_MAGIC + b"\xff" * 512
        fs = _make_file("old_report.xls", content)
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is True
        assert result.extension == "xls"


# ---------------------------------------------------------------------------
# validate_excel_upload — invalid magic bytes
# ---------------------------------------------------------------------------
class TestInvalidMagicBytes:
    """Files with wrong or missing magic bytes should be rejected."""

    def test_xlsx_with_random_bytes(self):
        fs = _make_file("fake.xlsx", b"\x00\x01\x02\x03\x04\x05\x06\x07")
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is False
        assert "not a valid .xlsx" in result.error

    def test_xls_with_random_bytes(self):
        fs = _make_file("fake.xls", b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11")
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is False
        assert "not a valid .xls" in result.error

    def test_xlsx_with_xls_magic(self):
        """XLS magic bytes inside an .xlsx filename should fail."""
        fs = _make_file("crossed.xlsx", XLS_MAGIC)
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is False

    def test_xls_with_xlsx_magic(self):
        """XLSX (PK) magic bytes inside an .xls filename should fail."""
        fs = _make_file("crossed.xls", XLSX_MAGIC)
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is False

    def test_empty_bytes(self):
        fs = _make_file("empty.xlsx", b"")
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is False

    def test_single_byte(self):
        fs = _make_file("tiny.xls", b"\xd0")
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is False

    def test_pdf_magic_in_xlsx(self):
        """A PDF disguised as .xlsx must be rejected."""
        fs = _make_file("sneaky.xlsx", b"%PDF-1.4 rest of file")
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is False

    def test_png_magic_in_xls(self):
        """A PNG disguised as .xls must be rejected."""
        fs = _make_file("image.xls", b"\x89PNG\r\n\x1a\n")
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is False


# ---------------------------------------------------------------------------
# validate_excel_upload — filename / extension edge cases
# ---------------------------------------------------------------------------
class TestFilenameSanitization:
    """Filename and extension validation edge cases."""

    def test_missing_filename_none(self):
        fs = _make_file("", XLSX_MAGIC)
        fs.filename = None
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is False
        assert "Missing filename" in result.error

    def test_missing_filename_empty(self):
        fs = _make_file("", XLSX_MAGIC)
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is False

    def test_no_extension(self):
        fs = _make_file("noext", XLSX_MAGIC)
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is False

    def test_disallowed_extension(self):
        fs = _make_file("malware.exe", b"MZ\x90\x00")
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is False
        assert "Invalid file extension" in result.error

    def test_csv_not_allowed(self):
        fs = _make_file("data.csv", b"a,b,c\n1,2,3\n")
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is False

    def test_extension_case_insensitive(self):
        """Uppercase .XLSX should still be accepted."""
        fs = _make_file("REPORT.XLSX", XLSX_MAGIC + b"\x00" * 100)
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is True
        assert result.extension == "xlsx"

    def test_double_extension(self):
        """Only the last extension matters (werkzeug secure_filename)."""
        fs = _make_file("report.csv.xlsx", XLSX_MAGIC)
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is True
        assert result.extension == "xlsx"

    def test_path_traversal_filename(self):
        """Path traversal in filename must not leak into safe_name."""
        fs = _make_file("../../etc/passwd.xlsx", XLSX_MAGIC)
        result = validate_excel_upload(fs, ALLOWED)
        # werkzeug secure_filename strips traversal — should still validate
        assert result.ok is True
        assert ".." not in result.safe_name
        assert "/" not in result.safe_name
        assert "\\" not in result.safe_name

    def test_spaces_and_special_chars(self):
        """Special characters in filename produce a safe UUID name."""
        fs = _make_file("my report (final) [v2].xlsx", XLSX_MAGIC)
        result = validate_excel_upload(fs, ALLOWED)
        assert result.ok is True
        # safe_name should be UUID-based, not contain spaces/brackets
        assert " " not in result.safe_name
        assert "[" not in result.safe_name

    def test_custom_allowed_set(self):
        """Only extensions in the provided set are allowed."""
        fs = _make_file("data.xlsx", XLSX_MAGIC)
        result = validate_excel_upload(fs, {"csv"})
        assert result.ok is False
        assert "Invalid file extension" in result.error


# ---------------------------------------------------------------------------
# UUID safe_name generation
# ---------------------------------------------------------------------------
class TestSafeNameGeneration:
    """The safe_name returned on success must be UUID-hex based."""

    def test_safe_name_is_uuid_hex_with_extension(self):
        fs = _make_file("report.xlsx", XLSX_MAGIC)
        result = validate_excel_upload(fs, ALLOWED)
        stem, ext = result.safe_name.rsplit(".", 1)
        assert ext == "xlsx"
        assert _UUID_HEX_RE.match(stem), f"stem '{stem}' is not a UUID hex"

    def test_safe_name_preserves_xls_extension(self):
        fs = _make_file("old.xls", XLS_MAGIC)
        result = validate_excel_upload(fs, ALLOWED)
        assert result.safe_name.endswith(".xls")

    def test_safe_names_are_unique(self):
        """Two validations of the same file should produce different UUIDs."""
        names = set()
        for _ in range(10):
            fs = _make_file("same.xlsx", XLSX_MAGIC)
            result = validate_excel_upload(fs, ALLOWED)
            names.add(result.safe_name)
        assert len(names) == 10

    def test_safe_name_not_set_on_failure(self):
        fs = _make_file("bad.xlsx", b"\x00\x00\x00\x00")
        result = validate_excel_upload(fs, ALLOWED)
        assert result.safe_name == ""


# ---------------------------------------------------------------------------
# save_upload
# ---------------------------------------------------------------------------
class TestSaveUpload:
    """save_upload should create directories and persist the file."""

    def test_creates_directory_and_saves(self, tmp_path):
        dest = str(tmp_path / "uploads" / "nested")
        content = XLSX_MAGIC + b"file body"
        fs = _make_file("report.xlsx", content)
        path = save_upload(fs, dest, "safe.xlsx")
        assert os.path.isfile(path)
        with open(path, "rb") as f:
            assert f.read() == content

    def test_returns_full_path(self, tmp_path):
        dest = str(tmp_path / "up")
        fs = _make_file("x.xlsx", XLSX_MAGIC)
        path = save_upload(fs, dest, "abc.xlsx")
        assert path == os.path.join(dest, "abc.xlsx")

    def test_overwrites_existing(self, tmp_path):
        dest = str(tmp_path)
        fs1 = _make_file("a.xlsx", b"first")
        save_upload(fs1, dest, "same.xlsx")
        fs2 = _make_file("b.xlsx", b"second")
        save_upload(fs2, dest, "same.xlsx")
        with open(os.path.join(dest, "same.xlsx"), "rb") as f:
            assert f.read() == b"second"


# ---------------------------------------------------------------------------
# malware_scan
# ---------------------------------------------------------------------------
class TestMalwareScan:
    """malware_scan is a no-op by default; raises on failure when enabled."""

    def test_disabled_by_default(self):
        """Should not raise or call anything when disabled."""
        malware_scan("/some/path")  # no error

    def test_disabled_explicit(self):
        malware_scan("/some/path", enabled=False, command_template="false")

    def test_empty_command_template_is_noop(self):
        malware_scan("/some/path", enabled=True, command_template="")

    @patch("services.upload_security.subprocess.run")
    def test_enabled_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        malware_scan("/tmp/file.xlsx", enabled=True, command_template="clamscan {path}")
        mock_run.assert_called_once_with(
            "clamscan /tmp/file.xlsx", shell=True, capture_output=True, text=True
        )

    @patch("services.upload_security.subprocess.run")
    def test_enabled_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stderr="FOUND virus", stdout=""
        )
        with pytest.raises(RuntimeError, match="Malware scan failed"):
            malware_scan("/tmp/bad.xlsx", enabled=True, command_template="clamscan {path}")

    @patch("services.upload_security.subprocess.run")
    def test_failure_uses_stdout_when_stderr_empty(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=2, stderr="", stdout="scan error output"
        )
        with pytest.raises(RuntimeError, match="scan error output"):
            malware_scan("/tmp/f.xlsx", enabled=True, command_template="scan {path}")

    @patch("services.upload_security.subprocess.run")
    def test_path_substitution(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        malware_scan(
            "/data/uploads/abc.xlsx",
            enabled=True,
            command_template="scanner --file={path} --mode=quick",
        )
        mock_run.assert_called_once_with(
            "scanner --file=/data/uploads/abc.xlsx --mode=quick",
            shell=True,
            capture_output=True,
            text=True,
        )


# ---------------------------------------------------------------------------
# UploadValidationResult dataclass
# ---------------------------------------------------------------------------
class TestUploadValidationResult:
    """Basic dataclass contract checks."""

    def test_defaults(self):
        r = UploadValidationResult(ok=True)
        assert r.ok is True
        assert r.error == ""
        assert r.extension == ""
        assert r.safe_name == ""

    def test_all_fields(self):
        r = UploadValidationResult(
            ok=True, error="", extension="xlsx", safe_name="abc123.xlsx"
        )
        assert r.extension == "xlsx"
        assert r.safe_name == "abc123.xlsx"
