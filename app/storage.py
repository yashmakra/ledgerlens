import hashlib
import os
from pathlib import Path
from uuid import uuid4

ALLOWED_CONTENT_TYPES = {
    "application/pdf": {".pdf"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/tiff": {".tif", ".tiff"},
}


def storage_root() -> Path:
    root = Path(os.getenv("DOCUMENT_STORAGE_DIR", "./data/documents"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def max_upload_bytes() -> int:
    return int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024


def detect_media_type(content: bytes) -> str | None:
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    return None


def validate_content(content: bytes, original_filename: str) -> str:
    media_type = detect_media_type(content)
    if not media_type:
        raise ValueError("Unsupported or invalid file signature")
    extension = Path(original_filename).suffix.lower()
    if extension not in ALLOWED_CONTENT_TYPES[media_type]:
        raise ValueError("Filename extension does not match file content")
    return media_type


def save_content(content: bytes, original_filename: str) -> tuple[str, str, str]:
    media_type = validate_content(content, original_filename)
    extension = Path(original_filename).suffix.lower()
    document_id = str(uuid4())
    destination = storage_root() / f"{document_id}{extension}"
    destination.write_bytes(content)
    return document_id, str(destination), media_type


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
