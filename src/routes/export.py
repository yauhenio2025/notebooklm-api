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

    # Build footnotes in viewer.html format with bibliographic data
    footnotes = []
    for cit in sorted(query.citations, key=lambda c: c.citation_number):
        authors = getattr(cit, "source_authors", None) or ""
        date = getattr(cit, "source_date", None) or ""
        source_title = cit.source_title or ""

        # Build formatted citation like "Ihde (2009), Postphenomenology and Technoscience"
        formatted = _format_citation(authors, date, source_title)

        footnotes.append(
            ExportFootnote(
                number=cit.citation_number,
                source_file=source_title,
                quoted_text=cit.cited_text or "",
                context_snippet="",
                aria_label=f"{cit.citation_number}: {formatted or source_title}",
                authors=authors,
                date=date,
                formatted_citation=formatted,
            )
        )

    # Build clean_html from answer text + citation markers
    clean_html = _build_clean_html(query.answer or "", footnotes)

    # Build notebook_sources from unique source titles
    notebook_sources = list(
        {cit.source_title for cit in query.citations if cit.source_title}
    )

    return ExportResponse(
        timestamp=query.asked_at.isoformat() if query.asked_at else "",
        notebook_url="",
        question=query.question,
        response_text=query.answer or "",
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


def _build_clean_html(answer: str, footnotes: list[ExportFootnote]) -> str:
    """Convert plain text answer + footnotes into HTML with citation markers.

    The notebooklm-py answer text typically contains inline citation references
    like [1], [2] etc. We convert these to <sup> tags matching the viewer.html format.
    """
    if not answer:
        return ""

    # Build source lookup
    source_map = {fn.number: fn.source_file for fn in footnotes}

    # Split into paragraphs
    paragraphs = answer.split("\n\n")
    html_parts = []

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # Replace citation references [N] with <sup> tags
        def replace_citation(match):
            num = int(match.group(1))
            source = source_map.get(num, "")
            return (
                f'<sup class="citation" data-num="{num}" '
                f'data-source="{source}" '
                f'title="{num}: {source}">{num}</sup>'
            )

        para_html = re.sub(r"\[(\d+)\]", replace_citation, para)

        # Detect headings (short bold-like lines ending with colon)
        if len(para) < 80 and para.endswith(":"):
            html_parts.append(f"<h3>{para_html}</h3>")
        elif para.startswith("- ") or para.startswith("* "):
            items = re.split(r"\n[-*]\s", para)
            items[0] = items[0].lstrip("- *")
            li_items = "".join(f"<li>{item.strip()}</li>\n" for item in items if item.strip())
            html_parts.append(f"<ul>\n{li_items}</ul>")
        else:
            html_parts.append(f"<p>{para_html}</p>")

    return "\n".join(html_parts)
