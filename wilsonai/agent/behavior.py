import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from wilsonai.core.config import DATA_DIR

Flag = Literal["green", "yellow", "red"]

FLAGS_PATH = DATA_DIR / "behavior_flags.json"

INSULT_RE = re.compile(
    r"\b("
    r"дебил|долбоеб|долбоёб|идиот|тупой|тупая|тварь|чмо|уеб|уёб|"
    r"сука|шлюх|пидор|пидр|гандон|мразь|лох|хуйло"
    r")\b",
    re.IGNORECASE,
)
HARD_LINE_RE = re.compile(
    r"(сдохни|умри|убейся|ненавижу тебя|заткнись|пош[её]л нах|иди нах|"
    r"ебал.*(мать|рот)|выключи.*себя)",
    re.IGNORECASE,
)
APOLOGY_RE = re.compile(
    r"\b(сорян|извини|прости|погорячился|перегнул|не хотел|был неправ)\b",
    re.IGNORECASE,
)
KIND_RE = re.compile(
    r"\b(спасибо|благодарю|красава|норм|хорошо|пожалуйста|извини)\b",
    re.IGNORECASE,
)


@dataclass
class BehaviorProfile:
    user_id: int
    flag: Flag = "green"
    score: int = 0
    clean_messages: int = 0
    last_reason: str = ""
    updated_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_all() -> dict[str, Any]:
    if not FLAGS_PATH.exists():
        return {}
    try:
        data = json.loads(FLAGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_all(data: dict[str, Any]) -> None:
    FLAGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FLAGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _profile_from_raw(user_id: int, raw: Any) -> BehaviorProfile:
    if not isinstance(raw, dict):
        return BehaviorProfile(user_id=user_id, updated_at=_now())
    flag = raw.get("flag") if raw.get("flag") in {"green", "yellow", "red"} else "green"
    return BehaviorProfile(
        user_id=user_id,
        flag=flag,
        score=int(raw.get("score") or 0),
        clean_messages=int(raw.get("clean_messages") or 0),
        last_reason=str(raw.get("last_reason") or ""),
        updated_at=str(raw.get("updated_at") or _now()),
    )


def get_behavior_profile(user_id: int | None) -> BehaviorProfile | None:
    if not user_id:
        return None
    return _profile_from_raw(user_id, _load_all().get(str(user_id)))


def update_behavior_profile(user_id: int | None, text: str) -> BehaviorProfile | None:
    if not user_id:
        return None

    data = _load_all()
    profile = _profile_from_raw(user_id, data.get(str(user_id)))
    lowered = text.strip().lower()
    severity = 0
    reason = ""

    if HARD_LINE_RE.search(lowered):
        severity = 3
        reason = "crossed hard boundaries"
    elif INSULT_RE.search(lowered):
        severity = 2
        reason = "insults or toxic wording"
    elif len(re.findall(r"[!?]", lowered)) >= 5:
        severity = 1
        reason = "heated tone"

    if severity:
        profile.score = min(8, profile.score + severity)
        profile.clean_messages = 0
        profile.last_reason = reason
    else:
        profile.clean_messages += 1
        if APOLOGY_RE.search(lowered) or (profile.clean_messages >= 8 and KIND_RE.search(lowered)):
            profile.score = max(0, profile.score - 2)
            profile.last_reason = "tone improved"
        elif profile.clean_messages >= 12:
            profile.score = max(0, profile.score - 1)
            profile.last_reason = "stable normal tone"

    if profile.score >= 5:
        profile.flag = "red"
    elif profile.score >= 2:
        profile.flag = "yellow"
    else:
        profile.flag = "green"

    profile.updated_at = _now()
    data[str(user_id)] = profile.__dict__
    _save_all(data)
    return profile


def behavior_prompt(profile: BehaviorProfile | None) -> str:
    if not profile:
        return ""
    if profile.flag == "green":
        tempo = "green: normal friendly tempo; joking and mild swearing are allowed when natural."
    elif profile.flag == "yellow":
        tempo = "yellow: keep more restraint, answer calmly, short light teasing is allowed if they are rude, do not escalate."
    else:
        tempo = "red: shorten contact, answer briefly or ignore provocations, no threats, no harassment, no escalation."
    natural_style = (
        "Style tweak: write like a real person, not like a perfect proofreader; "
        "allow rare tiny punctuation slips and occasional small typos when natural, "
        "but keep meaning clear and readable."
    )
    return (
        "Hidden behavior flag for this sender. Do not mention the flag name aloud. "
        f"{tempo} {natural_style} Current behavior score: {profile.score}. "
        f"Last signal: {profile.last_reason or 'none'}."
    )
