from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from turnstile_core.domain.assistant_models import AssistantRouterProposal, ChartSpec

HISTORY_LIMIT = 50
TITLE_LENGTH = 60

ANSWER_LANGUAGE: dict[str, str] = {
    "zh-CN": "Simplified Chinese",
    "zh-TW": "Traditional Chinese",
    "en": "English",
    "ko": "Korean",
    "ja": "Japanese",
}

TITLE_PROMPT = """You name a saved conversation so it can be recognised in a list.

Reply with the title only. No quotation marks, no trailing punctuation, no explanation,
no prefix such as "Title:". Name the subject, not the action: prefer "Department token
cost" over "The user asked about department token cost". Keep it under 6 words, or under
16 characters for Chinese, Japanese and Korean. Write the title in {language}.
"""


def clean_title(raw: str) -> str | None:
    line = raw.strip().splitlines()[0] if raw.strip() else ""
    for prefix in ("title:", "标题:", "标题：", "タイトル:", "제목:"):
        if line.lower().startswith(prefix):
            line = line[len(prefix):]
    line = " ".join(line.split())
    line = line.strip("\"'“”‘’`《》「」【】 ")
    line = line.rstrip("。.!?！？:：,，;；")
    return line[:TITLE_LENGTH] or None


@dataclass(frozen=True)
class ToolOutcome:
    payload: dict[str, Any]
    chart: ChartSpec | None = None
    router_proposal: AssistantRouterProposal | None = None
    summary: str = ""
    row_count: int = 0
