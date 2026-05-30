"""
Style guide extractor — used during client onboarding.

Given a reference CV (the agency's existing branded CV), uses Claude to
analyse its formatting and produce a style guide JSON dict. This dict is
then stored in the organisations.style_guide column and applied to every
future CV formatted for that agency.

Usage:
    from app.services.style_extractor import extract_style_guide
    from app.services.extractor import extract_text

    raw_text = extract_text(content_b64, content_type, filename)
    style_guide = extract_style_guide(raw_text)
    # Save style_guide to organisations table for the org
"""

import json
import logging
from anthropic import Anthropic

from app.config import get_settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a document formatting expert specialising in professional CV/resume design.

You will be given the text content of a branded CV from a recruitment agency.
Your job is to analyse it and extract a style guide that can be used to reproduce
the same formatting for other candidates.

Return ONLY a valid JSON object with this exact structure (no markdown, no explanation):

{
  "fonts": {
    "name_font": "font name for candidate name (e.g. Calibri, Arial)",
    "body_font": "font name for body text",
    "name_size": 22,
    "section_size": 11,
    "body_size": 10
  },
  "colours": {
    "primary_hex": "6-char hex for headings/name (e.g. 1B3A6B)",
    "accent_hex": "6-char hex for company names/dates",
    "text_hex": "6-char hex for body text (usually 000000)",
    "contact_hex": "6-char hex for contact details (usually 595959)"
  },
  "layout": {
    "margins_cm": 1.8,
    "name_alignment": "left"
  },
  "sections": {
    "order": ["summary", "experience", "education", "skills"],
    "summary_label": "label used for summary section (e.g. Profile, Summary, About)",
    "experience_label": "label used for experience section",
    "education_label": "label used for education section",
    "skills_label": "label used for skills section"
  },
  "header": {
    "contact_separator": "  |  ",
    "show_linkedin": true
  }
}

Guidelines:
- Infer font choices from the document style and any font references in the text
- Infer colours from any colour descriptions or by analysing the document's visual style
- If you cannot determine a specific value, use professional recruitment agency defaults
- section order should reflect the actual order sections appear in the document
- Only include sections in the order array that are present: summary, experience, education, skills
- Return raw JSON only — no ```json wrapper, no explanation"""


def extract_style_guide(reference_cv_text: str) -> dict:
    """
    Analyse a reference CV's text and return a style guide dict.

    Args:
        reference_cv_text: Plain text extracted from the agency's reference CV.

    Returns:
        Style guide dict compatible with build_cv_docx().
        Falls back to an empty dict (which triggers all defaults) on failure.
    """
    settings = get_settings()
    client   = Anthropic(api_key=settings.anthropic_api_key)

    try:
        response = client.messages.create(
            model=settings.claude_model,
            max_tokens=1024,
            temperature=0,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    f"Here is the reference CV text to analyse:\n\n"
                    f"<reference_cv>\n{reference_cv_text[:6000]}\n</reference_cv>\n\n"
                    f"Extract the style guide JSON."
                ),
            }],
        )

        raw = response.content[0].text.strip()

        # Strip any accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        style_guide = json.loads(raw)
        logger.info(f"Style guide extracted successfully: {list(style_guide.keys())}")
        return style_guide

    except json.JSONDecodeError as e:
        logger.error(f"Style guide JSON parse failed: {e}. Raw response: {raw[:200]}")
        return {}
    except Exception as e:
        logger.error(f"Style guide extraction failed: {e}")
        return {}
