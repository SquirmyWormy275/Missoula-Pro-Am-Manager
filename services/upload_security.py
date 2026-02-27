"""Upload security validation and storage helpers."""
from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename


_XLSX_MAGIC = b'PK\x03\x04'
_XLS_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'


@dataclass
class UploadValidationResult:
    ok: bool
    error: str = ''
    extension: str = ''
    safe_name: str = ''


def validate_excel_upload(file: FileStorage, allowed_extensions: set[str]) -> UploadValidationResult:
    filename = secure_filename(file.filename or '')
    if not filename or '.' not in filename:
        return UploadValidationResult(ok=False, error='Missing filename.')

    extension = filename.rsplit('.', 1)[1].lower()
    if extension not in allowed_extensions:
        return UploadValidationResult(ok=False, error='Invalid file extension.')

    sniff = file.stream.read(8)
    file.stream.seek(0)
    if extension == 'xlsx' and not sniff.startswith(_XLSX_MAGIC):
        return UploadValidationResult(ok=False, error='File content is not a valid .xlsx container.')
    if extension == 'xls' and not sniff.startswith(_XLS_MAGIC):
        return UploadValidationResult(ok=False, error='File content is not a valid .xls workbook.')

    safe_name = f'{uuid.uuid4().hex}.{extension}'
    return UploadValidationResult(ok=True, extension=extension, safe_name=safe_name)


def save_upload(file: FileStorage, upload_folder: str, filename: str) -> str:
    os.makedirs(upload_folder, exist_ok=True)
    path = os.path.join(upload_folder, filename)
    file.save(path)
    return path


def malware_scan(path: str, enabled: bool = False, command_template: str = '') -> None:
    """Optional malware scan hook. Raises RuntimeError if scan command fails."""
    if not enabled or not command_template:
        return
    command = command_template.replace('{path}', path)
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'Malware scan failed: {result.stderr.strip() or result.stdout.strip()}')

