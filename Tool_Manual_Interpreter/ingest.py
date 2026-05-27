# Tool_Manual_Interpreter/ingest.py
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

# Public API
def ingest_manual(manual_url: Optional[str], manual_path: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    """
    Normalize tool manual input into (manual_text, meta)
    [Priority] manual_url (if provided) -> manual_path (txt/md/pdf)

    Returns:
      manual_text: str  (cleaned / normalized)
      meta: dict        (source info for traceability)
    """
    if manual_url:
        raw = fetch_url(manual_url)
        text = html_to_text(raw)
        text = normalize_text(text)
        return text, {"source": "url", "url": manual_url}

    if manual_path:
        p = Path(manual_path)
        if not p.exists():
            raise FileNotFoundError(f"Tool manual path not found: {p}")

        suf = p.suffix.lower()
        if suf in (".txt", ".md"):
            text = read_text_file(p)
            return normalize_text(text), {"source": "path", "path": str(p), "type": "text"}

        if suf == ".pdf":
            text = read_pdf_text(p)
            text = normalize_text(text)
            if not text.strip():
                raise RuntimeError(
                    "PDF text extraction returned empty.\n"
                    "- export the manual as .txt and use TMI_TOOL_MANUAL_PATH to the .txt."
                )
            return text, {"source": "path", "path": str(p), "type": "pdf"}

        # fallback: try read as text
        text = read_text_file(p)
        return normalize_text(text), {"source": "path", "path": str(p), "type": f"text_fallback({suf})"}

    raise RuntimeError("Provide either manual_url or manual_path.")

# URL ingest
def fetch_url(url: str) -> str:
    headers = {
        "User-Agent": "PrZMA-TMI/1.0 (+https://example.invalid)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.1",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def html_to_text(html: str) -> str:
    """
    Lightweight HTML -> text:
    - drop script/style/noscript
    - remove tags
    - decode common entities minimally
    """
    # remove script/style/noscript blocks
    html = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", html)
    # remove comments
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    # replace <br> and </p> with newlines
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    html = re.sub(r"(?is)</p\s*>", "\n", html)
    # remove all remaining tags
    html = re.sub(r"(?is)<[^>]+>", " ", html)

    # basic entity decode
    html = (
        html.replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return html

# File ingest
def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def read_pdf_text(path: Path) -> str:
    """
    Minimal PDF text extraction using PyPDF2.
    If you want better extraction (layout-aware), we can switch to pdfplumber later.
    """
    try:
        import PyPDF2  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "PyPDF2 not installed. Install it (pip install PyPDF2) or provide a .txt manual."
        ) from e

    out = []
    with path.open("rb") as f:
        reader = PyPDF2.PdfReader(f)
        for i, page in enumerate(reader.pages):
            try:
                out.append(page.extract_text() or "")
            except Exception:
                out.append("")
    return "\n".join(out)

# Normalization
def normalize_text(text: str) -> str:
    """
    Make the manual text more LLM-friendly:
    - collapse excessive whitespace
    - keep paragraph boundaries
    - strip super long runs of blank lines
    """
    # normalize newlines
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # collapse spaces/tabs (but keep newlines)
    text = re.sub(r"[ \t]+", " ", text)
    # collapse too many blank lines
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    # strip edges
    return text.strip()
