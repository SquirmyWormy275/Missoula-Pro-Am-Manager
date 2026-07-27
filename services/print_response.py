"""
WeasyPrint-optional print response helper.

Three routes now return either a PDF (if WeasyPrint is installed) or a
plain-HTML fallback (the default on Railway, where cairo/pango/gdk-pixbuf
are too heavy to bundle):
    - routes/scoring.py::judge_sheet_for_event and judge_sheets_all
    - routes/scoring.py heat_sheet_pdf
    - routes/scheduling/birling.py::birling_print_blank (PR D, new)

This module owns the single try/except WeasyPrint pattern so all three
call sites use the same response shape and content-type fallback.
"""

from __future__ import annotations


def weasyprint_or_html(html: str, filename: str) -> tuple:
    """Return a Flask-compatible (body, status, headers) tuple.

    If WeasyPrint is importable, returns a PDF with
    Content-Disposition: attachment; filename=<filename>.pdf.
    Otherwise returns the HTML body with Content-Type text/html so the
    user can print via their browser (Ctrl-P → Save as PDF).

    Args:
        html: Rendered HTML body (from render_template of a standalone
            print template with inline CSS and @page rules).
        filename: Base filename WITHOUT extension.  '.pdf' is appended
            for the PDF path; no extension is added to the HTML fallback
            because the browser renders it inline.
    """
    try:
        from weasyprint import HTML as WP_HTML  # type: ignore

        pdf_bytes = WP_HTML(string=html).write_pdf()
        return (
            pdf_bytes,
            200,
            {
                "Content-Type": "application/pdf",
                "Content-Disposition": f'attachment; filename="{filename}.pdf"',
            },
        )
    except ImportError:
        return html, 200, {"Content-Type": "text/html"}
