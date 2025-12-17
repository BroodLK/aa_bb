"""
Routines related to corporation/alliance history checks.

The functions here produce friendly HTML summaries as well as reusable
helpers that other sections (e.g. cyno readiness) use to determine how
long a member has been in corp.
"""

import logging
from datetime import timedelta
from django.utils.html import format_html
from django.utils.timezone import now, timezone
from allianceauth.authentication.models import CharacterOwnership
from ..models import BigBrotherConfig, FrequentCorpChangesCache, CurrentStintCache
from ..app_settings import (
    ensure_datetime,
    is_npc_corporation,
    get_alliance_history_for_corp,
    get_alliance_name,
    get_corporation_info,
    get_character_employment,
)

logger = logging.getLogger(__name__)
TTL_SHORT = timedelta(hours=4)

# External site favicons, fetched each time directly from the source
ZKILL_ICON = "https://zkillboard.com/favicon.ico"
EVEWHO_ICON = "https://evewho.com/favicon.ico"
DOTLAN_ICON = "https://evemaps.dotlan.net/favicon.ico"
EVE411_ICON     = "https://www.eve411.com/favicon.ico"
FORUMS_ICON     = "https://eve-offline.net/favicon.ico"
EVESEARCH_ICON  = "https://eve-search.com/favicon.ico"


def get_frequent_corp_changes(user_id):
    """
    Build (and cache) an HTML report showing each corp membership stint.

    Hostile corps/alliances are highlighted in-line and per-character tables
    also include convenience links to the typical intel sites.
    """
    # Try 4h cache first
    try:
        cache = FrequentCorpChangesCache.objects.get(pk=user_id)
        if timezone.now() - cache.updated < TTL_SHORT:  # Serve cached card for ~4h to limit upstream calls.
            try:
                cache.last_accessed = timezone.now()
                cache.save(update_fields=["last_accessed"])
            except Exception:
                cache.save()
            return format_html(cache.html)
    except FrequentCorpChangesCache.DoesNotExist:
        pass
    # Load hostile lists
    cfg = BigBrotherConfig.get_solo()
    hostile_corps = {int(cid) for cid in cfg.hostile_corporations.split(',') if cid}  # Precompute hostile corp IDs.
    hostile_alliances = {int(aid) for aid in cfg.hostile_alliances.split(',') if aid}  # Likewise for alliances.

    characters = CharacterOwnership.objects.filter(user__id=user_id)
    html = ""

    for char in characters:
        char_name = str(char.character)
        char_id   = char.character.character_id
        try:
            history = get_character_employment(char_id)
        except Exception:
            continue

        char_links = (
            f'<a href="https://zkillboard.com/character/{char_id}/" target="_blank">'
            f'<img src="{ZKILL_ICON}" width="16" height="16" '
            f'style="margin-left:4px;vertical-align:middle;"/></a> '
            f'<a href="https://evewho.com/character/{char_id}" target="_blank">'
            f'<img src="{EVEWHO_ICON}" width="16" height="16" '
            f'style="margin-left:2px;vertical-align:middle;"/></a> '
            f'<a href="https://www.eve411.com/character/{char_id}" target="_blank">'
            f'<img src="{EVE411_ICON}" width="16" height="16" '
            f'style="margin-left:2px;vertical-align:middle;"/></a> '
            # Eve-Online forums user pages use the character name slug:
            f'<a href="https://forums.eveonline.com/u/{char_name.replace(" ", "_")}/summary" '
            f'target="_blank">'
            f'<img src="{FORUMS_ICON}" width="16" height="16" '
            f'style="margin-left:2px;vertical-align:middle;"/></a> '
            # and eve-search needs URL‐encoded name:
            f'<a href="https://eve-search.com/search/author/{char_name.replace(" ", "%20")}" '
            f'target="_blank">'
            f'<img src="{EVESEARCH_ICON}" width="16" height="16" '
            f'style="margin-left:2px;vertical-align:middle;"/></a> '
        )

        rows = []

        for idx, membership in enumerate(history):
            corp_id = membership['corporation_id']
            if is_npc_corporation(corp_id):  # Skip meaningless entries (NPC corps clutter the table).
                continue

            # Membership window
            start = ensure_datetime(membership['start_date'])
            end = ensure_datetime(history[idx+1]['start_date']) if idx+1 < len(history) else now()  # End date = next start or now.
            total_days = (end - start).days

            corp_name = get_corporation_info(corp_id)['name']
            membership_range = f"{start.date()} - {end.date()}"

            # Corp cell with external site favicons (fetched live)
            corp_color = ' class="text-danger' if (hostile_corps and corp_id in hostile_corps) else ''  # Highlight hostile corps.
            corp_cell = (
                f'<span{corp_color}>{corp_name}</span>'
                f'<a href="https://zkillboard.com/corporation/{corp_id}/" target="_blank">'
                f'<img src="{ZKILL_ICON}" width="16" height="16" style="margin-left:4px;vertical-align:middle;"/></a> '
                f'<a href="https://evewho.com/corporation/{corp_id}" target="_blank">'
                f'<img src="{EVEWHO_ICON}" width="16" height="16" style="margin-left:2px;vertical-align:middle;"/></a> '
                f'<a href="https://evemaps.dotlan.net/corp/{corp_id}" target="_blank">'
                f'<img src="{DOTLAN_ICON}" width="16" height="16" style="margin-left:2px;vertical-align:middle;"/></a> '
            )

            # Alliance segments
            alliances_html = []
            periods_html = []
            alliance_history = get_alliance_history_for_corp(corp_id)
            for j, ent in enumerate(alliance_history):
                a_start = ent['start_date']
                a_end = alliance_history[j+1]['start_date'] if j+1 < len(alliance_history) else None
                seg_start = max(start, a_start)
                seg_end = min(end, a_end) if a_end else end
                if seg_start < seg_end:  # Only render overlapping time periods (ignore non-overlaps).
                    aid = ent['alliance_id']
                    aname = get_alliance_name(aid)
                    period = f"{seg_start.date()} - {seg_end.date()}"

                    if aid:  # Only render alliance rows when the corp was in an alliance.
                        alliance_color = ' class="text-danger"' if (hostile_alliances and aid in hostile_alliances) else ''  # Flag hostile alliances.
                        name_cell = f'<span{alliance_color}>{aname}</span>'
                        icons = (
                            f'<a href="https://zkillboard.com/alliance/{aid}/" target="_blank">'
                            f'<img src="{ZKILL_ICON}" width="16" height="16" style="margin-left:4px;vertical-align:middle;"/></a> '
                            f'<a href="https://evewho.com/alliance/{aid}" target="_blank">'
                            f'<img src="{EVEWHO_ICON}" width="16" height="16" style="margin-left:2px;vertical-align:middle;"/></a> '
                            f'<a href="https://evemaps.dotlan.net/alliance/{aid}" target="_blank">'
                            f'<img src="{DOTLAN_ICON}" width="16" height="16" style="margin-left:2px;vertical-align:middle;"/></a> '
                        )
                    else:
                        name_cell = '-'
                        icons = ''
                    alliances_html.append(name_cell + icons)
                    periods_html.append(period)

            if not alliances_html:  # When no alliance data, fallback to corp membership range.
                alliances_html = ['-']
                periods_html = [membership_range]

            # Duration cell coloring only
            dur_color = ' class="text-danger"' if total_days < 10 else (' class="text-warning"' if total_days < 30 else '')  # Quick visual for recent corps.

            rows.append({
                'corp_cell': corp_cell,
                'membership_range': membership_range,
                'alliances_html': '<br>'.join(alliances_html),
                'periods_html': '<br>'.join(periods_html),
                'total_days': total_days,
                'dur_color': dur_color,
            })

        html += format_html("<h3>{} {}</h3>", char_name, format_html(char_links))
        html += '<table class="table table-striped table-hover stats">'
        html += '<thead><tr><th>Corporation</th><th>Membership</th><th>Alliance(s)</th><th>Alliance Dates</th><th>Time in Corp</th></tr></thead><tbody>'
        for r in rows:
            row_html = (
                '<tr>'
                f'<td>{r["corp_cell"]}</td>'
                f'<td>{r["membership_range"]}</td>'
                f'<td>{r["alliances_html"]}</td>'
                f'<td>{r["periods_html"]}</td>'
                f'<td{r["dur_color"]}>{r["total_days"]} days</td>'
                '</tr>'
            )
            html += format_html(row_html)
        html += '</tbody></table>'

    # Save cache
    try:
        FrequentCorpChangesCache.objects.update_or_create(
            user_id=user_id, defaults={"html": str(html), "last_accessed": timezone.now()}
        )
    except Exception:
        pass
    return format_html(html)

