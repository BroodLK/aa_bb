"""
Determine whether each of a user's characters is currently Alpha or Omega.

This module centralizes the heuristics for deciding a character's state so
the same logic can be reused both in HTML renderings and in background
tasks that persist the findings.
"""

from allianceauth.services.hooks import get_extension_logger


from aa_bb.models import CharacterAccountState, BigBrotherConfig
from aa_bb.app_settings import resolve_character_name, get_user_characters
from django.db import transaction
from django.utils.html import format_html, mark_safe
from django.utils import timezone
import json
import os
from datetime import timedelta, time

try:
    from corptools.models import CharacterAudit, Skill
except Exception:
    CharacterAudit = None
    Skill = None

logger = get_extension_logger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


_fallback_skill_ids_cache = None

def _load_fallback_skill_ids():
    """
    Load skills.json and return a sorted list of all skill IDs contained.
    """
    global _fallback_skill_ids_cache
    if _fallback_skill_ids_cache is not None:
        return _fallback_skill_ids_cache

    skills_json_file = os.path.join(BASE_DIR, "skills.json")
    try:
        with open(skills_json_file, "r") as f:
            skills_by_cat = json.load(f)
    except Exception:
        logger.exception("Failed to load skills.json fallback list.")
        return []

    ids = set()
    for _cat, blocks in skills_by_cat.items():
        for block in blocks:
            for k in block.keys():
                if k == "Category ID":
                    continue
                try:
                    ids.add(int(k))
                except Exception:
                    continue

    _fallback_skill_ids_cache = sorted(ids)
    return _fallback_skill_ids_cache

def in_utc_update_window(now, start_time, end_time):
    utc_time = now.time()
    if start_time < end_time:
        return start_time <= utc_time < end_time
    else:  # Window crosses midnight
        return utc_time >= start_time or utc_time < end_time


_alpha_skills_cache = None
_skills_cache = None

def _get_alpha_skills():
    global _alpha_skills_cache
    if _alpha_skills_cache is None:
        alpha_skills_file = os.path.join(BASE_DIR, "alpha_skills.json")
        try:
            with open(alpha_skills_file, "r") as f:
                _alpha_skills_cache = json.load(f)
        except Exception:
            logger.exception("Failed to load alpha_skills.json")
            return []
    return _alpha_skills_cache

def _get_skills():
    global _skills_cache
    if _skills_cache is None:
        skills_json_file = os.path.join(BASE_DIR, "skills.json")
        try:
            with open(skills_json_file, "r") as f:
                _skills_cache = json.load(f)
        except Exception:
            logger.exception("Failed to load skills.json")
            return {}
    return _skills_cache


