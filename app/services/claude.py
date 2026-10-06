"""
Claude API integration for CV parsing.
Sends raw CV text, receives structured JSON.
"""

import json
import logging
from app.config import get_settings
from app.models import ParsedCV

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a CV parsing assistant. Extract all information from the CV text provided and return it as a single valid JSON object.

Rules:
- Return ONLY valid JSON. No markdown, no commentary, no code fences.
- Use null for any field not present in the CV.
- Preserve the original wording of job descriptions and achievements exactly — do not summarise or embellish.
- Standardise all date formats to "Month YYYY" (e.g. "March 2022"). Use "Present" for current roles.
- Capitalisation: if full_name, job titles, or company/employer names appear in ALL CAPS or are otherwise mis-cased, convert them to natural Title Case, with minor words (of, and, the, for, to, in, etc.) in lower case unless first. PRESERVE genuine acronyms and standard brand capitalisation (e.g. IBM, KPMG, NHS, IKEA, PwC, BBC). Leave already-correctly-cased text unchanged, and keep responsibilities/achievements wording verbatim.
- If multiple phone numbers exist, use the first mobile number.
- credentials: extract any post-nominal letters or professional designations that appear after the candidate's name (e.g. "FRICS", "MSc MRICS", "CFA", "PhD"). Do not include these in full_name. If none are present, use null.
- LISTS LAID OUT IN COLUMNS — READ THEM DOWN THE COLUMNS, NOT ACROSS THE LINE. The CV text you are given preserves the page layout, so a list printed in two or three columns arrives with a whole ROW on one line, wide runs of spaces between the columns, and any cell too long for its column wrapping onto the next line underneath its own column. For example:
      ● Environmental            ● Stakeholder            ● Written and oral
        management                 engagement               communication
  That is THREE skills — "Environmental management", "Stakeholder engagement" and "Written and oral communication" — and each one is a separate item. It is not one skill, and it is not six. Tell-tale signs of a column layout: more than one bullet marker on a single line, or two or more consecutive spaces between items. A run of two or more spaces is a COLUMN BREAK and is never part of the text itself. Rejoin each cell with the indented text sitting underneath it in the same column. NEVER return a whole row, or a whole block, as a single string.
- Skills: preserve the original grouping exactly. If skills appear under category labels (e.g. "Property: x, y, z" or "Software: a, b, c"), keep each group as a single string including its label. If skills are already listed as individual items, keep them as individual items.
- Education: ALWAYS capture both start_date and end_date whenever the entry shows a date range in any form (e.g. "2007 - 2010", "Sep 2018 – Jun 2021", "2018 to 2021", "2019-22") — put the earlier date in start_date and the later in end_date, using the same "Month YYYY" (or just-year) formatting the source uses. Only if the entry shows a single date (one graduation/award year) leave start_date null and put that value in end_date (and year).
- Education dates, OPEN-ENDED ranges: if the range has no closing date because the study is still in progress — "2025 - current", "2023 – present", "2024 onwards", "Sept 2025 to date", "expected 2027" — put the opening date in start_date and the literal word "Present" in end_date. Do NOT look for a closing date anywhere else.
- Education dates, NEVER BORROW: a date belongs to the entry it is printed on and to no other. If an entry has no end date of its own, leave end_date null (or "Present" per the rule above). Never take a date from the entry above or below to fill a gap, even when every other entry has two dates. Put any additional information listed under an education entry — modules, achievements, grades detail, dissertation, activities — as separate strings in details. Leave details empty if none.
- Languages — PUT THEM IN ONE PLACE, THE PLACE THE CANDIDATE PUT THEM. The languages array is ONLY for a CV that gives languages their own dedicated section or heading. If the languages are listed inside the skills section instead (e.g. a skills list ending "Languages: French (B2), Spanish", or a "Skills & Interests" block that mentions them), keep them inside skills as ONE MORE SEPARATE ITEM in the skills array and return an EMPTY languages array. Never return the same languages in both skills and languages. This rule decides only WHERE the languages go — it must not change how the other skills are split up. Every skill that would otherwise be its own item stays its own item.
- Any section that is not summary/profile, experience/career history, education, skills, or languages goes into extra_sections. Capture the section title exactly as it appears, and each paragraph or bullet point as a separate item in the items array.