def time_in_corp(user_id):
    """
    Return the maximum number of days any of the user's characters have been
    continuously in the configured main corporation.
    """
    days = 0
    characters = CharacterOwnership.objects.filter(user__id=user_id)
    for char in characters:
        char_id   = char.character.character_id
        c_days = get_current_stint_days_in_corp(char_id, BigBrotherConfig.get_solo().main_corporation_id)
        if c_days > days:  # Track the maximum stint across all characters.
            days = c_days
    return days

def get_current_stint_days_in_corp(char_id: int, corp_id: int) -> int:
    """
    Return the number of days the character has been in the given corporation *currently*.
    If the character is not in that corporation right now, return 0.
    """
    try:
        if is_npc_corporation(corp_id):  # NPC corps don't matter for stint tracking.
            return 0

        # 4h cached value
        try:
            cache = CurrentStintCache.objects.get(char_id=char_id, corp_id=corp_id)
            if timezone.now() - cache.updated < TTL_SHORT:  # Serve cached days when fresh.
                try:
                    cache.last_accessed = timezone.now()
                    cache.save(update_fields=["last_accessed"])
                except Exception:
                    cache.save()
                return int(cache.days)
        except CurrentStintCache.DoesNotExist:
            pass

        history = get_character_employment(char_id)

        if not history:  # No employment history returned -> treat as zero.
            return 0

        # Latest membership entry appears at the end of the history list
        latest = history[-1]
        if latest["corporation_id"] != corp_id:  # Character left the corp already.
            return 0

        start = ensure_datetime(latest["start_date"])
        end = now()

        days = max(0, (end - start).days)
        try:
            CurrentStintCache.objects.update_or_create(
                char_id=char_id,
                corp_id=corp_id,
                defaults={"days": days, "last_accessed": timezone.now()},
            )
        except Exception:
            pass
        return days

    except Exception as e:
        logger.exception(
            "Failed to compute current stint days for char %s in corp %s: %s",
            char_id, corp_id, e
        )
        return 0