def determine_character_state(user_id, save: bool = False, cfg: BigBrotherConfig = None):
    """
    Inspect every owned character's skill levels and infer Alpha/Omega status.
    """
    if cfg is None:
        cfg = BigBrotherConfig.get_solo()
    max_cache_age = timedelta(hours=cfg.update_cache_ttl_hours)
    window_start = cfg.update_maintenance_window_start
    window_end = cfg.update_maintenance_window_end

    alpha_skills = _get_alpha_skills()
    alpha_caps = {skill["id"]: skill["cap"] for skill in alpha_skills}
    alpha_skill_ids = [skill["id"] for skill in alpha_skills]

    # skills = _get_skills() # Loaded but seemingly unused in this function currently

    all_char_ids_map = get_user_characters(user_id)  # iterates keys if dict
    char_ids = list(all_char_ids_map.keys())

    char_db_records = {
        rec.char_id: rec for rec in CharacterAccountState.objects.filter(char_id__in=char_ids)
    }

    result = {}

    # If corptools is not available, default to unknown status.
    if CharacterAudit is None or Skill is None:
        for char_id in char_ids:
            db_record = char_db_records.get(char_id)
            result[char_id] = {
                "state": (db_record.state if db_record else "unknown") or "unknown",
                "skill_used": (db_record.skill_used if db_record else None),
                "last_state": (db_record.state if db_record else None),
            }
        return result

    audits = (
        CharacterAudit.objects
        .filter(character__character_id__in=char_ids)
        .select_related("skilltotals")
        .only("id", "character__character_id", "skilltotals__total_sp")
    )
    total_sp_by_char = {}
    for a in audits:
        cid = a.character.character_id
        try:
            total_sp_by_char[cid] = a.skilltotals.total_sp
        except Exception:
            total_sp_by_char[cid] = None

    chars_to_check = []
    for char_id in char_ids:
        db_record = char_db_records.get(char_id)
        if cfg.clone_state_always_recheck and db_record and db_record.skill_used:
            chars_to_check.append(char_id)
            continue
        # Use cached state if we're outside the update window, or if it's within TTL inside the window.
        now = timezone.now()
        is_in_window = in_utc_update_window(now, window_start, window_end)
        use_cache = False
        if db_record and db_record.last_checked_at:
            if not is_in_window:
                use_cache = True
            elif (now - db_record.last_checked_at) < max_cache_age:
                use_cache = True

        if use_cache:
            result[char_id] = {
                "state": db_record.state if db_record.state else "unknown",
                "skill_used": db_record.skill_used,
                "last_state": db_record.state,
            }
            continue
        else:
            chars_to_check.append(char_id)

    # Nothing to do
    if not chars_to_check:
        return result

    extra_skill_ids = set()
    for char_id in chars_to_check:
        db_record = char_db_records.get(char_id)
        if db_record and db_record.skill_used:
            extra_skill_ids.add(int(db_record.skill_used))

    fallback_skill_ids = _load_fallback_skill_ids()
    skill_ids_to_fetch = sorted(set(alpha_skill_ids) | extra_skill_ids | set(fallback_skill_ids))

    skill_rows = (
        Skill.objects
        .filter(character__character__character_id__in=chars_to_check, skill_id__in=skill_ids_to_fetch)
        .values(
            "character__character__character_id",
            "skill_id",
            "trained_skill_level",
            "active_skill_level",
        )
    )

    per_char = {cid: {} for cid in chars_to_check}
    for row in skill_rows:
        cid = row["character__character__character_id"]
        sid = int(row["skill_id"])
        per_char[cid][sid] = {
            "trained": int(row["trained_skill_level"]),
            "active": int(row["active_skill_level"]),
        }

    for char_id in chars_to_check:
        db_record = char_db_records.get(char_id)
        total_sp = total_sp_by_char.get(char_id)

        state = None
        skill_used = None

        # 1) Re-check cached skill_used first
        if db_record and db_record.skill_used:
            sid = int(db_record.skill_used)
            levels = per_char.get(char_id, {}).get(sid, {"trained": 0, "active": 0})
            trained = levels["trained"]
            active = levels["active"]
            cap = alpha_caps.get(sid, 0)

            if active > cap:
                state = "omega"
                skill_used = sid
            elif trained > active:
                state = "alpha"
                skill_used = sid

        # 2) Check alpha-locked skills
        if state is None:
            for sid in alpha_skill_ids:
                levels = per_char.get(char_id, {}).get(sid, {"trained": 0, "active": 0})
                trained = levels["trained"]
                active = levels["active"]
                cap = alpha_caps.get(sid, 0)

                if active > cap:
                    state = "omega"
                    skill_used = sid
                    break
                elif trained > active:
                    state = "alpha"
                    skill_used = sid
                    break

        # 3) Fallback: if still unknown, check skills.json list
        if state is None and fallback_skill_ids:
            # Already fetched in bulk above
            for sid in fallback_skill_ids:
                levels = per_char.get(char_id, {}).get(sid, {"trained": 0, "active": 0})
                trained = levels["trained"]
                active = levels["active"]
                cap = alpha_caps.get(sid, 0)

                if active > cap:
                    state = "omega"
                    skill_used = sid
                    break
                elif trained > active:
                    state = "alpha"
                    skill_used = sid
                    break

        if state is None:
            state = "unknown"
            skill_used = None

        last_state = db_record.state if db_record else None
        result[char_id] = {
            "state": state,
            "skill_used": skill_used,
            "last_state": last_state,
        }

        if save:
            now = timezone.now()
            with transaction.atomic():
                CharacterAccountState.objects.update_or_create(
                    char_id=char_id,
                    defaults={
                        "state": state,
                        "skill_used": skill_used,
                        "last_total_sp": total_sp,
                        "last_checked_at": now,
                    },
                )

    return result


def render_character_states_html(user_id: int) -> str:
    """
    Returns an HTML snippet showing, for each of the user's characters:
      - the current state (alpha/omega/unknown)
    as a single table with columns Character | State
    """
    data = determine_character_state(user_id)

    html = """
    <table class="table table-striped table-hover stats">
      <thead>
        <tr>
          <th>Character</th>
          <th>State</th>
        </tr>
      </thead>
      <tbody>
    """

    for char_id, info in data.items():
        char_name = resolve_character_name(char_id)

        state_val = info.get("state", "unknown")
        if state_val == "omega":
            state_val_html = mark_safe('<span class="text-success">Omega</span>')
        elif state_val == "alpha":
            state_val_html = mark_safe('<span class="text-danger">Alpha</span>')
        else:
            state_val_html = "Unknown"

        html += format_html("<tr><td>{}</td><td>{}</td></tr>", char_name, state_val_html)

    html += "</tbody></table>"
    return format_html(html)
