"""
Mail intelligence helpers.

These helpers normalize MailMessage rows, detect suspicious senders or
recipients, and persist short notes for repeated reporting.
"""

import html
import logging
from typing import Dict, Optional, List
from datetime import datetime
from django.utils import timezone

from ..app_settings import (
    get_user_characters,
    get_entity_info,
    aablacklist_active,
)

if aablacklist_active():
    from .corp_blacklist import check_char_corp_bl
else:
    def check_char_corp_bl(_cid: int) -> bool:
        return False

from ..models import BigBrotherConfig, ProcessedMail, SusMailNote

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

try:
    from corptools.models import MailMessage, MailRecipient
except ImportError:
    logger.error("Corptools not installed, corp checks will not work.")


def _find_employment_at(employment: List[dict], date: datetime) -> Optional[dict]:
    for rec in employment:
        start = rec.get("start_date")
        end = rec.get("end_date")
        if start and start <= date and (end is None or date < end):
            return rec
    return None


def _find_alliance_at(history: List[dict], date: datetime) -> Optional[int]:
    for i, rec in enumerate(history):
        start = rec.get("start_date")
        next_start = history[i + 1]["start_date"] if i + 1 < len(history) else None
        if start and start <= date and (next_start is None or date < next_start):
            return rec.get("alliance_id")
    return None


def gather_user_mails(user_id: int):
    user_chars = get_user_characters(user_id)
    user_ids = set(user_chars.keys())
    return MailMessage.objects.filter(
        recipients__recipient_id__in=user_ids
    ).prefetch_related("recipients", "recipients__recipient_name")


def get_user_mails(qs) -> Dict[int, Dict]:
    result: Dict[int, Dict] = {}
    _info_cache: dict[tuple[int, int], dict] = {}

    def _cached_info(eid: int, when: datetime) -> dict:
        key = (int(eid or 0), int(when.date().toordinal()))
        if key in _info_cache:
            return _info_cache[key]
        info = get_entity_info(eid, when)
        _info_cache[key] = info
        return info

    for m in qs:
        mid = m.id_key
        sent = m.timestamp
        timeee = getattr(m, "timestamp", timezone.now())

        sender_id = m.from_id
        sinfo = _cached_info(sender_id, timeee)

        recipient_names = []
        recipient_ids = []
        recipient_corps = []
        recipient_corp_ids = []
        recipient_alliances = []
        recipient_alliance_ids = []

        for mr in m.recipients.all():
            rid = mr.recipient_id
            rinfo = _cached_info(rid, timeee)
            recipient_ids.append(rid)
            recipient_names.append(rinfo["name"])
            recipient_corps.append(rinfo["corp_name"])
            recipient_corp_ids.append(rinfo["corp_id"])
            recipient_alliances.append(rinfo["alli_name"])
            recipient_alliance_ids.append(rinfo["alli_id"])

        result[mid] = {
            "message_id": mid,
            "sent_date": sent,
            "subject": m.subject or "",
            "sender_name": sinfo["name"],
            "sender_id": sender_id,
            "sender_corporation": sinfo["corp_name"],
            "sender_corporation_id": sinfo["corp_id"],
            "sender_alliance": sinfo["alli_name"],
            "sender_alliance_id": sinfo["alli_id"],
            "recipient_names": recipient_names,
            "recipient_ids": recipient_ids,
            "recipient_corps": recipient_corps,
            "recipient_corp_ids": recipient_corp_ids,
            "recipient_alliances": recipient_alliances,
            "recipient_alliance_ids": recipient_alliance_ids,
            "status": "Read" if m.is_read else "Unread",
        }

    logger.debug("Extracted %d mails", len(result))
    return result


def get_cell_style_for_mail_cell(column: str, row: dict, index: Optional[int] = None) -> str:
    """Centralized inline-style logic so tables and exports highlight hostiles."""
    solo = BigBrotherConfig.get_solo()
    # sender cell
    if column.startswith('sender_'):  # Apply consistent styling to all sender-related columns.
        if aablacklist_active():
            if column == 'sender_name' and check_char_corp_bl(row.get('sender_id')):  # Highlight hostile/blacklisted senders.
                return 'color: red;'
        if column == 'sender_corporation' and str(row.get('sender_corporation_id')) in solo.hostile_corporations:  # Hostile corp.
            return 'color: red;'
        if column == 'sender_alliance' and str(row.get('sender_alliance_id')) in solo.hostile_alliances:  # Hostile alliance.
            return 'color: red;'
    # recipient cell
    if column.startswith('recipient_') and index is not None:  # Recipient columns use parallel arrays, keep indexes in sync.
        if aablacklist_active():
            # blacklist check
            rid = row['recipient_ids'][index]
            if check_char_corp_bl(rid):  # Individual recipient appears on blacklist.
                return 'color: red;'
        # corp/alliance hostility
        cid = row['recipient_corps'][index] if column == 'recipient_corps' else None
        aid = row['recipient_alliance_ids'][index] if column == 'recipient_alliance_ids' else None
        if cid and str(cid) in solo.hostile_corporations:  # Recipient's corp flagged hostile.
            return 'color: red;'
        if aid and str(aid) in solo.hostile_alliances:  # Recipient alliance flagged hostile.
            return 'color: red;'
    return ''


