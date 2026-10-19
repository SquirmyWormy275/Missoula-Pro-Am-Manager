"""Upload security validation and storage helpers."""
from __future__ import annotations

import io
import os
import re
import subprocess
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import iterparse
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


MAX_EXPANDED_BYTES = 20 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_ZIP_ENTRIES = 256
MAX_ROWS = 10000
MAX_COLUMNS = 100
MAX_CELLS = 100000


def validate_xlsx_container(stream):
    """Bound decompression and worksheet allocation before Excel libraries run."""
    compressed = stream.read(16 * 1024 * 1024 + 1)
    if len(compressed) > 16 * 1024 * 1024:
        raise ValueError('Workbook exceeds the upload size limit.')
    # Python 3.10 Werkzeug uploads may use a SpooledTemporaryFile without
    # seekable(); a bounded in-memory view is compatible with zipfile.
    with zipfile.ZipFile(io.BytesIO(compressed)) as archive:
        members = archive.infolist()
        if len(members) > MAX_ZIP_ENTRIES or sum(m.file_size for m in members) > MAX_EXPANDED_BYTES:
            raise ValueError('Workbook exceeds the expanded size or file-count limit.')
        node_count = 0
        for member in members:
            if (member.flag_bits & 1 or member.file_size > MAX_MEMBER_BYTES or
                    member.file_size > max(1, member.compress_size) * 100):
                raise ValueError('Workbook contains an oversized or excessively compressed member.')
            if not member.filename.endswith('.xml'):
                continue
            data = archive.read(member)
            if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
                raise ValueError('Workbook XML declarations are not allowed.')
            cells = 0
            for _, element in iterparse(io.BytesIO(data), events=('end',)):
                node_count += 1
                if node_count > 250000:
                    raise ValueError('Workbook XML exceeds the node limit.')
                tag = element.tag.rsplit('}', 1)[-1]
                if tag == 'row' and int(element.get('r', '0')) > MAX_ROWS:
                    raise ValueError('Workbook row exceeds import limits.')
                if tag in {'dimension', 'c'}:
                    reference = element.get('ref' if tag == 'dimension' else 'r', '')
                    for col, row in re.findall(r'([A-Z]+)([0-9]+)', reference, flags=re.IGNORECASE):
                        column = 0
                        for char in col.upper():
                            column = column * 26 + ord(char) - 64
                        if int(row) > MAX_ROWS or column > MAX_COLUMNS:
                            raise ValueError('Workbook worksheet dimensions exceed import limits.')
                if tag in {'c', 'si', 'row'}:
                    cells += 1
                    if cells > MAX_CELLS:
                        raise ValueError('Workbook contains too many cells or strings.')
                element.clear()


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

    if extension == 'xlsx':
        try:
            validate_xlsx_container(file.stream)
        except (ValueError, OSError, zipfile.BadZipFile, ET.ParseError, DefusedXmlException, RuntimeError) as exc:
            return UploadValidationResult(ok=False, error=f'Invalid or oversized workbook: {exc}')
        finally:
            file.stream.seek(0)

    safe_name = f'{uuid.uuid4().hex}.{extension}'
    return UploadValidationResult(ok=True, extension=extension, safe_name=safe_name)


def save_upload(file: FileStorage, upload_folder: str, filename: str) -> str:
    os.makedirs(upload_folder, mode=0o700, exist_ok=True)
    path = os.path.join(upload_folder, filename)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as destination:
            file.save(destination)
    except Exception:
        os.unlink(path)
        raise
    return path


def malware_scan(path: str, enabled: bool = False, command_template: str = '') -> None:
    """Optional malware scan hook. Raises RuntimeError if scan command fails."""
    if not enabled or not command_template:
        return
    import shlex
    command = command_template.replace('{path}', shlex.quote(path))
    args = shlex.split(command)
    result = subprocess.run(args, shell=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'Malware scan failed: {result.stderr.strip() or result.stdout.strip()}')

