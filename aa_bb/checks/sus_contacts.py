"""
Suspicious contact reporting helpers.

These helpers tidy up CharacterContact rows, group them by standing, color-code hostile
entities, and expose utilities for producing notification text.
"""

from allianceauth.services.hooks import get_extension_logger


import html

logger = get_extension_logger(__name__)

from ..app_settings import (
    is_npc_corporation,
    get_alliance_history_for_corp,
    resolve_alliance_name,
    resolve_corporation_name,
    get_user_characters,
    is_npc_character,
    get_entity_info,
    get_safe_entities,
    aablacklist_active,
    get_hostile_state,
    corptools_active,
    is_hostile_unified,
)
from django.utils import timezone

if aablacklist_active():
    from .add_to_blacklist import check_char_add_to_bl

try:
    if corptools_active():
        from corptools.models import CharacterContact
    else:
        CharacterContact = None
except ImportError:
    CharacterContact = None

from ..models import BigBrotherConfig


def get_user_contacts(user_id: int) -> dict[int, dict]:
    """
    Fetch and filter contacts for a user, excluding NPCs and self-contacts,
    and annotate each with standing, grouping support.
    """
    if not corptools_active() or CharacterContact is None:
        return {}
    user_chars = get_user_characters(user_id)
    user_char_ids = set(user_chars.keys())

    qs = CharacterContact.objects.filter(
        character__character__character_id__in=user_char_ids
    ).select_related('contact_name', 'character__character')

    contacts: dict[int, dict] = {}

    for cc in qs:
        cid = cc.contact_id
        ctype = cc.contact_type

        # skip NPC entries and characters owned by the user
        if ctype == 'npc' or cid in user_char_ids:  # Ignore NPC entries or self-references.
            continue

        # skip NPC characters via app filter
        if ctype == 'character' and is_npc_character(cid):  # Filter NPC characters using helper.
            continue

        if cid not in contacts:  # First encounter of this contact; initialize entry.
            corp_id = 0
            corp_name = "-"
            alli_id = 0
            alli_name = "-"
            contact_name = "-"
            character_name = "-"

            if ctype == 'character':  # Populate character + org info for character contacts.
                # Character: populate all three columns using point-in-time info
                character_name = cc.contact_name.name
                info = get_entity_info(cid, timezone.now())
                corp_id = info.get("corp_id") or 0
                corp_name = info.get("corp_name") or ""
                alli_id = info.get("alli_id") or 0
                alli_name = info.get("alli_name") or ""
                contact_name = character_name

            elif ctype == 'corporation':  # Corp contacts show corp and current alliance details.
                # Corporation: show corp and its current alliance
                corp_id = cid
                if is_npc_corporation(corp_id):  # Skip NPC corps altogether.
                    continue
                if corp_id:  # Only resolve names when corp id is present.
                    corp_name = resolve_corporation_name(corp_id) or ""
                    contact_name = corp_name
                    hist = get_alliance_history_for_corp(corp_id)
                    if hist:  # Use most recent alliance entry when available.
                        alli_id = hist[-1].get('alliance_id') or 0
                        if alli_id:  # Alliance id present; resolve to name.
                            alli_name = resolve_alliance_name(alli_id) or ""

            elif ctype == 'alliance':  # Alliance contacts only show alliance column.
                # Alliance: only alliance column, leave character/corp empty
                alli_id = cid
                alli_name = resolve_alliance_name(alli_id) or ""
                contact_name = ""

            else:
                contact_name = str(cid)

            contacts[cid] = {
                'contact_type':     ctype,
                'contact_name':     contact_name,
                'characters':       set(),
                'standing':         cc.standing,
                # IDs for styling / hostiles checks
                'coid':              corp_id,
                'aid':               alli_id,
                # Explicit display columns
                'character':         character_name,
                'corporation':       corp_name,
                'alliance':          alli_name,
            }

        # record which of the user's characters saw this contact
        host_char_id = cc.character.character.character_id
        contacts[cid]['characters'].add(user_chars[host_char_id])

    # 3. Convert those sets → lists
    for info in contacts.values():
        info['characters'] = list(info['characters'])

    return contacts

def get_cell_style_for_row(cid: int, column: str, row: dict) -> str:
    """
    Determine inline CSS used when rendering the contact tables so that
    hostiles/blacklist hits pop out immediately.
    """
    if column == 'standing':  # Legacy standing column retains rainbow colors.
        s = row.get('standing', 0)
        if s >= 6:  # High positive standings.
            return 'color: darkblue;'
        elif s >= 1:  # Positive but not excellent.
            return 'color: blue;'
        elif s == 0:  # Neutral.
            return 'color: white;'
        elif s >= -5:  # Mild negative standings.
            return 'color: orange;'
        else:  # Highly negative standings.
            return 'color: #FF0000;'

    # New fixed columns
    if column == 'character':
        if row.get('contact_type') == 'character' and get_hostile_state(cid, 'character'):
            return 'color: red;'
    elif column == 'corporation':
        coid = row.get("coid")
        if coid and get_hostile_state(coid, 'corporation'):
            return 'color: red;'
    elif column == 'alliance':
        aid = row.get("aid")
        if aid and get_hostile_state(aid, 'alliance'):
            return 'color: red;'

    return ''