def is_mail_row_hostile(row: dict) -> bool:
    solo = BigBrotherConfig.get_solo()

    if row.get("sender_name"):
        for key in ["GM ", "CCP "]:
            if key in str(row["sender_name"]):
                return True

    if aablacklist_active() and check_char_corp_bl(row.get("sender_id")):
        return True

    if str(row.get("sender_corporation_id")) in solo.hostile_corporations:
        return True
    if str(row.get("sender_alliance_id")) in solo.hostile_alliances:
        return True

    for idx, rid in enumerate(row["recipient_ids"]):
        if aablacklist_active() and check_char_corp_bl(rid):
            return True
        if str(row["recipient_corp_ids"][idx]) in solo.hostile_corporations:
            return True
        if str(row["recipient_alliance_ids"][idx]) in solo.hostile_alliances:
            return True

    return False



def render_mails(user_id: int) -> str:
    """
    Render an HTML table of hostile mails (up to 50 rows) with red highlights.
    """
    mails = get_user_mails(gather_user_mails(user_id))
    if not mails:  # User has no mail history yet.
        return '<p>No mails found.</p>'

    rows = sorted(mails.values(), key=lambda x: x['sent_date'], reverse=True)
    hostile_rows = [r for r in rows if is_mail_row_hostile(r)]
    total = len(hostile_rows)
    if total == 0:  # Nothing matched the hostile criteria.
        return '<p>No hostile mails found.</p>'

    limit = 50
    display = hostile_rows[:limit]
    skipped = max(total - limit, 0)

    # Only show these columns:
    VISIBLE = [
        'sent_date', 'subject',
        'sender_name', 'sender_corporation', 'sender_alliance',
        'recipient_names', 'recipient_corps', 'recipient_alliances', 'status',
    ]

    # Build HTML table
    html_parts = ['<table class="table table-striped table-hover stats">', '<thead><tr>']
    for col in VISIBLE:
        html_parts.append(f'<th>{html.escape(col.replace("_", " ").title())}</th>')
    html_parts.append('</tr></thead><tbody>')

    for row in display:
        html_parts.append('<tr>')
        for col in VISIBLE:
            val = row.get(col, '')
            # recipients come as lists
            if isinstance(val, list):  # List-based columns need per-element styling.
                parts = []
                for idx, item in enumerate(val):
                    style = ''  # default
                    # map list-column back to its id-array sibling:
                    if col == 'recipient_names':  # Names column uses multiple hostile checks.
                        rid = row['recipient_ids'][idx]
                        if aablacklist_active():
                            if check_char_corp_bl(rid):  # Highlight individual recipients on the blacklist.
                                style = 'color:red;'
                        elif str(row['recipient_corp_ids'][idx]) in BigBrotherConfig.get_solo().hostile_corporations:  # Recipient corp flagged hostile.
                            style = 'color:red;'
                        elif str(row['recipient_alliance_ids'][idx]) in BigBrotherConfig.get_solo().hostile_alliances:  # Recipient alliance flagged hostile.
                            style = 'color:red;'
                    elif col == 'recipient_corps':  # Corp column uses corp id list for styling.
                        cid = row['recipient_corp_ids'][idx]
                        if cid and str(cid) in BigBrotherConfig.get_solo().hostile_corporations:  # Hostile corporation entry.
                            style = 'color:red;'
                    elif col == 'recipient_alliances':  # Alliance column uses alliance ids.
                        aid = row['recipient_alliance_ids'][idx]
                        if aid and str(aid) in BigBrotherConfig.get_solo().hostile_alliances:  # Hostile alliance entry.
                            style = 'color:red;'

                    if style:  # Wrap each entry in a span to apply per-recipient color.
                        prefix = f"<span style='{style}'>"
                    else:
                        prefix = "<span>"

                    parts.append(f"{prefix}{html.escape(str(item))}</span>")
                cell = '<td>' + ', '.join(parts) + '</td>'
            else:
                # single-value columns
                style = ''
                if col.startswith('sender_'):  # Sender cells reuse the same hostile checks as the list-based helper.
                    if aablacklist_active():
                        if col == 'sender_name' and check_char_corp_bl(row['sender_id']):  # Sender is blacklisted.
                            style = 'color:red;'
                        elif col == 'sender_corporation' and str(row['sender_corporation_id']) in BigBrotherConfig.get_solo().hostile_corporations:  # Sender corp hostility.
                            style = 'color:red;'
                        elif col == 'sender_alliance' and str(row['sender_alliance_id']) in BigBrotherConfig.get_solo().hostile_alliances:  # Sender alliance hostility.
                            style = 'color:red;'
                    else:
                        if col == 'sender_corporation' and str(row['sender_corporation_id']) in BigBrotherConfig.get_solo().hostile_corporations:  # Sender corp hostility.
                            style = 'color:red;'
                        elif col == 'sender_alliance' and str(row['sender_alliance_id']) in BigBrotherConfig.get_solo().hostile_alliances:  # Sender alliance hostility.
                            style = 'color:red;'
                # subject/content keyword highlighting can be done client-side
                if style:  # Only emit style attribute when a highlight is needed.
                    style_attr = f" style='{style}'"
                else:
                    style_attr = ""
                cell = f"<td{style_attr}>{html.escape(str(val))}</td>"

            html_parts.append(cell)
        html_parts.append('</tr>')

    html_parts.append('</tbody></table>')
    if skipped:  # Alert reviewers when more hostile mails exist beyond the table.
        html_parts.append(f'<p>Showing {limit} of {total} hostile mails; skipped {skipped}.</p>')

    return '\n'.join(html_parts)



