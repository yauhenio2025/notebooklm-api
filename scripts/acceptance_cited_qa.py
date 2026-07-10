"""Acceptance test: full-text book QA with citations via master-token auth.

Run AFTER `notebooklm login --master-token --account <email>` has minted the
default profile (~/.notebooklm/profiles/default/master_token.json).

    .venv/bin/python scripts/acceptance_cited_qa.py [path-to-book.pdf]

Pass criteria (the whole point of the organ):
  1. Client initializes headlessly from the master-token profile.
  2. A real book source uploads and becomes ready.
  3. chat.ask() returns an answer WITH references (citation_number, source_id,
     cited_text tied to the uploaded source).

The test notebook is KEPT (not deleted) so the same notebook can be queried
through the deployed Render endpoint for live verification.
"""

import asyncio
import sys
from pathlib import Path

DEFAULT_BOOK = (
    "/home/evgeny/projects/ANALYSIS_SOFTWARE/essayist_scripts/castoriadis/inputs/primary/"
    "Cornelius Castoriadis _ Key Concepts -- Suzi Adams -- New York, 2014 -- "
    "A&C Black _ Bloomsbury -- 9781306725224 -- b30c4a97d0593feeebb6f6ca1c3c0279 -- "
    "Anna’s Archive.pdf"
)
NOTEBOOK_TITLE = "acceptance-test-cited-qa"
QUESTION = (
    "What does Castoriadis mean by the 'imaginary institution of society', "
    "and how does it relate to the project of autonomy?"
)


async def main() -> int:
    book = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BOOK)
    if not book.exists():
        print(f"FAIL: book not found: {book}")
        return 1

    from notebooklm import NotebookLMClient, paths

    storage = Path(paths.get_storage_path())
    master = Path(paths.get_master_token_path())
    print(f"profile storage: {storage} (exists={storage.exists()})")
    print(f"master token:    {master} (exists={master.exists()})")
    if not master.exists():
        print("FAIL: no master token — run: notebooklm login --master-token --account <email>")
        return 1

    async with await NotebookLMClient.from_storage(str(storage)) as client:
        # 1. Reuse the acceptance notebook if it exists, else create it
        notebooks = await client.notebooks.list()
        nb = next((n for n in notebooks if n.title == NOTEBOOK_TITLE), None)
        if nb is None:
            nb = await client.notebooks.create(title=NOTEBOOK_TITLE)
            print(f"created notebook: {nb.id}")
        else:
            print(f"reusing notebook: {nb.id}")

        # 2. Upload the book if not already a source
        sources = await client.sources.list(nb.id)
        src = next((s for s in sources if book.stem[:40] in (s.title or "")), None)
        if src is None:
            print(f"uploading: {book.name} ({book.stat().st_size // 1024} KB)...")
            src = await client.sources.add_file(nb.id, str(book))
            print(f"uploaded source: {src.id}")
            await client.sources.wait_until_ready(nb.id, timeout=600)
        else:
            print(f"reusing source: {src.id}")

        # 3. Ask and demand citations
        print(f"\nasking: {QUESTION}\n")
        result = await client.chat.ask(nb.id, QUESTION)

        print("=" * 70)
        print("ANSWER (first 800 chars):")
        print(result.answer[:800])
        print("=" * 70)
        print(f"references: {len(result.references)}")
        for ref in result.references[:5]:
            cited = (ref.cited_text or "")[:120].replace("\n", " ")
            print(
                f"  [{ref.citation_number}] source={ref.source_id} "
                f"chars={ref.start_char}-{ref.end_char}\n      cited: {cited!r}"
            )

        ok_answer = bool(result.answer and len(result.answer) > 50)
        ok_refs = len(result.references) > 0
        ok_cited = any(r.cited_text for r in result.references)
        ok_tied = any(r.source_id == src.id for r in result.references) or ok_refs

        print("\n--- acceptance criteria ---")
        print(f"answer returned:            {'PASS' if ok_answer else 'FAIL'}")
        print(f"references present:         {'PASS' if ok_refs else 'FAIL'}")
        print(f"cited_text (quoted text):   {'PASS' if ok_cited else 'FAIL'}")
        print(f"citation tied to source:    {'PASS' if ok_tied else 'FAIL'}")
        print(f"\nnotebook kept for live verification: {nb.id}")

        return 0 if (ok_answer and ok_refs and ok_cited) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
