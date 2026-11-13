"""Optional AI veto. It can only BLOCK a trade the deterministic rules already approved; it can never
create trades, size them or change risk. Disabled unless AI_VETO_ENABLED=true."""
import json
import logging

from app.config import settings

log = logging.getLogger(__name__)


def veto(candidate: dict) -> tuple[bool, str]:
    """Return (vetoed, note). Fails closed (veto) on any error when enabled."""
    if not settings.ai_veto_enabled:
        return False, "AI veto disabled"
    try:
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key, timeout=20)
        resp = client.chat.completions.create(
            model=settings.openai_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content":
                    "You review a proposed long spot crypto trade produced by a rule-based system. "
                    "Reply JSON {\"decision\": \"ALLOW\"|\"VETO\", \"note\": string}. VETO only for a concrete "
                    "reason visible in the data (e.g. extreme candle, broken data)."},
                {"role": "user", "content": json.dumps(candidate, default=str)},
            ],
        )
        out = json.loads(resp.choices[0].message.content)
        decision = out.get("decision")
        if decision not in ("ALLOW", "VETO"):
            return True, "invalid AI response"
        return decision == "VETO", str(out.get("note", ""))[:500]
    except Exception as exc:  # noqa: BLE001
        log.warning("AI veto unavailable, blocking entry: %s", exc)
        return True, f"AI unavailable: {exc}"
