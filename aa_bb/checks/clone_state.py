"""
Determine whether each of a user's characters is currently Alpha or Omega.

This module centralizes the heuristics for deciding a character's state so
the same logic can be reused both in HTML renderings and in background
tasks that persist the findings.
"""

from aa_bb.models import CharacterAccountState
from aa_bb.app_settings import resolve_character_name, get_user_characters
from django.db import transaction
from django.utils.html import format_html, mark_safe
from django.utils import timezone
import json
import os
import logging

try:
    from corptools.models import CharacterAudit, Skill
except Exception:
    CharacterAudit = None
    Skill = None

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def determine_character_state(user_id, save: bool = False):
    """
    Inspect every owned character's skill levels and infer Alpha/Omega status.
    """
    alpha_skills_file = os.path.join(BASE_DIR, "alpha_skills.json")

    # Load alpha skill caps
    with open(alpha_skills_file, "r") as f:
        alpha_skills = json.load(f)
    alpha_caps = {skill["id"]: skill["cap"] for skill in alpha_skills}
    alpha_skill_ids = [skill["id"] for skill in alpha_skills]

    char_db_records = {
        rec.char_id: rec for rec in CharacterAccountState.objects.all()
    }

    all_char_ids = get_user_characters(user_id)  # iterates keys if dict
    result = {}

    # If corptools isn't available, mark unknown quickly
    if CharacterAudit is None or Skill is None:
        for char_id in all_char_ids:
            db_record = char_db_records.get(char_id)
            result[char_id] = {
                "state": (db_record.state if db_record else "unknown") or "unknown",
                "skill_used": (db_record.skill_used if db_record else None),
                "last_state": (db_record.state if db_record else None),
            }
        return result

    char_ids = list(all_char_ids)

    # Build total_sp map (FAST: select_related only, no skill prefetch)
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

    # Decide which characters need checking (SP gate)
    chars_to_check = []
    for char_id in char_ids:
        db_record = char_db_records.get(char_id)
        total_sp = total_sp_by_char.get(char_id)

        # If we have a previous SP and it's unchanged, reuse cached state
        if db_record and total_sp is not None and db_record.last_total_sp is not None:
            if int(db_record.last_total_sp) == int(total_sp) and db_record.state in ("alpha", "omega", "unknown"):
                result[char_id] = {
                    "state": db_record.state,
                    "skill_used": db_record.skill_used,
                    "last_state": db_record.state,
                }
                continue

        chars_to_check.append(char_id)

    # Nothing to do
    if not chars_to_check:
        return result

    # Build the minimal set of skill IDs to fetch:
    #   - all alpha-locked skills
    #   - plus any cached skill_used for chars needing check (so we keep the fast shortcut idea)
    extra_skill_ids = set()
    for char_id in chars_to_check:
        db_record = char_db_records.get(char_id)
        if db_record and db_record.skill_used:
            extra_skill_ids.add(int(db_record.skill_used))

    skill_ids_to_fetch = sorted(set(alpha_skill_ids) | extra_skill_ids)

    # Single query: pull only the relevant Skill rows for just these characters
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

    now = timezone.now()

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
            cap = alpha_caps.get(sid, 5)

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
                cap = alpha_caps.get(sid, 5)

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
            with transaction.atomic():
                CharacterAccountState.objects.update_or_create(
                    char_id=char_id,
                    defaults={
                        "state": state,
                        "skill_used": skill_used,
                        "last_total_sp": total_sp,
                        "sp_last_checked": now,
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