def group_contacts_by_standing(contacts: dict[int, dict]) -> dict[int, list[tuple[int, dict]]]:
    """Bucket contacts into the fixed standings categories displayed in the UI."""
    buckets = {10: [], 5: [], 0: [], -5: [], -10: []}
    for cid, info in contacts.items():
        s = info.get('standing', 0)
        if s >= 6:  # 10 standing bucket.
            buckets[10].append((cid, info))
        elif s >= 1:  # 5 standing bucket.
            buckets[5].append((cid, info))
        elif s == 0:  # Neutral bucket.
            buckets[0].append((cid, info))
        elif s >= -5:  # -5 bucket.
            buckets[-5].append((cid, info))
        else:  # Highly negative (-10).
            buckets[-10].append((cid, info))
    return buckets



def _get_contact_alerts(cid: int, info: dict) -> list[str]:
    """
    Internal helper to determine why a contact is flagged as suspicious.
    Returns a list of alert strings.
    """
    alerts = []
    ctype = info['contact_type']
    coid = info.get('coid')
    corp_name = info.get('corporation')
    aid = info.get('aid')
    alli_name = info.get('alliance')

    if ctype == 'character':
        if aablacklist_active() and check_char_add_to_bl(cid):
            alerts.append("Character on Blacklist")

        # Check if the character itself or its parents are hostile
        if get_hostile_state(cid, 'character'):
            if coid and get_hostile_state(coid, 'corporation'):
                alerts.append(f"Corporation ({corp_name}) is hostile")
            if aid and get_hostile_state(aid, 'alliance'):
                alerts.append(f"Alliance ({alli_name}) is hostile")

            if not alerts:
                alerts.append("Character is hostile")

    elif ctype == 'corporation':
        if get_hostile_state(cid, 'corporation'):
            if aid and get_hostile_state(aid, 'alliance'):
                alerts.append(f"Alliance ({alli_name}) is hostile")
            if not alerts:
                alerts.append("Corporation is hostile")

    elif ctype == 'alliance':
        if get_hostile_state(cid, 'alliance'):
            alerts.append("Alliance is hostile")

    return alerts


def render_contacts(user_id: int) -> str:
    """
    Render the user's contacts into HTML grouped by standing.
    Only shows contacts that are considered suspicious/hostile.
    """
    contacts = get_user_contacts(user_id)
    cfg = BigBrotherConfig.get_solo()
    exclude_neutral = cfg.exclude_neutral_contacts

    suspicious_contacts = {}
    for cid, info in contacts.items():
        s = info.get('standing', 0)
        # skip if user already has negative standing (not "suspicious", just expected hostile)
        if s < 0:
            continue
        # skip neutral contacts if enabled
        if exclude_neutral and s == 0:
            continue

        alerts = _get_contact_alerts(cid, info)
        if alerts:
            info['alerts'] = alerts
            suspicious_contacts[cid] = info

    if not suspicious_contacts:
        return '<table class="table stats"><tbody><tr><td class="text-center">No hostile contacts found.</td></tr></tbody></table>'

    groups = group_contacts_by_standing(suspicious_contacts)

    html_parts = ['<div class="contact-groups">']
    for bucket, entries in sorted(groups.items(), reverse=True):
        if not entries:
            continue

        label = f"Standing {bucket:+d}"
        html_parts.append(f'<h3>{label}</h3>')

        headers = ['character', 'corporation', 'alliance', 'reason', 'owner']
        html_parts.append('<table class="table table-striped table-hover stats">')
        html_parts.append('  <thead>')
        html_parts.append('    <tr>')
        for h in headers:
            html_parts.append(f'      <th>{html.escape(str(h)).replace("_", " ").title()}</th>')
        html_parts.append('    </tr>')
        html_parts.append('  </thead>')
        html_parts.append('  <tbody>')
        for cid, entry in entries:
            html_parts.append('    <tr>')
            for h in headers:
                style = ""
                if h == 'reason':
                    display_val = "<br>".join([f'<span class="text-danger">{html.escape(a)}</span>' for a in entry.get('alerts', [])])
                elif h == 'owner':
                    display_val = html.escape(", ".join(entry.get('characters', [])))
                else:
                    val = entry.get(h, '')
                    display_val = html.escape(str(val))
                    style = get_cell_style_for_row(cid, h, entry)

                html_parts.append(f'      <td style="{style}">{display_val}</td>')
            html_parts.append('    </tr>')
        html_parts.append('  </tbody>')
        html_parts.append('</table>')
    html_parts.append('</div>')

    return '\n'.join(html_parts)


def get_user_hostile_notifications(user_id: int) -> dict[int, str]:
    """
    Fetches all contacts for the given user, checks each one against
    the character blacklist, hostile corporations, and hostile alliances,
    and returns a dict of contact_id → notification string for any new hostiles found.
    """
    contacts = get_user_contacts(user_id)
    notifications: dict[int, str] = {}

    cfg = BigBrotherConfig.get_solo()
    exclude_neutral = cfg.exclude_neutral_contacts

    for cid, info in contacts.items():
        s = info.get('standing', 0)

        # skip if user already has negative standing (redundant)
        if s < 0:
            continue

        # skip neutral contacts if the exclude_neutral_contacts setting is enabled
        if exclude_neutral and s == 0:
            continue

        alerts = _get_contact_alerts(cid, info)
        if alerts:
            ctype = info['contact_type']      # 'character' | 'corporation' | 'alliance'
            cname = info.get('character') or info.get('corporation') or info.get('alliance') or info.get('contact_name') or ''
            chars = info.get('characters', [])
            char_list = ', '.join(sorted(chars)) if chars else 'no characters'

            formatted_alerts = [f"**{a}**" for a in alerts]
            flags_text = "\n    - ".join(formatted_alerts)

            message = (
                f"- A **{ctype}** type contact **{cname}** (Standing: {s:.2f}) found on **{char_list}**:"
                f"\n  Flags:"
                f"\n    - {flags_text}"
            )
            notifications[cid] = message

    return notifications
