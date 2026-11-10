"""Turn a question, pasted text, or an upload into plain text for one run.

The upload is not written to disk. A zip is read in memory. Only text is returned.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ZIP_FILES = 100
MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024
MAX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
INPUT_CHAR_CAP = 12_000

TEXT_EXTENSIONS = frozenset(
    {
        ".txt", ".md", ".markdown", ".rst",
        ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
        ".json", ".html", ".htm", ".css", ".scss",
        ".yml", ".yaml", ".toml", ".ini", ".cfg",
        ".rs", ".go", ".java", ".c", ".h", ".cpp", ".hpp", ".cc",
        ".rb", ".php", ".sh", ".bash", ".sql", ".xml", ".csv",
        ".swift", ".kt", ".kts", ".lua", ".pl", ".r",
        ".vue", ".svelte",
    }
)
ZIP_EXTENSIONS = frozenset({".zip"})

INTENTS = {
    "find": "Find anything wrong",
    "improve": "Help me make this better",
    "test": "Run a test on it",
}


class IntakeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def privacy_accepted(value) -> bool:
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() in {"yes", "on", "true", "1"}:
        return True
    return False


def assemble(fields: dict, upload: tuple[str, bytes] | None, cap: int, limits: dict | None = None) -> str:
    if not privacy_accepted(fields.get("privacy")):
        raise IntakeError(
            "privacy_required",
            "Check the privacy notice before a test is sent. Nothing was stored.",
        )
    intent = INTENTS.get(str(fields.get("intent") or "").strip())
    question = _plain(fields.get("question"))
    paste = _plain(fields.get("paste") or fields.get("document"))
    file_text = ""
    file_label = ""
    if upload is not None:
        file_label, file_text = read_upload(upload[0], upload[1], limits=limits)
    parts: list[str] = []
    if intent:
        parts.append("Intent: " + intent)
    if question:
        parts.append(question)
    if paste:
        parts.append(paste)
    if file_text:
        parts.append(f"From {file_label}:\n{file_text}")
    text = "\n\n".join(parts).strip()
    if not text:
        raise IntakeError("empty", "Type a question, paste text, or upload a file.")
    limit = cap if cap and cap > 0 else INPUT_CHAR_CAP
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n\n[truncated]"
    return text


def read_upload(filename: str, data: bytes, *, limits: dict | None = None) -> tuple[str, str]:
    limits = limits or {}
    max_upload = int(limits.get("max_upload", MAX_UPLOAD_BYTES))
    if len(data) > max_upload:
        raise IntakeError("too_large", "The upload is larger than 2 MB.")
    name = safe_filename(filename)
    suffix = Path(name).suffix.lower()
    if suffix in ZIP_EXTENSIONS:
        text = read_zip(data, limits)
        return name, text
    if suffix not in TEXT_EXTENSIONS:
        raise IntakeError("bad_type", "Upload a text or code file, or a .zip. That file type is not accepted.")
    return name, _decode_text(data)


def safe_filename(filename: str) -> str:
    raw = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not raw or raw in {".", ".."} or "\x00" in raw or len(raw) > 180:
        raise IntakeError("bad_name", "That file name is not accepted.")
    if raw.startswith("."):
        raise IntakeError("bad_name", "That file name is not accepted.")
    return raw


def read_zip(data: bytes, limits: dict | None = None) -> str:
    limits = limits or {}
    max_files = int(limits.get("max_files", MAX_ZIP_FILES))
    max_uncompressed = int(limits.get("max_uncompressed", MAX_UNCOMPRESSED_BYTES))
    max_member = int(limits.get("max_member", MAX_MEMBER_BYTES))
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise IntakeError("bad_zip", "That zip could not be read.") from exc
    chunks: list[str] = []
    total = 0
    seen = 0
    try:
        for info in archive.infolist():
            if info.is_dir():
                continue
            seen += 1
            if seen > max_files:
                raise IntakeError("zip_bomb", "That zip has too many files.")
            _reject_member(info, max_member)
            total += info.file_size
            if total > max_uncompressed:
                raise IntakeError("zip_bomb", "That zip expands too far. It was not unpacked.")
            member = _member_name(info.filename)
            suffix = Path(member).suffix.lower()
            if suffix not in TEXT_EXTENSIONS:
                continue
            try:
                payload = archive.read(info)
            except RuntimeError as exc:
                raise IntakeError("bad_zip", "That zip could not be read.") from exc
            if len(payload) > max_member:
                raise IntakeError("zip_bomb", "That zip expands too far. It was not unpacked.")
            try:
                text = _decode_text(payload)
            except IntakeError:
                continue
            chunks.append(f"{member}\n{text}")
    finally:
        archive.close()
    if not chunks:
        raise IntakeError("no_text", "No text or code file was found in that zip.")
    return "\n\n".join(chunks)


def _reject_member(info: zipfile.ZipInfo, max_member: int) -> None:
    if info.flag_bits & 0x1:
        raise IntakeError("bad_zip", "Encrypted zip files are not accepted.")
    _member_name(info.filename)
    if info.file_size < 0 or info.file_size > max_member:
        raise IntakeError("zip_bomb", "A file in that zip is too large.")
    compressed = info.compress_size or 0
    if compressed and info.file_size > 100_000 and info.file_size / compressed > MAX_COMPRESSION_RATIO:
        raise IntakeError("zip_bomb", "That zip expands too far. It was not unpacked.")


def _member_name(filename: str) -> str:
    raw = (filename or "").replace("\\", "/")
    if raw.startswith("/") or raw.startswith("~"):
        raise IntakeError("traversal", "A path in that zip is not allowed.")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise IntakeError("traversal", "A path in that zip is not allowed.")
    name = parts[-1]
    if "\x00" in name:
        raise IntakeError("traversal", "A path in that zip is not allowed.")
    return "/".join(parts)


def _decode_text(data: bytes) -> str:
    if b"\x00" in data:
        raise IntakeError("not_text", "That file does not look like text.")
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise IntakeError("not_text", "That file does not look like text.") from None
    if any(ord(ch) < 32 and ch not in "\n\t\r" for ch in text):
        raise IntakeError("not_text", "That file does not look like text.")
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _plain(value) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in cleaned):
        raise IntakeError("bad_text", "The text has characters this form will not send.")
    return cleaned


def parse_multipart(content_type: str, body: bytes, max_file: int) -> tuple[dict, tuple[str, bytes] | None]:
    from email.parser import BytesParser
    from email.policy import HTTP

    if len(body) > max_file + 80_000:
        raise IntakeError("too_large", "The upload is larger than 2 MB.")
    header = b"Content-Type: " + content_type.encode("ascii", errors="replace") + b"\r\nMIME-Version: 1.0\r\n\r\n"
    message = BytesParser(policy=HTTP).parsebytes(header + body)
    if not message.is_multipart():
        raise IntakeError("bad_form", "The upload form could not be read.")
    fields: dict[str, str] = {}
    upload: tuple[str, bytes] | None = None
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        data = payload if isinstance(payload, bytes) else b""
        if filename:
            if upload is not None:
                raise IntakeError("bad_form", "Send one file.")
            if len(data) > max_file:
                raise IntakeError("too_large", "The upload is larger than 2 MB.")
            upload = (filename, data)
            continue
        try:
            fields[str(name)] = data.decode("utf-8")
        except UnicodeError:
            raise IntakeError("bad_text", "The form text is not UTF-8.") from None
    return fields, upload
