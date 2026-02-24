"""Export endpoints - bot.py-compatible JSON format for viewer.html."""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.schemas import ExportFootnote, ExportResponse
from src.services.query_service import get_query

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/notebooks/{notebook_id}/queries/{query_id}/export",
    response_model=ExportResponse,
)
async def api_export_query(
    notebook_id: str,
    query_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Export a query response in bot.py-compatible JSON format.

    This format is compatible with the existing viewer.html for rendering
    NotebookLM responses with citations.
    """
    query = await get_query(db, query_id)
    if not query or query.notebook_id != notebook_id:
        raise HTTPException(status_code=404, detail="Query not found")

    # Build full footnote lookup by citation number
    all_footnotes: dict[int, ExportFootnote] = {}
    for cit in sorted(query.citations, key=lambda c: c.citation_number):
        # Keep first (or best) footnote per citation number
        if cit.citation_number in all_footnotes:
            continue
        authors = getattr(cit, "source_authors", None) or ""
        date = getattr(cit, "source_date", None) or ""
        source_title = cit.source_title or ""
        formatted = _format_citation(authors, date, source_title)

        all_footnotes[cit.citation_number] = ExportFootnote(
            number=cit.citation_number,
            source_file=source_title,
            quoted_text=cit.cited_text or "",
            context_snippet="",
            aria_label=f"{cit.citation_number}: {formatted or source_title}",
            authors=authors,
            date=date,
            formatted_citation=formatted,
        )

    # Find which citation numbers are actually referenced in the answer text
    answer_text = query.answer or ""
    referenced_nums = _extract_referenced_citation_numbers(answer_text)

    # Filter footnotes to only those referenced in the text
    footnotes = [
        all_footnotes[n] for n in sorted(referenced_nums) if n in all_footnotes
    ]

    # Build clean_html from answer text + citation markers
    clean_html = _build_clean_html(answer_text, all_footnotes)

    # Build notebook_sources from unique source titles
    notebook_sources = list(
        {cit.source_title for cit in query.citations if cit.source_title}
    )

    return ExportResponse(
        timestamp=query.asked_at.isoformat() if query.asked_at else "",
        notebook_url="",
        question=query.question,
        response_text=answer_text,
        clean_html=clean_html,
        footnotes=[f.model_dump() for f in footnotes],
        notebook_sources=sorted(notebook_sources),
        model="notebooklm",
    )


def _format_citation(authors: str, date: str, title: str) -> str:
    """Format a bibliographic citation like 'Ihde (2009), Postphenomenology and Technoscience'."""
    # Extract last name of first author
    short_name = ""
    if authors:
        first_author = authors.split(",")[0].strip()
        name_parts = first_author.split()
        short_name = name_parts[-1] if name_parts else first_author

    # Extract year
    year = ""
    if date:
        y = date.strip()[:4]
        if y.isdigit():
            year = y

    # Build: "Author (Year), Title" or subsets
    prefix = ""
    if short_name and year:
        prefix = f"{short_name} ({year})"
    elif short_name:
        prefix = short_name
    elif year:
        prefix = f"({year})"

    if prefix and title:
        return f"{prefix}, {title}"
    return prefix or title or ""


def _extract_referenced_citation_numbers(text: str) -> set[int]:
    """Extract all citation numbers referenced in the answer text.

    Handles: [1], [1, 2], [4-6], [1, 3-5], [12-14]
    """
    nums = set()
    # Match all bracket groups: [anything with digits, commas, hyphens]
    for bracket_match in re.finditer(r"\[(\d[\d,\s\-]*)\]", text):
        inner = bracket_match.group(1)
        for part in inner.split(","):
            part = part.strip()
            range_match = re.match(r"(\d+)\s*-\s*(\d+)", part)
            if range_match:
                lo, hi = int(range_match.group(1)), int(range_match.group(2))
                nums.update(range(lo, hi + 1))
            elif part.isdigit():
                nums.add(int(part))
    return nums


def _make_sup(num: int, source_map: dict[int, str]) -> str:
    """Build a single <sup> citation tag."""
    source = source_map.get(num, "")
    return (
        f'<sup class="citation" data-num="{num}" '
        f'data-source="{_html_esc(source)}" '
        f'title="{num}: {_html_esc(source)}">{num}</sup>'
    )


def _html_esc(s: str) -> str:
    """Escape HTML special characters in attribute values."""
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _replace_citations(text: str, source_map: dict[int, str]) -> str:
    """Replace all citation bracket patterns with <sup> tags.

    Handles [1], [1, 2], [4-6], [1, 3-5, 8] etc.
    """
    def _replace_bracket(match):
        inner = match.group(1)
        sups = []
        for part in inner.split(","):
            part = part.strip()
            range_match = re.match(r"(\d+)\s*-\s*(\d+)", part)
            if range_match:
                lo, hi = int(range_match.group(1)), int(range_match.group(2))
                sups.append(", ".join(_make_sup(n, source_map) for n in range(lo, hi + 1)))
            elif part.isdigit():
                sups.append(_make_sup(int(part), source_map))
            else:
                sups.append(part)
        return ", ".join(sups)

    return re.sub(r"\[(\d[\d,\s\-]*)\]", _replace_bracket, text)


def _apply_markdown(text: str) -> str:
    """Convert basic markdown formatting to HTML.

    Handles **bold**, *italic*, and bold-italic combinations.
    """
    # Bold-italic ***text*** or ___text___
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", text)
    # Bold **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # Italic *text* (but not inside HTML tags)
    text = re.sub(r"(?<![<\w])\*([^*]+?)\*(?![>\w])", r"<em>\1</em>", text)
    return text


def _build_clean_html(answer: str, footnote_map: dict[int, ExportFootnote]) -> str:
    """Convert plain text answer into HTML with citation markers and formatting.

    Handles citation patterns: [1], [1, 2], [4-6], [1, 3-5, 8]
    Handles markdown: **bold**, *italic*
    """
    if not answer:
        return ""

    source_map = {num: fn.source_file for num, fn in footnote_map.items()}

    paragraphs = answer.split("\n\n")
    html_parts = []

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # Apply markdown formatting first, then citation replacement
        para_html = _apply_markdown(para)
        para_html = _replace_citations(para_html, source_map)

        # Detect headings (short bold-like lines ending with colon)
        if len(para) < 100 and para.rstrip().endswith(":"):
            html_parts.append(f"<h3>{para_html}</h3>")
        elif para.startswith("- ") or para.startswith("* "):
            items = re.split(r"\n[-*]\s", para)
            items[0] = re.sub(r"^[-*]\s*", "", items[0])
            li_html = []
            for item in items:
                item = item.strip()
                if not item:
                    continue
                item_html = _apply_markdown(item)
                item_html = _replace_citations(item_html, source_map)
                li_html.append(f"<li>{item_html}</li>\n")
            html_parts.append(f"<ul>\n{''.join(li_html)}</ul>")
        else:
            html_parts.append(f"<p>{para_html}</p>")

    return "\n".join(html_parts)
