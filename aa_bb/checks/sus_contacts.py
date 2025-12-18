"""
Suspicious contact reporting helpers.

These helpers tidy up CharacterContact rows, group them by standing, color-code hostile
entities, and expose utilities for producing notification text.
"""

import html
import logging

logger = logging.getLogger(__name__)

from ..app_settings import (
    is_npc_corporation,
    get_alliance_history_for_corp,
    resolve_alliance_name,
    resolve_corporation_name,
    get_user_characters,
    is_npc_character,
    get_entity_info,
    aablacklist_active
)
from django.utils import timezone

if aablacklist_active():
    from .corp_blacklist import check_char_corp_bl

try:
    from corptools.models import CharacterContact
except ImportError:
    logger.error("Corptools not installed, corp checks will not work.")

from ..models import BigBrotherConfig


def get_user_contacts(user_id: int) -> dict[int, dict]:
    """
    Fetch and filter contacts for a user, excluding NPCs and self-contacts,
    and annotate each with standing, grouping support.
    """
    # 1. Build a dict of the user's characters { character_id: character_name }
    user_chars = get_user_characters(user_id)
    user_char_ids = set(user_chars.keys())

    # 2. Pull in all CharacterContact rows for those character IDs
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
    # standing color (not shown in the 3-col table but kept for compatibility)
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
    if aablacklist_active():
        if column == 'character':  # Character column only highlights hostile chars.
            if row.get('contact_type') == 'character' and check_char_corp_bl(cid):  # Highlight hostile character contacts.
                return 'color: red;'
            return ''

    if column == 'corporation':  # Corp column highlights hostile corporations.
        coid = row.get("coid")
        if coid and str(coid) in BigBrotherConfig.get_solo().hostile_corporations:  # Highlight hostile corps.
            return 'color: red;'
        return ''

    if column == 'alliance':  # Alliance column highlights hostile alliances.
        aid = row.get("aid")
        if aid and str(aid) in BigBrotherConfig.get_solo().hostile_alliances:  # Highlight hostile alliances.
            return 'color: red;'
        return ''

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



def render_contacts(user_id: int) -> str:
    """
    Render the user's contacts into HTML grouped by standing.
    """
    contacts = get_user_contacts(user_id)
    groups = group_contacts_by_standing(contacts)

    if not contacts:  # No contact records available; show placeholder.
        return '<p>No contacts found.</p>'

    html_parts = ['<div class="contact-groups">']
    for bucket, entries in sorted(groups.items(), reverse=True):
        label = f"Standing {bucket:+d}"
        html_parts.append(f'<h3>{label}</h3>')
        if not entries:  # No contacts in this category.
            html_parts.append('<p>No contacts in this category.</p>')
            continue

        # Fixed 3-column table per requirements
        headers = ['character', 'corporation', 'alliance']
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
                val = entry.get(h, '')
                display_val = ', '.join(map(str, val)) if isinstance(val, list) else val  # Join multiple character names.
                style = get_cell_style_for_row(cid, h, entry)
                html_parts.append(f'      <td style="{style}">{html.escape(str(display_val))}</td>')
            html_parts.append('    </tr>')
        html_parts.append('  </tbody>')
        html_parts.append('</table>')
    html_parts.append('</div>')

    return '\n'.join(html_parts)

import logging
logger = logging.getLogger(__name__)

def get_user_hostile_notifications(user_id: int) -> dict[int, str]:
    """
    Fetches all contacts for the given user, checks each one against
    the character blacklist, hostile corporations, and hostile alliances,
    and returns a dict of contact_id → notification string for any new hostiles found.
    """
    contacts = get_user_contacts(user_id)
    notifications: dict[int, str] = {}

    # Shortcut to the current hostile lists
    cfg = BigBrotherConfig.get_solo()
    hostile_corps = cfg.hostile_corporations
    hostile_allis = cfg.hostile_alliances
    logger.info(f"{hostile_allis}")

    for cid, info in contacts.items():
        ctype     = info['contact_type']      # 'character' | 'corporation' | 'alliance'
        # derive a display name based on the type
        if ctype == 'character':  # Display actual character name for hostile characters.
            cname = info.get('character') or ''
        elif ctype == 'corporation':  # Corporations show corp display name.
            cname = info.get('corporation') or ''
        elif ctype == 'alliance':  # Alliances show alliance display name.
            cname = info.get('alliance') or ''
        else:
            cname = info.get('contact_name') or ''
        chars     = info.get('characters', set())
        coid      = info.get('coid')          # corporation ID (int or None)
        corp_name = info.get('corporation')
        aid       = info.get('aid')           # alliance ID (int or None)
        alli_name = info.get('alliance')
        s         = info.get('standing', 0)

        alerts: list[str] = []
        logger.info(f"{cname},{aid},{alli_name}")

        # 1) Character-specific blacklist check
        if aablacklist_active():
            if ctype == 'character' and check_char_corp_bl(cid):  # Character is on blacklist.
                alerts.append(f"**{cname}** is on blacklist")

        # 2) Hostile corporation check (characters & corporations)
        if ctype in ('character', 'corporation') and coid != 0:  # Evaluate corp affiliation when present.
            if str(coid) in hostile_corps:  # Corp matches hostile list.
                alerts.append(f"corporation **{corp_name}** is on hostile list")

        # 3) Hostile alliance check (characters, corporations & alliances)
        if aid != 0 and str(aid) in hostile_allis:  # Alliance belongs to hostile list.
            alerts.append(f"alliance **{alli_name}** is on hostile list")

        if alerts:  # Build notification only when this contact triggered alerts.
            char_list = ', '.join(sorted(chars)) if chars else 'no characters'
            # Build a single notification string
            message = (
                f"- A {s} **{ctype}** type contact **{cname}** found on **{char_list}**, flags: "
                + "; ".join(alerts)
            )
            notifications[cid] = message

    return notifications
