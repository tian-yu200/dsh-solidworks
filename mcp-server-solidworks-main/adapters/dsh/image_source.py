"""Resolve drawing inputs shared by OCR and crop readers."""
import os
import re
from pathlib import Path

ATTACHMENT_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$", re.IGNORECASE)
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

# DSH keeps image attachments under objects/ and every other upload (a PDF) under file-objects/.
ATTACHMENT_STORES = ("objects", "file-objects")


def resolve_image_path(image_path, workspace_root=None):
    text = (image_path or "").strip()
    if not text:
        raise RuntimeError("MISSING_PARAMETER: image_path is required (a local image or PDF path, or sha256 attachment id).")
    match = ATTACHMENT_RE.fullmatch(text)
    if text.lower().startswith("sha256:"):
        if not match:
            raise RuntimeError("INVALID_ATTACHMENT_ID: expected sha256:<64 hexadecimal characters>.")
        digest = match.group(1).lower()
        home = Path(os.getenv("DSH_HOME", str(Path.home() / ".dsh"))).expanduser()
        for store in ATTACHMENT_STORES:
            candidate = home / "attachments" / "v1" / store / digest[:2] / digest
            if candidate.is_file():
                return candidate.resolve()
        raise RuntimeError("ATTACHMENT_NOT_FOUND: attachment object was not found in the current DSH instance.")
    if SCHEME_RE.match(text) or text.lower().startswith("file:"):
        raise RuntimeError("LOCAL_PATH_REQUIRED: image_path must be a local file path or sha256 attachment id.")
    path = Path(text).expanduser()
    if not path.is_absolute() and workspace_root is not None:
        path = Path(workspace_root) / path
    path = path.resolve()
    if not path.exists():
        raise RuntimeError(f"FILE_NOT_FOUND: no such file: {path}")
    if not path.is_file():
        raise RuntimeError(f"NOT_A_FILE: image_path is not a file: {path}")
    return path
