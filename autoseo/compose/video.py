"""Write a 35-50 second narration script for a Short.

A Short is not a blog post read aloud. The first sentence has to earn the next three seconds or the
viewer is gone, and the whole thing has to work with the sound off — most Shorts autoplay muted, so
the captions carry it.

The strongest asset here is a claim that can be checked in thirty seconds: the app works in airplane
mode. That is provable on camera and no competitor can copy it, so scripts lead with the
demonstrable thing rather than a benefit statement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from autoseo.compose import llm
from autoseo.compose.blog import BRAND
from autoseo.core.log import get_logger
from autoseo.quality import gate

log = get_logger(__name__)

MAX_ATTEMPTS = 2

PROMPT = """{brand}

Write a narration script for a 40-second vertical video (YouTube Short / Instagram Reel) about:
{topic}

Rules:
- 90-120 words. Read aloud that is roughly 40 seconds.
- First sentence is a concrete, checkable claim or a specific problem. No throat-clearing, no
  "in today's world", no rhetorical question.
- Short sentences. Each one becomes a caption line, so it must be readable in a glance.
- Plain spoken English. No hashtags, no emoji, no markdown, no stage directions, no speaker labels.
- One honest limitation somewhere in the middle. It buys more trust than another benefit.
- End on a statement, not a call to action and not an inspirational line.
- Do not invent statistics, studies or reviews.

Return ONLY the narration text. Nothing else."""


@dataclass
class VideoScript:
    topic: str
    script: str
    title: str
    description: str
    search_terms: str

    @property
    def words(self) -> int:
        return len(self.script.split())


def _clean(raw: str) -> str:
    text = re.sub(r"^```\w*\s*|\s*```$", "", raw.strip())
    text = re.sub(r"^(?:narration|script|voiceover)\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.M)      # stray bullets
    text = re.sub(r"[*_#]", "", text)                        # stray markdown
    return " ".join(text.split())


def write(topic: str, search_terms: str = "journal writing calm morning",
          tier: llm.Tier = llm.Tier.FREE) -> VideoScript | None:
    """Draft and gate a narration script. Two attempts, then give up rather than loop."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        script = _clean(llm.complete(PROMPT.format(brand=BRAND, topic=topic), tier=tier))

        # The blog length floor does not apply to narration; `social` context skips it.
        verdict = gate.evaluate(script, context="social", check_duplication=False)
        words = len(script.split())
        log.info("attempt %d: %d words, %s", attempt, words, verdict.summary()[:70])

        if verdict.passed and 70 <= words <= 150:
            title = topic if len(topic) <= 90 else topic[:87] + "..."
            return VideoScript(
                topic=topic, script=script,
                title=f"{title} #Shorts",
                description=(f"{script[:280]}\n\nDailyVox is a free, open-source voice journal for "
                             f"iPhone. Everything runs on-device.\nhttps://getdailyvox.com\n\n"
                             f"#Shorts #journaling #privacy"),
                search_terms=search_terms,
            )
        if not verdict.passed:
            log.warning("  rejected: %s", "; ".join(verdict.reasons[:2]))
        else:
            log.warning("  wrong length: %d words (want 70-150)", words)

    return None
