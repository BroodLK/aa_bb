"""
Mail intelligence helpers.

These helpers normalize MailMessage rows, detect suspicious senders or
recipients, and persist short notes for repeated reporting.
"""

from allianceauth.services.hooks import get_extension_logger


import html
from typing import Dict, Optional, List
from datetime import datetime
from django.utils import timezone

from ..app_settings import (
    get_user_characters,
    get_entity_info,
    get_safe_entities,
    aablacklist_active,
    get_hostile_state,
    corptools_active,
    is_hostile_unified,
)

if aablacklist_active():
    from .add_to_blacklist import check_char_add_to_bl
else:
    def check_char_corp_bl(_cid: int) -> bool:
        return False

from django.db.models import Q
from ..models import BigBrotherConfig, ProcessedMail, SusMailNote, EntityInfoCache

logger = get_extension_logger(__name__)

try:
    if corptools_active():
        from corptools.models import MailMessage, MailRecipient
    else:
        MailMessage = None
        MailRecipient = None
except ImportError:
    MailMessage = None
    MailRecipient = None


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
    if not corptools_active() or MailMessage is None:
        return []
    user_chars = get_user_characters(user_id)
    user_ids = set(user_chars.keys())
    return MailMessage.objects.filter(
        recipients__recipient_id__in=user_ids
    ).prefetch_related("recipients", "recipients__recipient_name")


def get_user_mails(qs) -> Dict[int, Dict]:
    # Use a list to avoid re-evaluating the queryset
    entries = list(qs)
    if not entries:
        return {}

    result: Dict[int, Dict] = {}

    # Pre-collect all entity IDs and their normalized timestamps for bulk fetching info
    # EntityInfoCache normalizes to the hour.
    lookups = set()
    for m in entries:
        dt_hour = m.timestamp.replace(minute=0, second=0, microsecond=0)
        if m.from_id:
            lookups.add((m.from_id, dt_hour))
        for mr in m.recipients.all():
            if mr.recipient_id:
                lookups.add((mr.recipient_id, dt_hour))

    # Bulk fetch EntityInfoCache entries
    # To avoid a massive Q-object chain for very large batches, we fetch by entity_id
    # and filter by hour as well to limit the result set.
    eids = {l[0] for l in lookups}
    hours = {l[1] for l in lookups}
    cache_entries = EntityInfoCache.objects.filter(entity_id__in=eids, as_of__in=hours)

    # Map them by (entity_id, as_of)
    info_map = {
        (ce.entity_id, ce.as_of): ce.data
        for ce in cache_entries
    }

    def _get_info(eid: int, when: datetime) -> dict:
        if not eid:
            return get_entity_info(None, when)

        dt_hour = when.replace(minute=0, second=0, microsecond=0)
        cached = info_map.get((eid, dt_hour))
        if cached:
            return cached

        # Fallback to single fetch
        info = get_entity_info(eid, when)
        info_map[(eid, dt_hour)] = info
        return info

    for m in entries:
        mid = m.id_key
        sent = m.timestamp
        timeee = m.timestamp or timezone.now()

        sender_id = m.from_id
        sinfo = _get_info(sender_id, timeee)

        recipient_names = []
        recipient_ids = []
        recipient_corps = []
        recipient_corp_ids = []
        recipient_alliances = []
        recipient_alliance_ids = []

        r_info_cache = {}

        for mr in m.recipients.all():
            rid = mr.recipient_id
            rinfo = _get_info(rid, timeee)
            recipient_ids.append(rid)
            recipient_names.append(rinfo["name"])
            recipient_corps.append(rinfo["corp_name"])
            recipient_corp_ids.append(rinfo["corp_id"])
            recipient_alliances.append(rinfo["alli_name"])
            recipient_alliance_ids.append(rinfo["alli_id"])
            r_info_cache[rid] = rinfo

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
            "info_cache": {
                sender_id: sinfo,
                **r_info_cache
            }
        }

    logger.debug("Extracted %d mails", len(result))
    return result


def get_cell_style_for_mail_cell(column: str, row: dict, index: Optional[int] = None) -> str:
    """Centralized inline-style logic so tables and exports highlight hostiles."""
    when = row.get("sent_date")
    # sender cell
    if column.startswith('sender_'):
        sid = row.get('sender_id')
        if get_hostile_state(sid, when=when, entity_info_cache=row.get("info_cache")):
            return 'color: red;'
    # recipient cell
    if column.startswith('recipient_') and index is not None:
        rid = row['recipient_ids'][index]
        if get_hostile_state(rid, when=when, entity_info_cache=row.get("info_cache")):
            return 'color: red;'
    return ''


