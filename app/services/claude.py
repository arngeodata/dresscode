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
- If multiple phone numbers exist, use the first mobile number.
- Skills: preserve the original grouping exactly. If skills appear under category labels (e.g. "Property: x, y, z" or "Software: a, b, c"), keep each group as a single string including its label. If skills are already listed as individual items, keep them as individual items.
- Any section that is not summary/profile, experience/career history, education, skills, or languages goes into extra_sections. Capture the section title exactly as it appears, and each paragraph or bullet point as a separate item in the items array.

Return this exact structure:
{
  "candidate": {
    "full_name": string | null,
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
      "year": string | null
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

    # Guard against very long CVs that would exceed max output tokens
    max_input_chars = 15_000
    if len(raw_text) > max_input_chars:
        logger.warning(f"CV text truncated from {len(raw_text)} to {max_input_chars} chars before parsing")
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

            # Strip markdown code fences if Claude included them despite instructions
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            parsed_dict = json.loads(content)
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
