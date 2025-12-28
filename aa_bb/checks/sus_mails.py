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
    get_safe_entities,
    aablacklist_active,
    get_hostile_state,
)

if aablacklist_active():
    from .add_to_blacklist import check_char_add_to_bl
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
    when = row.get("sent_date")
    # sender cell
    if column.startswith('sender_'):
        sid = row.get('sender_id')
        if get_hostile_state(sid, when=when):
            return 'color: red;'
    # recipient cell
    if column.startswith('recipient_') and index is not None:
        rid = row['recipient_ids'][index]
        if get_hostile_state(rid, when=when):
            return 'color: red;'
    return ''


def is_mail_row_hostile(row: dict) -> bool:
    def _to_int(val):
        try:
            return int(val) if val is not None else None
        except (ValueError, TypeError):
            return None

    sender_id = _to_int(row.get("sender_id"))
    sender_corp_id = _to_int(row.get("sender_corporation_id"))
    sender_alli_id = _to_int(row.get("sender_alliance_id"))
    when = row.get("sent_date")

    recipient_ids = [_to_int(rid) for rid in row.get("recipient_ids", [])]
    recipient_corp_ids = [_to_int(rcid) for rcid in row.get("recipient_corp_ids", [])]
    recipient_alli_ids = [_to_int(raid) for raid in row.get("recipient_alliance_ids", [])]

    if row.get("sender_name"):
        for key in ["GM ", "CCP "]:
            if key in str(row["sender_name"]):
                return True

    # Same corporation check (sender and ALL recipients in same corp)
    if sender_corp_id and all(rcid == sender_corp_id for rcid in recipient_corp_ids if rcid):
        if recipient_corp_ids: # Ensure there is at least one recipient with a corp
            return False

    # Check sender hostility
    if get_hostile_state(sender_id, when=when):
        return True

    # Check recipients hostility
    for rid in recipient_ids:
        if get_hostile_state(rid, when=when):
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
            if isinstance(val, list):
                parts = []
                for idx, item in enumerate(val):
                    style = get_cell_style_for_mail_cell(col, row, index=idx)
                    prefix = f"<span style='{style}'>" if style else "<span>"
                    parts.append(f"{prefix}{html.escape(str(item))}</span>")
                cell = '<td>' + ', '.join(parts) + '</td>'
            else:
                style = get_cell_style_for_mail_cell(col, row)
                style_attr = f" style='{style}'" if style else ""
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
            # Check sender
            if get_hostile_state(m.get("sender_id"), 'character'):
                flags.append(f"Sender **{m['sender_name']}** is hostile/blacklisted")

            # Check recipients
            for idx, rid in enumerate(m.get("recipient_ids", [])):
                if get_hostile_state(rid, 'character'):
                    name = m["recipient_names"][idx]
                    flags.append(f"Recipient **{name}** is hostile/blacklisted")

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