def get_user_hostile_mails(user_id: int) -> Dict[int, str]:
    cfg = BigBrotherConfig.get_solo()

    all_qs = gather_user_mails(user_id)
    all_ids = list(all_qs.values_list("id_key", flat=True))

    seen_ids = set(
        ProcessedMail.objects.filter(mail_id__in=all_ids).values_list("mail_id", flat=True)
    )

    new_ids = [mid for mid in all_ids if mid not in seen_ids]
    notes: Dict[int, str] = {}

    if new_ids:
        new_qs = all_qs.filter(id_key__in=new_ids)
        new_rows = get_user_mails(new_qs)

        hostile_rows: dict[int, dict] = {mid: m for mid, m in new_rows.items() if is_mail_row_hostile(m)}

        pms: dict[int, ProcessedMail] = {}
        if hostile_rows:
            ProcessedMail.objects.bulk_create(
                [ProcessedMail(mail_id=mid) for mid in hostile_rows.keys()],
                ignore_conflicts=True,
            )
            pms = {
                pm.mail_id: pm
                for pm in ProcessedMail.objects.filter(mail_id__in=hostile_rows.keys())
            }

        for mid, m in hostile_rows.items():
            pm = pms.get(mid)
            if not pm:
                continue

            flags: List[str] = []
            if aablacklist_active() and check_char_corp_bl(m["sender_id"]):
                flags.append(f"Sender **{m['sender_name']}** is on blacklist")
            if str(m["sender_corporation_id"]) in cfg.hostile_corporations:
                flags.append(f"Sender corp **{m['sender_corporation']}** is hostile")
            if str(m["sender_alliance_id"]) in cfg.hostile_alliances:
                flags.append(f"Sender alliance **{m['sender_alliance']}** is hostile")

            for idx, rid in enumerate(m.get("recipient_ids", [])):
                name = m["recipient_names"][idx]
                if aablacklist_active() and check_char_corp_bl(rid):
                    flags.append(f"Recipient **{name}** is on blacklist")
                cid = m["recipient_corp_ids"][idx]
                if cid and str(cid) in cfg.hostile_corporations:
                    flags.append(f"Recipient corp **{m['recipient_corps'][idx]}** is hostile")
                aid = m["recipient_alliance_ids"][idx]
                if aid and str(aid) in cfg.hostile_alliances:
                    flags.append(f"Recipient alliance **{m['recipient_alliances'][idx]}** is hostile")

            flags_text = "\n    - ".join(flags) if flags else "(no flags)"

            note_text = (
                f"- **'{m['subject']}'**: "
                f"\n  - sent {m['sent_date']}; "
                f"\n  - from **{m['sender_name']}**(**{m['sender_corporation']}**/"
                f"**{m['sender_alliance']}**), "
                f"\n  - flags:\n    - {flags_text}"
            )

            SusMailNote.objects.update_or_create(
                mail=pm,
                defaults={"user_id": user_id, "note": note_text},
            )
            notes[mid] = note_text

    for note in SusMailNote.objects.filter(user_id=user_id):
        notes[note.mail.mail_id] = note.note

    return notes