Return this exact structure:
{
  "candidate": {
    "full_name": string | null,
    "credentials": string | null,
    "email": string | null,
    "phone": string | null,
    "location": string | null,
    "linkedin": string | null
  },
  "summary": string | null,
  "experience": [
    {
      "title": string | null,
      "company": string | null,
      "start_date": string | null,
      "end_date": string | null,
      "responsibilities": [string]
    }
  ],
  "education": [
    {
      "institution": string | null,
      "qualification": string | null,
      "start_date": string | null,
      "end_date": string | null,
      "year": string | null,
      "details": [string]
    }
  ],
  "skills": [string],
  "languages": [string],
  "extra_sections": [
    {
      "title": string,
      "items": [string]
    }
  ]
}"""


def _extract_json(content: str) -> dict:
    """
    Pull the JSON object out of a model response.

    The system prompt asks for bare JSON, but the model sometimes wraps it in a
    code fence, prefixes it with a sentence, or appends a note after the closing
    brace. A plain json.loads() survives none of these — trailing text raises
    "Extra data", which is what failed the Stephanie So CV on 14 Sep 2026.

    Raises:
        json.JSONDecodeError: if no JSON object can be read from the response.
    """
    text = content.strip()

    # Fenced block: take what is inside the first fence.
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 2:
            block = parts[1].lstrip()
            if block[:4].lower() == "json":
                block = block[4:]
            text = block.strip()

    # Drop any prose before the object.
    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("No JSON object found in response", text or "", 0)
    text = text[start:]

    # raw_decode reads the first complete value and ignores anything after it,
    # so a trailing note no longer fails the parse.
    obj, _ = json.JSONDecoder().raw_decode(text)

    if not isinstance(obj, dict):
        raise json.JSONDecodeError("Response was not a JSON object", text, 0)

    return obj


def parse_cv(raw_text: str) -> tuple[ParsedCV, int, int]:
    """
    Parse raw CV text using the Claude Haiku API.

    Returns:
        Tuple of (ParsedCV, input_tokens, output_tokens)

    Raises:
        ValueError: If Claude returns invalid JSON after retries
        RuntimeError: If the Anthropic API is unavailable
    """
    import anthropic

    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    # Guard against a runaway attachment, NOT against ordinary long CVs.
    #
    # This was 15,000 chars, set "to avoid exceeding max output tokens". That
    # reasoning did not hold. claude_max_tokens is 64,000 and a CV produces
    # roughly one output token per 4.4 chars of input, so the output cap is not
    # reached until about 280,000 input chars. The old limit was ~19x tighter
    # than its own stated purpose required, and it silently truncated real CVs:
    # Steve Cousins (15,092 chars, 6 Oct 2026) lost one qualification outright
    # and had another clipped mid-line, and the client was sent that CV with no
    # warning to anyone. Do not lower this again without redoing that sum.
    #
    # 100,000 chars is ~6-7x the longest CV seen, yields ~23,000 output tokens
    # (about a third of the cap), and still stops someone emailing a 400-page
    # PDF by mistake.
    max_input_chars = 100_000
    if len(raw_text) > max_input_chars:
        # ERROR, not warning. Truncation means the formatted CV we are about to
        # send is incomplete. That must be loud enough to find in the logs
        # without knowing to look for it.
        logger.error(
            f"CV TEXT TRUNCATED: {len(raw_text)} chars cut to {max_input_chars}. "
            f"{len(raw_text) - max_input_chars} chars discarded. The formatted CV "
            f"will be missing content from the end of the source document."
        )
        raw_text = raw_text[:max_input_chars]

    last_error = None

    for attempt in range(2):  # 2 attempts max
        try:
            response = client.messages.create(
                model=settings.claude_model,
                max_tokens=settings.claude_max_tokens,
                temperature=0,
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Parse this CV and return structured JSON:\n\n{raw_text}"
                            if attempt == 0
                            else f"The previous response was not valid JSON. Parse this CV and return ONLY valid JSON with no other text:\n\n{raw_text}"
                        ),
                    }
                ],
            )

            content = response.content[0].text.strip()
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens

            parsed_dict = _extract_json(content)
            parsed_cv = ParsedCV(**parsed_dict)

            logger.info(
                f"CV parsed successfully. Tokens: {input_tokens} in / {output_tokens} out"
            )
            return parsed_cv, input_tokens, output_tokens

        except json.JSONDecodeError as e:
            last_error = e
            logger.warning(f"Claude returned invalid JSON on attempt {attempt + 1}: {e}")
            if attempt == 1:
                raise ValueError(
                    f"Claude returned invalid JSON after 2 attempts: {e}"
                ) from e

        except anthropic.APIStatusError as e:
            logger.error(f"Anthropic API error: {e.status_code} - {e.message}")
            raise RuntimeError(f"Anthropic API unavailable: {e.status_code}") from e

        except anthropic.APIConnectionError as e:
            logger.error(f"Anthropic connection error: {e}")
            raise RuntimeError("Could not connect to Anthropic API") from e
