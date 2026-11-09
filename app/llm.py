import os
import json
import re
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field


class ExtractedGameData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    studio_name: str | None
    game_title: str | None
    contact_email: str | None
    game_page_url: str | None
    materials_url: str | None


class TriageAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal["billing", "support", "complaint", "other"]
    business_type: Literal["incoming_offer", "reply_to_outreach", "request_for_information", "other"]
    draft_reply: str = Field(min_length=1, max_length=2000)
    confidence: Literal["high", "medium", "low"]
    escalate: bool
    extracted_data: ExtractedGameData
    translation_ru: str = Field(min_length=1, max_length=4000)


def analyze_message(text: str) -> TriageAnalysis:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("llm_not_configured")

    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv("OPENAI_BASE_URL") or None,
        timeout=60.0,
        max_retries=1,
    )
    schema = json.dumps(TriageAnalysis.model_json_schema(), ensure_ascii=False)
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-6-luna"),
        response_format={"type": "json_object"},
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": (
                    "You triage messages received by a game localization partnership team. "
                    "Return only a JSON object matching this JSON Schema: " + schema + "\n"
                    "Use exactly one required category: billing, support, complaint, other. "
                    "Most game partnership proposals are other. Also set business_type to exactly one of "
                    "incoming_offer (new game offered for localization), reply_to_outreach (reply to our "
                    "outgoing contact), request_for_information (request for details or terms), or other. "
                    "Extract studio/game/contact/URLs only "
                    "when explicitly present; otherwise use null. Copy studio and game names exactly "
                    "as written in the source; do not translate or transliterate them. Never infer "
                    "facts, metrics, or deal terms. If information is insufficient, confidence must be "
                    "low and escalate must be true. Write a concise draft reply in the sender's language, "
                    "1 to 6 sentences. Do not claim a message or email was sent. Translate the full "
                    "incoming message to natural Russian in translation_ru, but preserve company names, "
                    "game titles, brand names, email addresses, and URLs exactly as in the source."
                ),
            },
            {"role": "user", "content": text},
        ],
    )
    raw = response.choices[0].message.content or ""
    if not raw.strip():
        raise ValueError("empty_model_output")
    analysis = TriageAnalysis.model_validate(json.loads(raw))
    without_urls = re.sub(r"https?://\S+", "", analysis.draft_reply)
    sentence_marks = re.findall(r"[.!?。！？]+", without_urls)
    sentence_count = len(sentence_marks) if sentence_marks else int(bool(without_urls.strip()))
    if not 1 <= sentence_count <= 6:
        raise ValueError("draft_reply_must_have_1_to_6_sentences")
    if not any("\u0400" <= char <= "\u04ff" for char in analysis.translation_ru):
        raise ValueError("translation_ru_is_not_russian")
    for name in (analysis.extracted_data.studio_name, analysis.extracted_data.game_title):
        if name and name not in text:
            raise ValueError("extracted_name_not_present_in_original")
        if name and name not in analysis.translation_ru:
            raise ValueError("original_name_missing_from_russian_translation")
    return analysis
