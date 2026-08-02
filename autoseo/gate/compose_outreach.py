"""Turn outreach targets into pitches worth sending.

The pitch that fails is "please add our app". The one that works gives the author a reason their
readers benefit — a gap in their own list, with checkable evidence. So each draft names the
competitors that page already covers, states the specific gap DailyVox fills, and offers proof the
author can verify in under a minute rather than asking them to take our word for it.

No LLM here, deliberately. The facts are fixed (on-device, no account, MIT, "Data Not Collected"),
the structure is the same every time, and a template produces a better pitch than a model that might
invent a feature we do not have. Generation earns its place where judgement is needed; this is not
that place.
"""

from __future__ import annotations

from autoseo.decide.outreach import Target
from autoseo.gate.queue import Item

PROOF = (
    "- Works in airplane mode — the whole app runs on-device, so it is checkable in 30 seconds\n"
    "- App Store privacy label reads \"Data Not Collected\"\n"
    "- MIT licensed, source at github.com/intrepidkarthi/dailyvox — the privacy claim is auditable\n"
    "- Free, no account, no subscription, iPhone only"
)


def draft(target: Target) -> Item:
    named = ", ".join(target.competitors_named[:4]) or "the usual options"
    gap = (
        "every option on it sends entries to a server. There is no entry for readers who want "
        "journaling that never leaves the device"
    )

    subject = "A gap in your journaling app roundup" if target.title else f"Addition for {target.domain}"
    body = f"""Subject: {subject}

Hi,

Your piece covers {named} — useful list, and I ended up on it while researching what
people actually recommend for private journaling.

One gap: {gap}. That is a real segment (the people asking
"does this app read my entries?") and right now your list has nothing to point them at.

DailyVox fills it:
{PROOF}

If it is useful I will send a short, sourced comparison row in your existing format — feature,
price, privacy label, and one honest limitation (iPhone only, no web or Android version). No
obligation to include it.

{target.url}

Thanks,
Karthik
getdailyvox.com"""

    rationale = (
        f"{target.domain} was cited {target.citation_count}x by the answer engine across "
        f"{len(set(target.questions))} buyer question(s) and names "
        f"{len(target.competitors_named)} competitor(s) but not DailyVox. "
        f"Ahrefs' 75k-brand study puts branded web mentions at 0.664 correlation with AI Overview "
        f"presence vs 0.218 for backlinks — being on this page is worth more than a blog post."
    )

    return Item(
        kind="outreach",
        channel="manual",
        title=f"Pitch: {target.domain}",
        body=body,
        rationale=rationale,
        meta={"url": target.url, "domain": target.domain, "score": target.score},
    )


def queue_top(targets: list[Target], limit: int = 3) -> list[Item]:
    """Only the top few. A queue of forty pitches gets ignored; three get sent."""
    return [draft(t) for t in targets[:limit] if not t.we_are_listed]
