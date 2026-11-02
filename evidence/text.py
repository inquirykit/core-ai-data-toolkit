"""
The words of a capture, for its workspace's search (store.py's ``search``
table): the visible text of its HTML and MHTML artifacts and the content of its
plain text ones, as one text, at most MAX_TEXT characters. Screenshots and
other files have no words to give. Only the standard library: an artifact
that does not parse gives what it can, never an error.

``snippet`` cuts the part of a text around the first word a search looks for.
"""

import email
import email.policy
import re
from html import unescape
from html.parser import HTMLParser
from typing import Iterable, List, Optional, Sequence, Tuple

# Characters of a capture's text kept for the search: a long page's first ones.
MAX_TEXT = 500_000
# Characters around a search's first word in a snippet.
SNIPPET = 160

HTML_TYPES = ("text/html", "application/xhtml+xml")
MHTML_TYPES = ("multipart/related", "message/rfc822", "application/x-mimearchive")
# Elements whose content is not text a reader sees.
HIDDEN = {"script", "style", "noscript", "template", "svg", "head"}
# Elements that end a line of text, so words on either side stay apart.
BLOCKS = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
          "header", "footer", "nav", "aside", "main", "blockquote", "pre", "table", "ul", "ol", "dd", "dt", "form",
          "option", "hr"}


class _Text(HTMLParser):
    """The text a reader of the page sees, a line per block."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.hidden = 0  # a head never closed hides nothing past here
        elif tag in HIDDEN:
            self.hidden += 1
        elif tag in BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        elif tag in BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _tidy(text: str) -> str:
    """Spaces collapsed within lines, empty lines dropped."""
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def html_text(html: str) -> str:
    parser = _Text()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # HTMLParser is lenient; anything left is a page this cannot read
        pass
    return _tidy(unescape("".join(parser.parts)))


def _decode(data: bytes, charset: Optional[str] = None) -> str:
    for encoding in (charset, "utf-8"):
        if encoding:
            try:
                return data.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
    return data.decode("utf-8", errors="replace")


def mhtml_text(data: bytes) -> str:
    """The text of an MHTML snapshot's HTML and plain text parts (its frames' too)."""
    try:
        message = email.message_from_bytes(data, policy=email.policy.default)
        texts = []
        for part in message.walk():
            kind = part.get_content_type()
            if kind not in ("text/html", "text/plain"):
                continue
            payload = part.get_payload(decode=True) or b""
            text = _decode(payload, part.get_content_charset())
            texts.append(html_text(text) if kind == "text/html" else _tidy(text))
        return "\n".join(t for t in texts if t)
    except Exception:
        return ""


def _kind(name: str, media_type: str) -> Optional[str]:
    media_type = (media_type or "").split(";")[0].strip().lower()
    name = name.lower()
    if media_type in HTML_TYPES or name.endswith((".html", ".htm", ".xhtml")):
        return "html"
    if media_type in MHTML_TYPES or name.endswith((".mhtml", ".mht")):
        return "mhtml"
    if media_type == "text/plain" or name.endswith(".txt"):
        return "text"
    return None


def capture_text(artifacts: Iterable[Tuple[str, str, bytes]]) -> str:
    """The words of a capture's artifacts, given as (name, media type, bytes): its pages' text, in their order."""
    texts = []
    for name, media_type, data in artifacts:
        kind = _kind(name, media_type)
        if kind == "html":
            texts.append(html_text(_decode(data)))
        elif kind == "mhtml":
            texts.append(mhtml_text(data))
        elif kind == "text":
            texts.append(_tidy(_decode(data)))
    return "\n".join(t for t in texts if t)[:MAX_TEXT]


def snippet(text: Optional[str], terms: Sequence[str], width: int = SNIPPET) -> Optional[str]:
    """The ``width`` characters of ``text`` around the first of ``terms`` (lowercase words or phrases) it holds,
    with "…" where it was cut; None when it holds none."""
    if not text or not terms:
        return None
    lowered = text.lower()
    found = [(i, term) for term in terms if term and (i := lowered.find(term)) >= 0]
    if not found:
        return None
    at, term = min(found)
    start = max(0, at - (width - len(term)) // 2)
    end = min(len(text), start + width)
    start = max(0, end - width)
    cut = re.sub(r"\s+", " ", text[start:end]).strip()
    return ("…" if start else "") + cut + ("…" if end < len(text) else "")
