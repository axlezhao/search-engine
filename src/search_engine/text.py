"""HTML parsing, tokenization, and stemming."""

from __future__ import annotations

import re
import warnings
from collections import Counter

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning, XMLParsedAsHTMLWarning
from nltk.stem import PorterStemmer


# Unicode letters and digits, excluding underscore. This follows the assignment's
# definition of tokens as all alphanumeric sequences.
ALNUM_RE = re.compile(r"[^\W_]+", re.UNICODE)
IMPORTANT_TAGS = ("title", "h1", "h2", "h3", "b", "strong")
_STEMMER = PorterStemmer()


def tokenize(text: str) -> list[str]:
    """Return case-folded, Porter-stemmed alphanumeric tokens."""
    return [_STEMMER.stem(match.group(0).casefold()) for match in ALNUM_RE.finditer(text)]


def extract_document_fields(html: str) -> tuple[Counter[str], Counter[str], str, str]:
    """Return term frequencies and display fields from possibly broken HTML.

    BeautifulSoup's built-in parser is deliberately used because the corpus contains
    malformed and non-HTML content. Script/style text is excluded from indexing.
    Important-field counts are supplemental signals; those occurrences also remain
    in the total term frequency.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        soup = BeautifulSoup(html or "", "html.parser")
    for ignored in soup(("script", "style", "noscript")):
        ignored.decompose()

    visible_text = " ".join(soup.get_text(" ", strip=True).split())
    total = Counter(tokenize(visible_text))
    important: Counter[str] = Counter()
    for element in soup.find_all(IMPORTANT_TAGS):
        important.update(tokenize(element.get_text(" ", strip=True)))
    title_element = soup.find("title") or soup.find("h1")
    title = " ".join(title_element.get_text(" ", strip=True).split()) if title_element else ""
    snippet = visible_text[:320]
    if len(visible_text) > 320:
        snippet = snippet.rsplit(" ", 1)[0] + "…"
    return total, important, title[:240], snippet


def extract_term_counts(html: str) -> tuple[Counter[str], Counter[str]]:
    """Backward-compatible term-frequency helper."""
    total, important, _, _ = extract_document_fields(html)
    return total, important
