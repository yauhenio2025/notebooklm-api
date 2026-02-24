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

    # Build footnotes in viewer.html format
    footnotes = []
    for cit in sorted(query.citations, key=lambda c: c.citation_number):
        footnotes.append(
            ExportFootnote(
                number=cit.citation_number,
                source_file=cit.source_title or "",
                quoted_text=cit.cited_text or "",
                context_snippet="",
                aria_label=f"{cit.citation_number}: {cit.source_title or ''}",
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
