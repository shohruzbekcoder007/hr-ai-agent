"""
Document structure extraction, profiles, and query routing for RAG.

Works for laws (bob/modda), decrees, regulations, and unstructured PDFs.
Embedding-service is NOT involved — only text analysis + metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Patterns: Uzbek Latin / Cyrillic + Russian-ish headings
# ---------------------------------------------------------------------------

# 5-bob / 1-боб / V bob / глава 5. Title...
# Note: Latin "bob" and Cyrillic "боб" are different letters.
_CHAPTER_RE = re.compile(
    r"(?m)^[\s]*"
    r"(?:"
    r"(?P<num1>\d{1,3})\s*[-–—.]?\s*(?:bob|боб)\b"
    r"|(?P<num2>[IVXLC]{1,8})\s*[-–—.]?\s*(?:bob|боб)\b"
    r"|(?P<num3>\d{1,3})\s*[-–—.]?\s*(?:бўлим|булим)\b"
    r"|(?P<num4>[IVXLC]{1,8})\s*[-–—.]?\s*(?:бўлим|булим|БЎЛИМ)\b"
    r"|(?P<num5>\d{1,3})\s*[-–—.]?\s*глава\b"
    r"|глава\s*(?P<num6>\d{1,3})\b"
    r"|(?:bob|боб)\s*(?P<num7>\d{1,3})\b"
    r")"
    r"[\s.:–—-]*(?P<title>[^\n]{0,120})?",
    re.IGNORECASE,
)

# 15-modda / 15-modda. / Статья 15
_ARTICLE_RE = re.compile(
    r"(?im)^[\s]*"
    r"(?:"
    r"(?P<num1>\d{1,4})\s*[-–—.]?\s*modda\b"
    r"|(?P<num2>\d{1,4})\s*[-–—.]?\s*модда\b"
    r"|статья\s*(?P<num3>\d{1,4})\b"
    r"|(?P<num4>\d{1,4})\s*[-–—.]?\s*статья\b"
    r")"
    r"[\s.:–—-]*(?P<title>[^\n]{0,160})?"
)

_ARTICLE_INLINE_RE = re.compile(
    r"(?i)\b(?P<num>\d{1,4})\s*[-–—.]?\s*(?:modda|модда|статья)\b"
)

_CHAPTER_INLINE_RE = re.compile(
    r"\b(?P<num>\d{1,3})\s*[-–—.]?\s*(?:bob|бўлим|глава)\b"
    r"|\b(?:bob|глава)\s*(?P<num2>\d{1,3})\b",
    re.IGNORECASE,
)


def _clean_title(t: str) -> str:
    t = (t or "").strip(" \t.:–—-")
    t = re.sub(r"\s+", " ", t)
    return t[:160]


def _roman_to_int(s: str) -> Optional[int]:
    s = (s or "").upper().strip()
    if not s or not re.fullmatch(r"[IVXLC]+", s):
        return None
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    total = 0
    prev = 0
    for ch in reversed(s):
        v = vals.get(ch, 0)
        if v < prev:
            total -= v
        else:
            total += v
            prev = v
    return total or None


def _num_from_match(m: re.Match[str], *groups: str) -> str:
    for g in groups:
        raw = m.groupdict().get(g)
        if not raw:
            continue
        raw = raw.strip()
        if raw.isdigit():
            return str(int(raw))
        ri = _roman_to_int(raw)
        if ri is not None:
            return str(ri)
        return raw
    return ""


@dataclass
class StructureNode:
    kind: str  # chapter | article
    number: str
    title: str
    char_start: int
    char_end: int = -1
    page: Optional[int] = None


@dataclass
class DocumentStructure:
    chapters: list[StructureNode] = field(default_factory=list)
    articles: list[StructureNode] = field(default_factory=list)
    structure_quality: str = "none"  # high | medium | none
    doc_type: str = "generic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "structure_quality": self.structure_quality,
            "doc_type": self.doc_type,
            "chapter_count": len(self.chapters),
            "article_count": len(self.articles),
            "chapters": [
                {
                    "number": c.number,
                    "title": c.title,
                    "char_start": c.char_start,
                }
                for c in self.chapters[:200]
            ],
            "articles_sample": [
                {
                    "number": a.number,
                    "title": a.title,
                    "char_start": a.char_start,
                }
                for a in self.articles[:50]
            ],
        }


def detect_doc_type(text: str, filename: str = "") -> str:
    t = (text or "")[:8000].lower()
    fn = (filename or "").lower()
    if "kodeks" in t or "кодекс" in t or "kodeksi" in fn or "кодекс" in fn:
        return "law"
    if "farmon" in t or "фармон" in t or "указ" in t:
        return "decree"
    if "qaror" in t or "қарор" in t or "постановление" in t:
        return "resolution"
    if "nizom" in t or "низом" in t or "положение" in t:
        return "regulation"
    if "yo'riqnoma" in t or "yoriqnoma" in t or "инструкция" in t:
        return "instruction"
    if "reglament" in t or "регламент" in t:
        return "reglament"
    if "hisobot" in t or "отчет" in t or "report" in t:
        return "report"
    return "generic"


def extract_structure(text: str, *, filename: str = "") -> DocumentStructure:
    """Scan full document text for chapters and articles."""
    text = text or ""
    doc_type = detect_doc_type(text, filename)
    chapters: list[StructureNode] = []
    articles: list[StructureNode] = []
    seen_ch: set[str] = set()
    seen_art: set[str] = set()

    for m in _CHAPTER_RE.finditer(text):
        num = _num_from_match(m, "num1", "num2", "num3", "num4", "num5", "num6", "num7")
        if not num or num in seen_ch:
            continue
        # skip unlikely huge chapter numbers from noise
        try:
            if int(num) > 500:
                continue
        except ValueError:
            pass
        seen_ch.add(num)
        chapters.append(
            StructureNode(
                kind="chapter",
                number=num,
                title=_clean_title(m.group("title") or ""),
                char_start=m.start(),
            )
        )

    for m in _ARTICLE_RE.finditer(text):
        num = _num_from_match(m, "num1", "num2", "num3", "num4")
        if not num or num in seen_art:
            continue
        try:
            if int(num) > 5000:
                continue
        except ValueError:
            pass
        seen_art.add(num)
        articles.append(
            StructureNode(
                kind="article",
                number=num,
                title=_clean_title(m.group("title") or ""),
                char_start=m.start(),
            )
        )

    chapters.sort(key=lambda n: n.char_start)
    articles.sort(key=lambda n: n.char_start)

    # assign char_end
    for i, ch in enumerate(chapters):
        ch.char_end = (
            chapters[i + 1].char_start if i + 1 < len(chapters) else len(text)
        )
    for i, art in enumerate(articles):
        art.char_end = (
            articles[i + 1].char_start if i + 1 < len(articles) else len(text)
        )

    n_ch, n_art = len(chapters), len(articles)
    if n_art >= 5 or (n_ch >= 2 and n_art >= 2):
        quality = "high"
    elif n_ch >= 1 or n_art >= 1:
        quality = "medium"
    else:
        quality = "none"

    return DocumentStructure(
        chapters=chapters,
        articles=articles,
        structure_quality=quality,
        doc_type=doc_type,
    )


def chapter_for_article(
    structure: DocumentStructure, article_num: str
) -> Optional[StructureNode]:
    art = next((a for a in structure.articles if a.number == str(article_num)), None)
    if not art:
        return None
    # last chapter that starts at or before article
    cand = [c for c in structure.chapters if c.char_start <= art.char_start]
    return cand[-1] if cand else None


def build_document_profile(
    *,
    source: str,
    text: str,
    structure: DocumentStructure,
    page_count: int = 0,
) -> dict[str, Any]:
    """Deterministic profile (no LLM). Summary = title-ish head + stats."""
    head = re.sub(r"\s+", " ", (text or "")[:500]).strip()
    title = head[:200] if head else source
    # Prefer first non-empty line looking like a title
    for line in (text or "").splitlines()[:30]:
        line = line.strip()
        if len(line) >= 12 and not re.match(r"^\d+$", line):
            title = line[:200]
            break

    chapter_titles = [
        f"{c.number}-bob" + (f": {c.title}" if c.title else "")
        for c in structure.chapters[:40]
    ]
    summary_parts = [
        f"Hujjat: {source}",
        f"Turi: {structure.doc_type}",
        f"Struktura: {structure.structure_quality}",
        f"Boblar soni: {len(structure.chapters)}",
        f"Moddalar soni: {len(structure.articles)}",
    ]
    if page_count:
        summary_parts.append(f"Sahifalar: {page_count}")
    if chapter_titles:
        summary_parts.append("Boblar: " + "; ".join(chapter_titles[:15]))
    if title:
        summary_parts.append(f"Sarlavha/boshi: {title}")

    profile_text = "\n".join(summary_parts)
    return {
        "doc_id": source,
        "source_file": source,
        "doc_type": structure.doc_type,
        "structure_quality": structure.structure_quality,
        "chapter_count": len(structure.chapters),
        "article_count": len(structure.articles),
        "page_count": page_count,
        "title": title,
        "summary": profile_text,
        "chapters": [
            {"number": c.number, "title": c.title} for c in structure.chapters
        ],
        "profile_text": profile_text,
    }


def build_toc_text(structure: DocumentStructure, source: str) -> str:
    lines = [f"Mundarija / TOC — {source}"]
    if not structure.chapters and not structure.articles:
        lines.append("(struktura topilmadi)")
        return "\n".join(lines)
    for c in structure.chapters:
        lines.append(
            f"Bob {c.number}" + (f": {c.title}" if c.title else "")
        )
    # sample articles under first chapters only in TOC text
    if structure.articles and len(structure.articles) <= 80:
        lines.append("Moddalar:")
        for a in structure.articles:
            lines.append(
                f"  {a.number}-modda" + (f": {a.title}" if a.title else "")
            )
    else:
        lines.append(f"Jami moddalar: {len(structure.articles)}")
    return "\n".join(lines)


def structure_aware_units(
    text: str,
    structure: DocumentStructure,
    *,
    source: str,
    path: str,
    file_type: str,
    page_map: Optional[list[tuple[int, int, int]]] = None,
    max_article_chars: int = 6000,
) -> list[dict[str, Any]]:
    """
    Build index units with rich metadata.

    page_map: list of (char_start, char_end, page_no) optional
    """
    units: list[dict[str, Any]] = []
    if structure.structure_quality == "none" or (
        not structure.articles and not structure.chapters
    ):
        return units

    def page_at(pos: int) -> int:
        if not page_map:
            return -1
        for a, b, p in page_map:
            if a <= pos < b:
                return p
        return page_map[-1][2] if page_map else -1

    # Prefer article-level units when articles exist
    if structure.articles:
        for art in structure.articles:
            body = text[art.char_start : art.char_end].strip()
            if not body:
                continue
            if len(body) > max_article_chars:
                body = body[:max_article_chars]
            ch = chapter_for_article(structure, art.number)
            chapter_num = ch.number if ch else ""
            chapter_title = ch.title if ch else ""
            heading = (
                f"{chapter_num}-bob > {art.number}-modda"
                if chapter_num
                else f"{art.number}-modda"
            )
            if art.title:
                heading += f": {art.title}"
            units.append(
                {
                    "text": body,
                    "source": source,
                    "path": path,
                    "page": page_at(art.char_start),
                    "file_type": file_type,
                    "chunk_kind": "article",
                    "doc_id": source,
                    "article_num": art.number,
                    "article_title": art.title or "",
                    "chapter_num": chapter_num,
                    "chapter_title": chapter_title,
                    "heading_path": heading,
                    "parent_id": f"{source}::ch-{chapter_num}" if chapter_num else "",
                    "structure_quality": structure.structure_quality,
                    "doc_type": structure.doc_type,
                }
            )
    elif structure.chapters:
        for ch in structure.chapters:
            body = text[ch.char_start : ch.char_end].strip()
            if not body:
                continue
            if len(body) > max_article_chars:
                body = body[:max_article_chars]
            heading = f"{ch.number}-bob" + (f": {ch.title}" if ch.title else "")
            units.append(
                {
                    "text": body,
                    "source": source,
                    "path": path,
                    "page": page_at(ch.char_start),
                    "file_type": file_type,
                    "chunk_kind": "chapter",
                    "doc_id": source,
                    "article_num": "",
                    "article_title": "",
                    "chapter_num": ch.number,
                    "chapter_title": ch.title or "",
                    "heading_path": heading,
                    "parent_id": "",
                    "structure_quality": structure.structure_quality,
                    "doc_type": structure.doc_type,
                }
            )
    return units


# ---------------------------------------------------------------------------
# Query routing
# ---------------------------------------------------------------------------


@dataclass
class QueryRoute:
    routes: list[str]
    article_num: str = ""
    chapter_num: str = ""
    wants_counts: bool = False
    wants_profile: bool = False
    wants_toc: bool = False


def route_query(question: str) -> QueryRoute:
    q = (question or "").strip()
    ql = q.lower()
    # fold apostrophes
    for ch in ("'", "'", "ʻ", "ʼ"):
        ql = ql.replace(ch, "")

    routes: list[str] = []
    article_num = ""
    chapter_num = ""
    wants_counts = False
    wants_profile = False
    wants_toc = False

    # counts
    if re.search(
        r"(nechta|qancha|nechi|how many|сколько|қанча|нечта)\s+"
        r".*(bob|modda|бўлим|глава|статья|модда|article|chapter)",
        ql,
    ) or re.search(
        r"(bob|modda|глава|статья|модда).*(nechta|qancha|soni|count|число)",
        ql,
    ):
        wants_counts = True
        routes.append("structured_counts")

    # article N
    m = re.search(
        r"(?:^|\s)(\d{1,4})\s*[-–—.]?\s*(?:modda|модда|статья)\b"
        r"|(?:modda|модда|статья)\s*[-–—.]?\s*(\d{1,4})\b"
        r"|article\s*(\d{1,4})\b",
        q,
        re.IGNORECASE,
    )
    if m:
        article_num = next(g for g in m.groups() if g)
        routes.append("article_lookup")

    # chapter N / which chapter for article
    if re.search(
        r"qaysi\s+bob|which\s+chapter|қайси\s+боб|в какой главе",
        ql,
        re.IGNORECASE,
    ):
        routes.append("hierarchy")
        wants_toc = True
    m2 = re.search(
        r"(?:^|\s)(\d{1,3})\s*[-–—.]?\s*(?:bob|боб|бўлим|глава)\b"
        r"|(?:bob|боб|глава)\s*(\d{1,3})\b",
        q,
        re.IGNORECASE,
    )
    if m2:
        chapter_num = next(g for g in m2.groups() if g)
        routes.append("chapter_lookup")

    # profile / regulates
    if re.search(
        r"(nima\s+tartibga|nimani\s+tartib|umumiy\s+nima|what\s+does|"
        r"regulates|о чём|мазмуни|mavzusi|haqida\s+hujjat)",
        ql,
        re.IGNORECASE,
    ):
        wants_profile = True
        routes.append("doc_profile")

    if re.search(
        r"mundarija|table of contents|\btoc\b|оглавление",
        ql,
        re.IGNORECASE,
    ):
        wants_toc = True
        routes.append("toc")

    if not routes:
        routes.append("semantic")
    elif "semantic" not in routes and not wants_counts:
        # also run semantic for hybrid richness except pure count queries
        if not (len(routes) == 1 and routes[0] == "structured_counts"):
            routes.append("semantic")

    return QueryRoute(
        routes=routes,
        article_num=str(article_num) if article_num else "",
        chapter_num=str(chapter_num) if chapter_num else "",
        wants_counts=wants_counts,
        wants_profile=wants_profile,
        wants_toc=wants_toc,
    )


def format_counts_answer(profiles: list[dict[str, Any]], question: str) -> str:
    if not profiles:
        return "Indekslangan hujjatlar bo‘yicha struktura statistikasi topilmadi."
    ql = (question or "").lower()
    want_art = any(k in ql for k in ("modda", "статья", "article", "модда"))
    want_ch = any(k in ql for k in ("bob", "боб", "глава", "chapter", "бўлим", "булим"))
    if not want_art and not want_ch:
        want_art = want_ch = True

    lines: list[str] = []
    for p in profiles:
        src = p.get("source_file") or p.get("doc_id") or "?"
        arts = int(p.get("article_count") or 0)
        chs = int(p.get("chapter_count") or 0)
        # Skip empty unstructured files when the question is about a real code/law
        if arts == 0 and chs == 0 and p.get("structure_quality") in (None, "none"):
            continue
        bits: list[str] = []
        if want_art:
            bits.append(f"{arts} ta modda")
        if want_ch:
            bits.append(f"{chs} ta bob")
        lines.append(f"{src}: " + ", ".join(bits) + ".")
    if not lines:
        return "Indekslangan hujjatlarda bob/modda soni topilmadi (struktura yo‘q yoki reindex kerak)."
    # Lead with a clear FACT line for the host model to relay verbatim
    return "FACT (struktura hisobi — ixtiyoriy qayta yozmang):\n" + "\n".join(lines)