def is_mail_row_hostile(row: dict, safe_entities: set = None, cfg: BigBrotherConfig = None) -> bool:
    """
    Checks if a mail is considered hostile using the unified processor.
    """
    sender_id = row.get("sender_id")
    recipient_ids = row.get("recipient_ids", [])
    involved = [sender_id] + list(recipient_ids)

    # CCP/GM check (Custom rule for mails)
    if row.get("sender_name"):
        for key in ["GM ", "CCP "]:
            if key in str(row["sender_name"]):
                return True

    return is_hostile_unified(
        involved_ids=involved,
        when=row.get("sent_date"),
        safe_entities=safe_entities,
        entity_info_cache=row.get("info_cache"),
        cfg=cfg
    )



def render_mails(user_id: int) -> str:
    """
    Render an HTML table of hostile mails (up to 50 rows) with red highlights.
    """
    mails = get_user_mails(gather_user_mails(user_id))
    if not mails:  # User has no mail history yet.
        return '<table class="table stats"><tbody><tr><td class="text-center">No mails found.</td></tr></tbody></table>'

    from ..app_settings import get_safe_entities
    safe_entities = get_safe_entities()

    rows = sorted(mails.values(), key=lambda x: x['sent_date'], reverse=True)
    hostile_rows = [r for r in rows if is_mail_row_hostile(r, safe_entities=safe_entities)]
    total = len(hostile_rows)
    if total == 0:  # Nothing matched the hostile criteria.
        return '<table class="table stats"><tbody><tr><td class="text-center">No hostile mails found.</td></tr></tbody></table>'

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
                    prefix = f"<span style='{style}' class='text-danger'>" if style else "<span>"
                    parts.append(f"{prefix}{html.escape(str(item))}</span>")
                cell = '<td>' + ', '.join(parts) + '</td>'
            else:
                style = get_cell_style_for_mail_cell(col, row)
                style_attr = f" style='{style}' class='text-danger'" if style else ""
                cell = f"<td{style_attr}>{html.escape(str(val))}</td>"

            html_parts.append(cell)
        html_parts.append('</tr>')

    html_parts.append('</tbody></table>')
    if skipped:  # Alert reviewers when more hostile mails exist beyond the table.
        html_parts.append(f'<p>Showing {limit} of {total} hostile mails; skipped {skipped}.</p>')

    return '\n'.join(html_parts)



def get_user_hostile_mails(user_id: int, cfg: BigBrotherConfig = None, safe_entities: set = None, entity_info_cache: dict = None) -> Dict[int, str]:
    if cfg is None:
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

        # Mark all new mails as processed to prevent re-processing safe ones
        ProcessedMail.objects.bulk_create(
            [ProcessedMail(mail_id=mid) for mid in new_ids],
            ignore_conflicts=True,
        )

        if safe_entities is None:
            from ..app_settings import get_safe_entities
            safe_entities = get_safe_entities()

        hostile_rows: dict[int, dict] = {mid: m for mid, m in new_rows.items() if is_mail_row_hostile(m, safe_entities=safe_entities, cfg=cfg)}

        pms: dict[int, ProcessedMail] = {}
        if hostile_rows:
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
            if get_hostile_state(m.get("sender_id"), 'character', safe_entities=safe_entities, entity_info_cache=entity_info_cache, cfg=cfg):
                flags.append(f"Sender **{m['sender_name']}** is hostile/blacklisted")

            # Check recipients
            for idx, rid in enumerate(m.get("recipient_ids", [])):
                if get_hostile_state(rid, 'character', safe_entities=safe_entities, entity_info_cache=entity_info_cache, cfg=cfg):
                    name = m["recipient_names"][idx]
                    flags.append(f"Recipient **{name}** is hostile/blacklisted")

            flags_text = "\n    - ".join(flags) if flags else "(no flags)"

            note_text = (
                f"- **'{m['subject']}'** (Sent: {m['sent_date']})"
                f"\n  - **From:** {m['sender_name']} ({m['sender_corporation']} | {m['sender_alliance']})"
                f"\n  - **Flags:**"
                f"\n    - {flags_text}"
            )

            SusMailNote.objects.update_or_create(
                mail=pm,
                defaults={"user_id": user_id, "note": note_text},
            )
            notes[mid] = note_text

    for note in SusMailNote.objects.filter(user_id=user_id):
        notes[note.mail.mail_id] = note.note

    return notes
