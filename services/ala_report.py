"""
ALA (American Lumberjack Association) membership report helpers.

Queries pro competitors for a tournament and returns sorted attendee lists
with normalized membership status for the admin ALA report.
"""

import os
import tempfile
from datetime import datetime

from models.competitor import ProCompetitor


def _normalize_ala_status(value):
    """Map any truthy value to 'Member', falsy/null to 'Non-Member'.

    Handles boolean, string ('yes', 'true', '1'), and None.
    """
    if value is None:
        return "Non-Member"
    if isinstance(value, bool):
        return "Member" if value else "Non-Member"
    if isinstance(value, str):
        return (
            "Member" if value.strip().lower() in ("true", "yes", "1") else "Non-Member"
        )
    return "Member" if value else "Non-Member"


def build_ala_report(tournament):
    """Build ALA membership report data for a tournament.

    Returns dict with keys:
        all_attendees: list of dicts sorted alphabetically
        non_members: filtered list where ala_status == 'Non-Member'
        tournament: the tournament object
        generated_at: datetime string
        year: tournament year
    """
    competitors = ProCompetitor.query.filter_by(
        tournament_id=tournament.id,
        status="active",
    ).all()

    attendees = []
    for c in competitors:
        attendees.append(
            {
                "name": c.name,
                "ala_status": _normalize_ala_status(c.is_ala_member),
            }
        )

    attendees.sort(key=lambda a: _sort_key_from_name(a["name"]))

    non_members = [a for a in attendees if a["ala_status"] == "Non-Member"]

    return {
        "all_attendees": attendees,
        "non_members": non_members,
        "tournament": tournament,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "year": tournament.year,
    }


def _sort_key_from_name(name):
    """Sort key from a name string — last name then first name."""
    parts = name.strip().rsplit(" ", 1)
    if len(parts) == 2:
        first, last = parts[0], parts[1]
    else:
        first, last = "", parts[0]
    return (last.lower(), first.lower())


def generate_ala_pdf(report_data):
    """Generate a two-page ALA membership PDF using reportlab.

    Returns the path to a temporary PDF file.  Caller is responsible for
    cleanup after sending the file.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
    from reportlab.lib.styles import getSampleStyleSheet

    fd, path = tempfile.mkstemp(prefix="ala_report_", suffix=".pdf")
    os.close(fd)

    doc = SimpleDocTemplate(
        path,
        pagesize=letter,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()
    elements = []

    year = report_data["year"]
    generated = report_data["generated_at"]

    # --- Page 1: All Attendees ---
    elements.append(
        Paragraph(
            f"Missoula Pro-Am {year} - ALA Membership Status - All Attendees",
            styles["Heading1"],
        )
    )
    elements.append(Paragraph(f"Generated: {generated}", styles["Normal"]))
    elements.append(Spacer(1, 0.25 * inch))

    all_data = [["#", "Competitor Name", "ALA Membership Status"]]
    for i, a in enumerate(report_data["all_attendees"], 1):
        all_data.append([str(i), a["name"], a["ala_status"]])

    if len(all_data) == 1:
        all_data.append(["", "No competitors found", ""])

    table = Table(all_data, colWidths=[0.5 * inch, 4 * inch, 2.5 * inch])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.black),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.Color(0.95, 0.95, 0.95)],
                ),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    elements.append(table)

    # Force page break
    from reportlab.platypus import PageBreak

    elements.append(PageBreak())

    # --- Page 2: Non-Members ---
    elements.append(
        Paragraph(
            f"Missoula Pro-Am {year} - Non-ALA Members",
            styles["Heading1"],
        )
    )
    elements.append(Paragraph(f"Generated: {generated}", styles["Normal"]))
    elements.append(Spacer(1, 0.25 * inch))

    non_data = [["#", "Competitor Name", "ALA Membership Status"]]
    for i, a in enumerate(report_data["non_members"], 1):
        non_data.append([str(i), a["name"], a["ala_status"]])

    if len(non_data) == 1:
        non_data.append(["", "All competitors are ALA members", ""])

    table2 = Table(non_data, colWidths=[0.5 * inch, 4 * inch, 2.5 * inch])
    table2.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.black),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.Color(0.95, 0.95, 0.95)],
                ),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    elements.append(table2)

    doc.build(elements)
    return path
