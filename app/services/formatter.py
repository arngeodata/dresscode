"""
DOCX template stamping.
Downloads the agency's brand template from Supabase Storage,
replaces {{PLACEHOLDER}} markers with parsed CV data,
and returns a formatted DOCX as bytes.

Template placeholders:
    {{CANDIDATE_NAME}}      - Full name
    {{CANDIDATE_EMAIL}}     - Email address
    {{CANDIDATE_PHONE}}     - Phone number
    {{CANDIDATE_LOCATION}}  - Location
    {{CANDIDATE_LINKEDIN}}  - LinkedIn URL
    {{SUMMARY}}             - Professional summary

For repeating blocks (experience, education), the template should contain
marker rows/paragraphs with {{EXPERIENCE_BLOCK}} and {{EDUCATION_BLOCK}}.
These are replaced with dynamically generated content.
"""

import io
import copy
import logging
from app.models import ParsedCV, ExperienceEntry, EducationEntry

logger = logging.getLogger(__name__)


def stamp_template(template_bytes: bytes, cv: ParsedCV) -> bytes:
    """
    Stamp parsed CV data into the brand DOCX template.

    Args:
        template_bytes: Raw bytes of the brand template DOCX
        cv: Parsed CV data from Claude

    Returns:
        Formatted DOCX as bytes, ready for email attachment
    """
    from docx import Document
    from docx.shared import Pt
    from docx.oxml.ns import qn

    doc = Document(io.BytesIO(template_bytes))

    # Build simple replacement map for scalar fields
    replacements = {
        "{{CANDIDATE_NAME}}":     cv.candidate.full_name or "",
        "{{CANDIDATE_EMAIL}}":    cv.candidate.email or "",
        "{{CANDIDATE_PHONE}}":    cv.candidate.phone or "",
        "{{CANDIDATE_LOCATION}}": cv.candidate.location or "",
        "{{CANDIDATE_LINKEDIN}}": cv.candidate.linkedin or "",
        "{{SUMMARY}}":            cv.summary or "",
    }

    # ── Replace scalar placeholders in all paragraphs ─────────────────────────
    for para in doc.paragraphs:
        _replace_in_paragraph(para, replacements)

    # ── Replace scalar placeholders inside table cells ─────────────────────────
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    _replace_in_paragraph(para, replacements)

    # ── Handle experience block ────────────────────────────────────────────────
    _replace_block(doc, "{{EXPERIENCE_BLOCK}}", cv.experience, _render_experience_entry)

    # ── Handle education block ─────────────────────────────────────────────────
    _replace_block(doc, "{{EDUCATION_BLOCK}}", cv.education, _render_education_entry)

    # ── Handle skills block ────────────────────────────────────────────────────
    _replace_skills(doc, cv.skills)

    # ── Save to buffer ────────────────────────────────────────────────────────
    output = io.BytesIO()
    doc.save(output)
    output.seek(0)
    return output.read()


def _replace_in_paragraph(para, replacements: dict):
    """Replace placeholder text in a paragraph, preserving runs where possible."""
    full_text = "".join(run.text for run in para.runs)

    for placeholder, value in replacements.items():
        if placeholder in full_text:
            full_text = full_text.replace(placeholder, value)
            # Clear all runs and put the replaced text in the first run
            if para.runs:
                para.runs[0].text = full_text
                for run in para.runs[1:]:
                    run.text = ""
            break  # One placeholder per paragraph expected


def _replace_block(doc, marker: str, entries: list, renderer):
    """
    Find the paragraph containing `marker`, insert rendered entries before it,
    then remove the marker paragraph.
    """
    from docx.oxml import OxmlElement

    marker_para = None
    for para in doc.paragraphs:
        if marker in para.text:
            marker_para = para
            break

    if marker_para is None:
        logger.debug(f"Marker {marker} not found in template — skipping block")
        return

    # Insert rendered entry paragraphs before the marker
    for entry in reversed(entries):
        rendered_paras = renderer(entry)
        for new_para in reversed(rendered_paras):
            marker_para._element.addprevious(new_para._element)

    # Remove the marker paragraph
    marker_para._element.getparent().remove(marker_para._element)


def _render_experience_entry(entry: ExperienceEntry):
    """Create a list of Paragraph objects for one experience entry."""
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    temp_doc = Document()
    paras = []

    # Job title + company
    p = temp_doc.add_paragraph()
    r = p.add_run(f"{entry.title or 'Role'}")
    r.bold = True
    r.font.size = Pt(11)
    if entry.company:
        r2 = p.add_run(f"  |  {entry.company}")
        r2.font.size = Pt(11)
    paras.append(p)

    # Dates
    if entry.start_date or entry.end_date:
        p2 = temp_doc.add_paragraph()
        date_str = f"{entry.start_date or ''} – {entry.end_date or 'Present'}".strip(" –")
        r3 = p2.add_run(date_str)
        r3.italic = True
        r3.font.size = Pt(10)
        paras.append(p2)

    # Responsibilities
    for resp in entry.responsibilities:
        p3 = temp_doc.add_paragraph(style="List Bullet")
        r4 = p3.add_run(resp)
        r4.font.size = Pt(10)
        paras.append(p3)

    # Spacer
    paras.append(temp_doc.add_paragraph())
    return paras


def _render_education_entry(entry: EducationEntry):
    """Create a list of Paragraph objects for one education entry."""
    from docx import Document
    from docx.shared import Pt

    temp_doc = Document()
    paras = []

    p = temp_doc.add_paragraph()
    r = p.add_run(entry.qualification or "Qualification")
    r.bold = True
    r.font.size = Pt(10)
    if entry.institution:
        r2 = p.add_run(f", {entry.institution}")
        r2.font.size = Pt(10)
    if entry.year:
        r3 = p.add_run(f" ({entry.year})")
        r3.font.size = Pt(10)
    paras.append(p)
    return paras


def _replace_skills(doc, skills: list[str]):
    """Replace {{SKILLS_LIST}} marker with a comma-separated skills string."""
    skills_str = ", ".join(skills) if skills else ""
    for para in doc.paragraphs:
        if "{{SKILLS_LIST}}" in para.text:
            _replace_in_paragraph(para, {"{{SKILLS_LIST}}": skills_str})
            return

    # Also check tables
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    if "{{SKILLS_LIST}}" in para.text:
                        _replace_in_paragraph(para, {"{{SKILLS_LIST}}": skills_str})
                        return
