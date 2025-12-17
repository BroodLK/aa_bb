from django.db.utils import OperationalError
import time
import traceback
from django.utils import timezone
from celery import shared_task

from .models import (
    UserStatus,
    BigBrotherConfig,
    NeutralHandling,
    TicketToolConfig,
    AA_CONTACTS_INSTALLED
)

from .app_settings import (
    resolve_character_name,
    get_users,
    get_user_id,
    get_character_id,
    get_pings,
    send_message
)
from aa_bb.checks.awox import get_awox_kill_links
from aa_bb.checks.cyno import get_user_cyno_info, get_current_stint_days_in_corp
from aa_bb.checks.skills import get_multiple_user_skill_info, skill_ids, get_char_age
from aa_bb.checks.hostile_assets import get_hostile_asset_locations
from aa_bb.checks.hostile_clones import get_hostile_clone_locations
from aa_bb.checks.sus_contacts import get_user_hostile_notifications
from aa_bb.checks.sus_contracts import get_user_hostile_contracts
from aa_bb.checks.sus_mails import get_user_hostile_mails
from aa_bb.checks.sus_trans import get_user_hostile_transactions
from aa_bb.checks.clone_state import determine_character_state
from aa_bb.checks.corp_changes import time_in_corp

# Import sibling tasks to maintain module API surface
from .tasks_cb import *
from .tasks_ct import *
from .tasks_tickets import *
from .tasks_other import *

from allianceauth.eveonline.models import EveCharacter
from django.contrib.auth import get_user_model

try:
    from aadiscordbot.utils.auth import get_discord_user_id
    from aadiscordbot.tasks import run_task_function
except ImportError:
    get_discord_user_id = None
    run_task_function = None

from allianceauth.services.hooks import get_extension_logger

logger = get_extension_logger(__name__)
VERBOSE_WEBHOOK_LOGGING = True

def send_status_embed(
    subject: str,
    lines: list[str],
    *,
    override_title: str | None = None,
    color: int = 0xED4245,  # Discord red
):
    """
    Send a Discord embed via the existing send_message() webhook.
    """

    if VERBOSE_WEBHOOK_LOGGING:
        logger.debug(
            "[EMBED] send_status_embed called | subject=%r | lines=%d",
            subject,
            len(lines) if lines else 0,
        )

    # Defensive: never send empty embeds
    if not lines:
        logger.debug("[EMBED] aborted: no lines supplied")
        return

    # Discord limits
    MAX_DESC = 4096
    MAX_LINES = 50

    title = override_title if override_title is not None else subject

    if VERBOSE_WEBHOOK_LOGGING:
        logger.debug(
            "[EMBED] title resolved | title=%r | color=%#x",
            title,
            color,
        )

    # Trim excessive lines but keep tables intact
    safe_lines = lines[:MAX_LINES]

    if len(lines) > MAX_LINES:
        logger.warning(
            "[EMBED] line cap exceeded | original=%d | capped=%d",
            len(lines),
            MAX_LINES,
        )

    description = "\n".join(safe_lines)

    # Hard truncate if someone messed up
    if len(description) > MAX_DESC:
        logger.error(
            "[EMBED] description overflow | chars=%d | truncating",
            len(description),
        )
        description = description[: MAX_DESC - 3] + "..."

    if VERBOSE_WEBHOOK_LOGGING:
        logger.debug(
            "[EMBED] payload ready | lines=%d | chars=%d",
            len(safe_lines),
            len(description),
        )

    embed = {
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": color,
            }
        ]
    }

    if VERBOSE_WEBHOOK_LOGGING:
        logger.debug("[EMBED] sending embed payload")
    time.sleep(0.25)
    send_message(embed)


# Helper to keep embed lines reasonably narrow for mobile (≈40 chars)
def _chunk_embed_lines(lines, max_chars=1900):
    """
    Split a list of lines into chunks whose joined text length
    is <= max_chars, without breaking ``` code blocks.

    Returns: List[List[str]] – each inner list is one embed body.
    """
    # First, group into "segments": either a full code block or a run of normal lines
    segments = []
    current_segment = []
    in_code = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```"):
            # Starting a new code block
            if not in_code:
                # flush any accumulated non-code segment
                if current_segment:
                    segments.append(current_segment)
                    current_segment = []
                in_code = True
                current_segment = [line]
            else:
                # closing an existing code block
                current_segment.append(line)
                segments.append(current_segment)
                current_segment = []
                in_code = False
        else:
            current_segment.append(line)

    if current_segment:
        segments.append(current_segment)

    # Now pack segments into chunks by total char length
    chunks = []
    current_chunk = []
    current_len = 0

    for seg in segments:
        # Estimate length if we add this segment (with newlines)
        seg_text = "\n".join(seg)
        seg_len = len(seg_text) + (1 if current_chunk else 0)  # +1 for newline before segment

        if seg_len > max_chars:
            # Segment itself is huge; fall back to splitting inside it line-by-line
            for line in seg:
                line_len = len(line) + (1 if current_chunk else 0)
                if current_len + line_len > max_chars and current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = [line]
                    current_len = len(line)
                else:
                    current_chunk.append(line)
                    current_len += line_len
            continue

        if current_len + seg_len > max_chars and current_chunk:
            # Start a new chunk
            chunks.append(current_chunk)
            current_chunk = list(seg)
            current_len = len(seg_text)
        else:
            # Add segment to current chunk
            if current_chunk:
                current_chunk.append("")  # ensure a blank line between segments
                current_len += 1
            current_chunk.extend(seg)
            current_len += len(seg_text)

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def parse_hostile_summary(summary: str):
    """
    Parse the summary string from get_hostile_asset_locations /
    get_hostile_clone_locations into (owner, ships, chars).

    Expected format from helpers is:
      "Owner Name | Ships: A, B | Chars: X, Y"
    or
      "Owner Name | Chars: X, Y"

    Ships may be empty for clones.
    """
    owner = summary or "Unresolvable"
    ships = ""
    chars: list[str] = []

    if not summary:
        return owner, ships, chars

    parts = [p.strip() for p in summary.split("|") if p.strip()]
    if parts:
        owner = parts[0]

    for seg in parts[1:]:
        label, _, rest = seg.partition(":")
        label = label.strip().lower()
        value = rest.strip()
        if label == "ships":
            ships = value
        elif label == "chars":
            if value:
                chars = [c.strip() for c in value.split(",") if c.strip()]

    return owner, ships, chars


@shared_task
def BB_update_single_user(user_id, char_name):
    """
    Process updates for a single user.
    Broken out from BB_run_regular_updates for scalability.
    """
    logger.info(f"START Update for user: {char_name} (ID: {user_id})")

    instance = BigBrotherConfig.get_solo()
    if not instance.is_active:
        logger.info(f"BigBrother inactive. Skipping update for {char_name}.")
        return

    User = get_user_model()

    # Retry logic previously inside the main loop
    for attempt in range(3):
        try:
            # pingroleID = instance.pingroleID # Unused variable?

            logger.info(f"[{char_name}] Fetching Cyno Info...")
            cyno_result = get_user_cyno_info(user_id)

            logger.info(f"[{char_name}] Fetching Skill Info...")
            skills_result = get_multiple_user_skill_info(user_id, skill_ids)

            logger.info(f"[{char_name}] Determining Character State...")
            state_result = determine_character_state(user_id, True)

            logger.info(f"[{char_name}] Fetching AWOX Links...")
            awox_data = get_awox_kill_links(user_id)
            awox_links = [x["link"] for x in awox_data]
            awox_map = {x["link"]: x for x in awox_data}

            logger.info(f"[{char_name}] Fetching Hostile Clones...")
            hostile_clones_result = get_hostile_clone_locations(user_id)

            logger.info(f"[{char_name}] Fetching Hostile Assets...")

            hostile_assets_result = get_hostile_asset_locations(user_id)

            logger.info(f"[{char_name}] Fetching Sus Contacts/Contracts/Mails/Trans...")
            sus_contacts_result = {str(cid): v for cid, v in get_user_hostile_notifications(user_id).items()}
            sus_contracts_result = {str(issuer_id): v for issuer_id, v in get_user_hostile_contracts(user_id).items()}
            sus_mails_result = {str(issuer_id): v for issuer_id, v in get_user_hostile_mails(user_id).items()}
            sus_trans_result = {str(issuer_id): v for issuer_id, v in get_user_hostile_transactions(user_id).items()}

            sp_age_ratio_result: dict[str, dict] = {}

            def norm(d):
                d = d or {}
                return {
                    n: {k: v for k, v in (entry if isinstance(entry, dict) else {}).items() if k != 'age'}
                    # drop 'age' noise when diffing
                    for n, entry in d.items()
                }

            def skills_norm(d):
                out = {}
                for name, entry in (d or {}).items():
                    if not isinstance(entry, dict):  # ignore non-dict placeholders just in case
                        continue
                    filtered = {}
                    for k, v in entry.items():
                        k_str = str(k)
                        if k_str == 'total_sp':  # skip total SP row when comparing per skill
                            continue
                        if isinstance(v, dict):  # only keep nested skill dicts
                            filtered[k_str] = {
                                'trained': v.get('trained', 0) or 0,
                                'active': v.get('active', 0) or 0,
                            }
                    out[name] = filtered
                return out

            logger.info(f"[{char_name}] Processing SP Ratios...")
            for char_nameeee, data in skills_result.items():
                char_id = get_character_id(char_nameeee)
                char_age = get_char_age(char_id)
                total_sp = data["total_sp"]
                sp_days = (total_sp - 384000) / 64800 if total_sp else 0  # convert SP into training-day equivalent

                sp_age_ratio_result[char_nameeee] = {
                    **data,  # keep original skill info
                    "sp_days": sp_days,
                    "char_age": char_age,
                }

            has_cyno = any(
                char_dic.get("can_light", False)
                for char_dic in (cyno_result or {}).values()
            )
            has_skills = any(
                entry[sid]["trained"] > 0 or entry[sid]["active"] > 0
                for entry in skills_result.values()
                for sid in skill_ids
            )
            has_awox = bool(awox_links)
            has_hostile_clones = bool(hostile_clones_result)
            has_hostile_assets = bool(hostile_assets_result)
            has_sus_contacts = bool(sus_contacts_result)
            has_sus_contracts = bool(sus_contracts_result)
            has_sus_mails = bool(sus_mails_result)
            has_sus_trans = bool(sus_trans_result)

            # load (or create) cached status so diffs apply correctly
            status, created = UserStatus.objects.get_or_create(user_id=user_id)

            # On the very first run for this user, silently create a baseline
            if not instance.new_user_notify:
                send_notifications = not created
            else:
                send_notifications = True
            logger.info(f"[{char_name}] Status loaded (created={created}). Calculating changes...")

            changes = []

            def as_dict(x):
                return x if isinstance(x, dict) else {}  # utility to guard against None/non-dict entries

            if set(state_result) != set(status.clone_status or []):  # clone-state map changed?
                # capture clone-state transitions (alpha→omega etc.)
                old_states = status.clone_status or {}
                diff = {}
                flagggs = []

                # build dict of changes
                for char_idddd, new_data in state_result.items():
                    old_data = old_states.get(str(char_idddd)) or old_states.get(char_idddd)  # handle str/int keys
                    if not old_data or old_data.get("state") != new_data.get(
                        "state"):  # capture per-character state transitions
                        diff[char_idddd] = {
                            "old": old_data.get("state") if old_data else None,  # previous state (None when unseen)
                            "new": new_data.get("state"),
                        }

                # add messages to flags
                for char_idddd, change in diff.items():
                    char_nameeeee = resolve_character_name(char_idddd)
                    flagggs.append(
                        f"\n- **{char_nameeeee}**: {change['old']} → **{change['new']}**"
                    )

                pinggg = ""

                if "omega" in flagggs:  # ping when someone upgrades to omega
                    pinggg = get_pings('Omega Detected')

                # final summary message
                if flagggs:  # only when changes are detected should notifications and saves occur
                    if instance.clone_notify:
                        changes.append(f"###{pinggg} Clone state change detected:{''.join(flagggs)}")
                    status.clone_status = state_result
                    status.save()

            if set(sp_age_ratio_result) != set(status.sp_age_ratio_result or []):  # detect changes in SP-to-age ratios
                flaggs = []

                def _safe_ratio(info: dict):
                    age = info.get("char_age")
                    if not isinstance(age, (int, float)) or age <= 0:  # bail when no usable age is available
                        return None
                    return (info.get("sp_days") or 0) / max(age, 1)

                for char_nameee, new_info in sp_age_ratio_result.items():
                    old_info = (status.sp_age_ratio_result or {}).get(char_nameee, {})

                    old_ratio = _safe_ratio(old_info)
                    new_ratio = _safe_ratio(new_info)

                    # Pull total SP values (fallback to 0 if missing)
                    old_total_sp = old_info.get("total_sp") or 0
                    new_total_sp = new_info.get("total_sp") or 0

                    # Estimated injected SP is simply the SP delta, clamped at 0
                    injected_est = max(0, new_total_sp - old_total_sp)

                    # Only flag when ratio increased and we have both ratios
                    if old_ratio is not None and new_ratio is not None and new_ratio > old_ratio:
                        # Format nicely with thousand separators and trimmed ratios
                        flaggs.append(
                            "- **{name}**:\n"
                            "  • Previous total SP: {old_sp:,}\n"
                            "  • New total SP: {new_sp:,}\n"
                            "  • Est. injected: **{inj_sp:,} SP**\n"
                            "  • SP/age ratio: {old_r:.2f} → **{new_r:.2f}**\n".format(
                                name=char_nameee,
                                old_sp=old_total_sp,
                                new_sp=new_total_sp,
                                inj_sp=injected_est,
                                old_r=old_ratio,
                                new_r=new_ratio,
                            )
                        )

                if flaggs:  # only send notification when at least one character’s ratio increased
                    sp_list = "".join(flaggs)
                    if instance.sp_inject_notify:
                        changes.append(f"## {get_pings('SP Injected')} Skill Injection detected:\n{sp_list}")

            status.sp_age_ratio_result = sp_age_ratio_result
            status.save()

            if status.has_awox_kills != has_awox or set(awox_links) != set(
                status.awox_kill_links or []):  # new awox activity?
                # detect new AWOX links and optionally raise a ticket
                # Compare and find new links
                old_links = set(status.awox_kill_links or [])
                new_links = set(awox_links) - old_links

                def format_awox_line(link):
                    details = awox_map.get(link)
                    if details:
                        return (f"- {link}\n"
                                f"  - Date: {details.get('date')}\n"
                                f"  - Value: {details.get('value')} ISK")
                    return f"- {link}"

                link_list = "\n".join(format_awox_line(link) for link in new_links)
                logger.info(f"{char_name} new links {link_list}")
                link_list3 = "\n".join(f"- {link}" for link in awox_links)
                logger.info(f"{char_name} new links {link_list3}")
                link_list2 = "\n".join(f"- {link}" for link in old_links)
                logger.info(f"{char_name} old links {link_list2}")
                if status.has_awox_kills != has_awox and has_awox:  # first time awox kills were spotted for this user
                    if not has_awox:
                        if instance.awox_notify:
                            changes.append(f"### AWOX Kill Status: 🟢")
                    status.has_awox_kills = has_awox
                    logger.info(f"{char_name} changed")
                if new_links:  # send notifications only for links not yet alerted on
                    if instance.awox_notify:
                        changes.append(f"###{get_pings('AwoX')} New AWOX Kill(s):\n{link_list}")
                    logger.info(f"{char_name} new links")
                    tcfg = TicketToolConfig.get_solo()
                    if tcfg.awox_monitor_enabled and time_in_corp(
                        user_id) >= 1:  # guardrail: only fire tickets for monitored corps
                        try:
                            try:
                                user = User.objects.get(id=user_id)
                                discord_id = get_discord_user_id(user)

                                ticket_message = f"<@&{tcfg.Role_ID}>,<@{discord_id}> detection indicates your involvement in an AWOX kill, please explain:\n{link_list}"
                                send_message(f"ticket for {instance.user} created, reason - AWOX Kill")
                                run_task_function.apply_async(
                                    args=["aa_bb.tasks_bot.create_compliance_ticket"],
                                    kwargs={
                                        "task_args": [instance.user.id, discord_id, "awox_kill", ticket_message],
                                        "task_kwargs": {}
                                    }
                                )
                            except Exception as e:
                                logger.error(e)
                                pass

                        except Exception as e:
                            logger.error(e)
                            pass
                old = set(status.awox_kill_links or [])
                new = set(awox_links) - old
                if new:  # merge newly seen links into the cached list
                    # notify
                    status.awox_kill_links = list(old | new)
                    status.updated = timezone.now()
                    status.save()

            if status.has_cyno != has_cyno or norm(cyno_result) != norm(status.cyno or {}):  # cyno readiness changed?

                # 1) Flag change for top-level boolean
                if status.has_cyno != has_cyno:  # flip the top-level boolean when overall readiness changes
                    if not has_cyno:
                        if instance.cyno_notify:
                            changes.append(f"### Cyno Status: 🟢")
                    status.has_cyno = has_cyno

                # 2) Grab the old vs. new JSON blobs
                old_cyno: dict = status.cyno or {}
                new_cyno: dict = cyno_result

                # Determine which character names actually changed
                changed_chars = []
                for char_namee, new_data in new_cyno.items():
                    old_data = old_cyno.get(char_namee, {})
                    old_filtered = {k: v for k, v in old_data.items() if
                                    k != 'age'}  # ignore 'age' helper field in comparisons
                    new_filtered = {k: v for k, v in new_data.items() if
                                    k != 'age'}  # ignore 'age' helper field in comparisons

                    if old_filtered != new_filtered:  # record only characters whose cyno skill blob changed
                        changed_chars.append(char_namee)

                # 3) If any changed, build one table per character
                if changed_chars:  # only build the verbose table output when someone’s cyno profile actually changed
                    # Mapping for display names
                    cyno_display = {
                        "s_cyno": "Cyno Skill",
                        "s_cov_cyno": "CovOps Cyno",
                        "s_recon": "Recon Ships",
                        "s_hic": "Heavy Interdiction",
                        "s_blops": "Black Ops",
                        "s_covops": "Covert Ops",
                        "s_brun": "Blockade Runners",
                        "s_sbomb": "Stealth Bombers",
                        "s_scru": "Strat Cruisers",
                        "s_expfrig": "Expedition Frigs",
                        "s_carrier": "Carriers",
                        "s_dread": "Dreads",
                        "s_fax": "FAXes",
                        "s_super": "Supers",
                        "s_titan": "Titans",
                        "s_jf": "JFs",
                        "s_rorq": "Rorqs",
                        "i_recon": "Has a Recon",
                        "i_hic": "Has a HIC",
                        "i_blops": "Has a Blops",
                        "i_covops": "Has a Covops",
                        "i_brun": "Has a Blockade Runner",
                        "i_sbomb": "Has a Bomber",
                        "i_scru": "Has a T3C",
                        "i_expfrig": "Has a Exp. Frig.",
                        "i_carrier": "Has a Carrier",
                        "i_dread": "Has a Dread",
                        "i_fax": "Has a FAX",
                        "i_super": "Has a Super",
                        "i_titan": "Has a Titan",
                        "i_jf": "Has a JF",
                        "i_rorq": "Has a Rorq",
                    }

                    # Column order
                    cyno_keys = [
                        "s_cyno", "s_cov_cyno", "s_recon", "s_hic", "s_blops",
                        "s_covops", "s_brun", "s_sbomb", "s_scru", "s_expfrig",
                        "s_carrier", "s_dread", "s_fax", "s_super", "s_titan", "s_jf", "s_rorq",
                        "i_recon", "i_hic", "i_blops", "i_covops", "i_brun",
                        "i_sbomb", "i_scru", "i_expfrig",
                        "i_carrier", "i_dread", "i_fax", "i_super", "i_titan", "i_jf", "i_rorq",
                    ]

                    if changed_chars:  # only build table output when specific characters changed
                        if instance.cyno_notify:
                            changes.append(f"###{get_pings('All Cyno Changes')} Changes in cyno capabilities detected:")

                    for charname in changed_chars:
                        old_entry = old_cyno.get(charname, {})
                        new_entry = new_cyno.get(charname, {})
                        anything = any(
                            val in (1, 2, 3, 4, 5)
                            for val in new_entry.values()
                        )
                        if anything == False:  # skip characters that have no meaningful cyno skills
                            continue
                        if new_entry.get("can_light", False) == True:  # highlight characters that can actively light cynos
                            pingrole = get_pings('Can Light Cyno')
                        else:
                            pingrole = get_pings('Cyno Update')
                        if instance.cyno_notify:
                            changes.append(f"- **{charname}**{pingrole}:")
                        table_lines = [
                            "(1 = trained but alpha, 2 = active)",
                            "Value                 | Old   | New",
                            "------------------------------------"
                        ]

                        for key in cyno_keys:
                            display = cyno_display.get(key, key)
                            old_val = str(old_entry.get(key, 0))
                            new_val = str(new_entry.get(key, 0))
                            if old_val != new_val:
                                if instance.cyno_notify:
                                    table_lines.append(f"{display.ljust(21)} | {old_val.ljust(7)} | {new_val.ljust(6)}")

                        # Show can_light as a summary at bottom
                        can_light_old = old_entry.get("can_light", False)
                        can_light_new = new_entry.get("can_light", False)
                        if instance.cyno_notify:
                            table_lines.append("")
                            table_lines.append(
                                f"{'Can Light'.ljust(21)} | "
                                f"{('Yes' if can_light_old else 'No').ljust(7)} | "
                                f"{('Yes' if can_light_new else 'No').ljust(6)}")

                        try:
                            cid = get_character_id(charname)
                            ev = EveCharacter.objects.get(character_id=cid)

                            corp_id = ev.corporation_id
                            corp_name = ev.corporation_name

                            corp_days = get_current_stint_days_in_corp(cid, corp_id)
                            corp_label = f"Time in {corp_name}"

                            table_lines.append(f"{corp_label:<21} | {corp_days} days")
                        except EveCharacter.DoesNotExist:
                            logger.warning(f"EveCharacter not found for {charname} (id={cid}), skipping corp time.")
                        except Exception as e:
                            logger.warning(f"Could not fetch corp time for {charname}: {e}")


                        table_block = "```\n" + "\n".join(table_lines) + "\n```"
                        if instance.cyno_notify:
                            changes.append(table_block)

                # 4) Save new blob
                status.cyno = new_cyno

            if status.has_skills != has_skills or skills_norm(skills_result) != skills_norm(
                status.skills or {}):  # skill list changed?
                # 1) If the boolean flag flipped, append the 🔴 / 🟢 as before
                if status.has_skills != has_skills:  # emit a coarse-grained flag when the threshold crosses zero/any skills
                    if not has_skills:
                        if instance.cyno_notify:
                            changes.append(f"### Skill Status: 🟢")
                    status.has_skills = has_skills

                # 2) Grab the old vs. new JSON blobs
                old_skills: dict = status.skills or {}
                new_skills: dict = skills_result

                # Determine which character names actually changed
                changed_chars = []

                def normalize_keys(d):
                    return {
                        str(k): v for k, v in d.items()
                        if str(k) != "total_sp"  # ignore total SP entry when diffing
                    }

                for character_name, new_data in new_skills.items():
                    # Defensive: ensure old_data is a dict; otherwise treat as empty
                    old_data = old_skills.get(character_name)
                    if not isinstance(old_data, dict):  # treat missing blobs as empty dicts
                        old_data = {}

                    # Defensive: ensure new_data is a dict as well
                    if not isinstance(new_data, dict):  # same safeguard for new data
                        new_data = {}

                    old_data_norm = normalize_keys(old_data)
                    new_data_norm = normalize_keys(new_data)

                    if old_data_norm != new_data_norm:  # record only characters whose skill payload changed
                        changed_chars.append(character_name)

                # 3) If any changed, build one table per character
                if changed_chars:
                    # A mapping from skill_id → human-readable name
                    skill_names = {
                        3426: "CPU Management",
                        21603: "Cyno Field Theory",
                        22761: "Recon Ships",
                        28609: "HICs",
                        28656: "Black Ops",
                        12093: "CovOps/SBs",
                        20533: "Capital Ships",
                        19719: "Blockade Runners",
                        30651: "Caldari T3Cs",
                        30652: "Gallente T3Cs",
                        30653: "Minmatar T3Cs",
                        30650: "Amarr T3Cs",
                        33856: "Expedition Frig",
                    }

                    # Keep the same order you gave, but dedupe 12093 once
                    ordered_skill_ids = [
                        3426, 21603, 22761, 28609, 28656,
                        12093, 20533, 19719,
                        30651, 30652, 30653, 30650, 33856,
                    ]

                    if changed_chars:  # preface the per-character tables with a summary line
                        if instance.cyno_notify:
                            changes.append(f"##{get_pings('skills')} Changes in skills detected:")

                    for charname in changed_chars:
                        raw_old = old_skills.get(charname)
                        old_entry = raw_old if isinstance(raw_old, dict) else {}

                        raw_new = new_skills.get(charname)
                        new_entry = raw_new if isinstance(raw_new, dict) else {}
                        anything = any(
                            (
                                new_entry.get(sid, {"trained": 0, "active": 0})["trained"] > 0
                                or
                                new_entry.get(sid, {"trained": 0, "active": 0})["active"] > 0
                            )
                            for sid in ordered_skill_ids
                        )
                        if anything == False:  # skip characters with zero relevant skills (just noise)
                            continue
                        logger.info(new_entry.values())

                        if instance.cyno_notify:
                            changes.append(f"- **{charname}**:")
                        table_lines = [
                            "Skill              | Old       | New",
                            "------------------------------------",
                        ]

                        for sid in ordered_skill_ids:
                            name = skill_names.get(sid, f"Skill ID {sid}")

                            old_skill = old_entry.get(str(sid), {"trained": 0, "active": 0})
                            new_skill = new_entry.get(sid, {"trained": 0, "active": 0})

                            if not isinstance(old_skill, dict):  # guard against malformed cache entries
                                old_skill = {"trained": 0, "active": 0}
                            if not isinstance(new_skill, dict):  # same safeguard for new data
                                new_skill = {"trained": 0, "active": 0}

                            old_tr = old_skill.get("trained", 0)
                            old_ac = old_skill.get("active", 0)
                            new_tr = new_skill.get("trained", 0)
                            new_ac = new_skill.get("active", 0)

                            if old_tr == new_tr and old_ac == new_ac:
                                continue

                            old_fmt = f"{old_tr}/{old_ac}"
                            new_fmt = f"{new_tr}/{new_ac}"
                            name_padded = name.ljust(18)

                            table_lines.append(
                                f"{name_padded} | {old_fmt.ljust(9)} | {new_fmt.ljust(8)}"
                            )

                        if len(table_lines) > 2:
                            table_block = "```\n" + "\n".join(table_lines) + "\n```"
                            if instance.cyno_notify:
                                changes.append(table_block)

                status.skills = new_skills
            if status.has_hostile_assets != has_hostile_assets or set(hostile_assets_result) != set(
                status.hostile_assets or []
            ):
                old_systems = set(status.hostile_assets or [])
                new_systems = set(hostile_assets_result) - old_systems

                # Build mapping: char -> list of (system, owner, ships)
                assets_by_char: dict[str, list[tuple[str, str, str]]] = {}

                for system in new_systems:
                    summary = hostile_assets_result.get(system, "")
                    owner, ships, chars = parse_hostile_summary(summary)

                    # If helper didn't give chars for some reason, fall back
                    if not chars:
                        chars = ["Unknown Character"]

                    for cname in chars:
                        assets_by_char.setdefault(cname, []).append(
                            (system, owner, ships)
                        )

                lines: list[str] = []
                for cname in sorted(assets_by_char.keys()):
                    lines.append(f"- {cname}")
                    for system, owner, ships in assets_by_char[cname]:
                        lines.append(f"  - {system} ({owner})")
                        if ships:
                            lines.append(f"    - {ships}")

                if lines:
                    link_list = "\n".join(lines)
                    logger.info(f"{char_name} new hostile assets:\n{link_list}")

                # Overall boolean flip
                if status.has_hostile_assets != has_hostile_assets:
                    if not has_hostile_assets:
                        if instance.asset_notify:
                            changes.append("### Hostile Asset Status: 🟢")
                    logger.info(f"{char_name} hostile asset status changed")

                # Only add a "New Hostile Assets" section when there are actually new systems
                if new_systems and lines:
                    if instance.asset_notify:
                        changes.append(f"###{get_pings('New Hostile Assets')} New Hostile Assets:\n{link_list}")
                    logger.info(f"{char_name} new hostile asset systems: {', '.join(sorted(new_systems))}")

                status.has_hostile_assets = has_hostile_assets
                status.hostile_assets = hostile_assets_result

            if status.has_hostile_clones != has_hostile_clones or set(hostile_clones_result) != set(
                status.hostile_clones or []
            ):
                old_systems = set(status.hostile_clones or [])
                new_systems = set(hostile_clones_result) - old_systems

                # Build mapping: char -> list of (system, owner)
                clones_by_char: dict[str, list[tuple[str, str]]] = {}

                for system in new_systems:
                    summary = hostile_clones_result.get(system, "")
                    owner, _ships, chars = parse_hostile_summary(summary)

                    if not chars:
                        chars = ["Unknown Character"]

                    for cname in chars:
                        clones_by_char.setdefault(cname, []).append(
                            (system, owner)
                        )

                lines: list[str] = []
                for cname in sorted(clones_by_char.keys()):
                    lines.append(f"- {cname}")
                    for system, owner in clones_by_char[cname]:
                        lines.append(f"  - {system} ({owner})")

                if lines:
                    link_list = "\n".join(lines)
                    logger.info(f"{char_name} new hostile clones:\n{link_list}")

                # Overall boolean flip
                if status.has_hostile_clones != has_hostile_clones:
                    if not has_hostile_clones:
                        if instance.clone_notify:
                            changes.append("### Hostile Clone Status: 🟢")
                    logger.info(f"{char_name} hostile clone status changed")

                if new_systems and lines:
                    if instance.clone_notify:
                        changes.append(f"###{get_pings('New Hostile Clones')} New Hostile Clone(s):\n{link_list}")
                    logger.info(f"{char_name} new hostile clone systems: {', '.join(sorted(new_systems))}")

                status.has_hostile_clones = has_hostile_clones
                status.hostile_clones = hostile_clones_result

            if status.has_sus_contacts != has_sus_contacts or set(sus_contacts_result) != set(
                as_dict(status.sus_contacts) or {}):  # suspect contacts changed?
                old_contacts = as_dict(status.sus_contacts) or {}
                # normalized_old = { str(cid): v for cid, v in status.sus_contacts.items() }
                # normalized_new = { str(cid): v for cid, v in sus_contacts_result.items() }

                old_ids = set(as_dict(status.sus_contacts).keys())
                new_ids = set(sus_contacts_result.keys())
                new_links = new_ids - old_ids
                if new_links:  # highlight only contacts not previously reported
                    link_list = "\n".join(
                        f"🔗 {sus_contacts_result[cid]}" for cid in new_links
                    )
                    logger.info(f"{char_name} new assets:\n{link_list}")

                if old_ids:  # optional debug log for existing entries
                    old_link_list = "\n".join(
                        f"🔗 {old_contacts[cid]}" for cid in old_ids if cid in old_contacts
                    )
                    logger.info(f"{char_name} old assets:\n{old_link_list}")

                if status.has_sus_contacts != has_sus_contacts:  # flag boolean flip
                    if not has_sus_contacts:
                        if instance.contact_notify:
                            changes.append(f"### Suspicious Contact Status: 🟢")
                logger.info(f"{char_name} status changed")

                if new_links:  # include the new contact entries in the summary
                    if instance.contact_notify:
                        changes.append(f"### New Suspicious Contacts:")
                        for cid in new_links:
                            res = sus_contacts_result[cid]
                            ping = get_pings('New Suspicious Contacts')
                            if res.startswith("- A -"):  # skip ping for alliance-only entries
                                ping = ""
                            changes.append(f"{res} {ping}")

                status.has_sus_contacts = has_sus_contacts
                status.sus_contacts = sus_contacts_result

            if status.has_sus_contracts != has_sus_contracts or set(sus_contracts_result) != set(
                as_dict(status.sus_contracts) or {}):  # suspicious contracts changed?
                old_contracts = as_dict(status.sus_contracts) or {}
                # normalized_old = { str(cid): v for cid, v in status.sus_contacts.items() }
                # normalized_new = { str(cid): v for cid, v in sus_contacts_result.items() }

                old_ids = set(as_dict(status.sus_contracts).keys())
                new_ids = set(sus_contracts_result.keys())
                new_links = new_ids - old_ids
                if new_links:  # only surface contracts not yet alerted on
                    link_list = "\n".join(
                        f"🔗 {sus_contracts_result[issuer_id]}" for issuer_id in new_links
                    )
                    logger.info(f"{char_name} new assets:\n{link_list}")

                if old_ids:  # optional logging for previous entries
                    old_link_list = "\n".join(
                        f"🔗 {old_contracts[issuer_id]}" for issuer_id in old_ids if issuer_id in old_contracts
                    )
                    logger.info(f"{char_name} old assets:\n{old_link_list}")

                if status.has_sus_contracts != has_sus_contracts:  # summarize boolean change
                    if not has_sus_contracts:
                        if instance.contract_notify:
                            changes.append(f"## Suspicious Contract Status: 🟢")
                logger.info(f"{char_name} status changed")

                if new_links:  # write each new contract entry to the report
                    if instance.contract_notify:
                        changes.append(f"## New Suspicious Contracts:")
                        for issuer_id in new_links:
                            res = sus_contracts_result[issuer_id]
                            ping = get_pings('New Suspicious Contracts')
                            if res.startswith("- A -"):  # skip ping for alliance-level alerts
                                ping = ""
                            changes.append(f"{res} {ping}")

                status.has_sus_contracts = has_sus_contracts
                status.sus_contracts = sus_contracts_result

            if status.has_sus_mails != has_sus_mails or set(sus_mails_result) != set(
                as_dict(status.sus_mails) or {}):  # suspicious mails changed?
                old_mails = as_dict(status.sus_mails) or {}
                # normalized_old = { str(cid): v for cid, v in status.sus_contacts.items() }
                # normalized_new = { str(cid): v for cid, v in sus_contacts_result.items() }

                old_ids = set(as_dict(status.sus_mails).keys())
                new_ids = set(sus_mails_result.keys())
                new_links = new_ids - old_ids
                if new_links:  # only highlight unseen mail threads
                    link_list = "\n".join(
                        f"🔗 {sus_mails_result[issuer_id]}" for issuer_id in new_links
                    )
                    logger.info(f"{char_name} new assets:\n{link_list}")

                if old_ids:  # optional logging for previous entries
                    old_link_list = "\n".join(
                        f"🔗 {old_mails[issuer_id]}" for issuer_id in old_ids if issuer_id in old_mails
                    )
                    logger.info(f"{char_name} old assets:\n{old_link_list}")

                if status.has_sus_mails != has_sus_mails:  # summarize boolean change
                    if not has_sus_mails:
                        if instance.mail_notify:
                            changes.append(f"### Suspicious Mail Status: 🟢")
                logger.info(f"{char_name} status changed")

                if new_links:  # enumerate the new mail entries for the report
                    if instance.mail_notify:
                        changes.append(f"### New Suspicious Mails:")
                        for issuer_id in new_links:
                            res = sus_mails_result[issuer_id]
                            ping = get_pings('New Suspicious Mails')
                            if res.startswith("- A -"):  # skip ping for alliance-level alerts
                                ping = ""
                            changes.append(f"{res} {ping}")

                status.has_sus_mails = has_sus_mails
                status.sus_mails = sus_mails_result

            if status.has_sus_trans != has_sus_trans or set(sus_trans_result) != set(
                as_dict(status.sus_trans) or {}):  # suspicious wallet txns changed?
                old_trans = as_dict(status.sus_trans) or {}
                # normalized_old = { str(cid): v for cid, v in status.sus_contacts.items() }
                # normalized_new = { str(cid): v for cid, v in sus_contacts_result.items() }

                old_ids = set(as_dict(status.sus_trans).keys())
                new_ids = set(sus_trans_result.keys())
                new_links = new_ids - old_ids
                if new_links:  # only highlight newly detected transactions
                    link_list = "\n".join(
                        f"{sus_trans_result[issuer_id]}" for issuer_id in new_links
                    )
                    logger.info(f"{char_name} new trans:\n{link_list}")

                if old_ids:
                    old_link_list = "\n".join(
                        f"{old_trans[issuer_id]}" for issuer_id in old_ids if issuer_id in old_trans
                    )
                    logger.info(f"{char_name} old trans:\n{old_link_list}")

                if status.has_sus_trans != has_sus_trans:
                    if not has_sus_trans:
                        if instance.transaction_notify:
                            changes.append(f"## Suspicious Transactions Status: 🟢")
                logger.info(f"{char_name} status changed")
                if new_links:
                    if instance.transaction_notify:
                        changes.append(f"### New Suspicious Transactions{get_pings('New Suspicious Transactions')}:\n{link_list}")
                status.has_sus_trans = has_sus_trans
                status.sus_trans = sus_trans_result

            if send_notifications and changes:
                """
                Build all embed chunks and hand them off to a dedicated
                send task so Discord messages are serialized and never
                interleave between users.
                """
                logger.info(
                    "[%s] Preparing %d change blocks for Discord...",
                    char_name,
                    len(changes),
                )

                all_chunks: list[list[str]] = []

                # 1) Overall header – almost always a tiny single-chunk embed
                header_lines = [f"‼️ Status change detected for {char_name}"]
                for header_chunk in _chunk_embed_lines(header_lines, max_chars=1900):
                    all_chunks.append(header_chunk)

                # 2) One or more embeds per change block, chunked by char count
                for chunk in changes:
                    raw_lines = [ln for ln in chunk.split("\n") if ln.strip()]

                    for body_chunk in _chunk_embed_lines(raw_lines, max_chars=1900):
                        logger.info(
                            "[%s] Prepared embed chunk with %d lines",
                            char_name,
                            len(body_chunk),
                        )
                        all_chunks.append(body_chunk)

                if all_chunks:
                    logger.info(
                        "[%s] Enqueuing %d embed chunks to BB_send_discord_notifications",
                        char_name,
                        len(all_chunks),
                    )
                    BB_send_discord_notifications.delay(char_name, all_chunks)


            status.updated = timezone.now()
            status.save()

            logger.info(f"END Update for user: {char_name} (ID: {user_id}) - Success")
            break

        except OperationalError as e:
            code = e.args[0] if e.args else None
            if code == 1213 or "deadlock" in str(e).lower():
                delay = 0.5 * (attempt + 1)
                logger.warning(
                    f"Deadlock while processing {char_name} "
                    f"(attempt {attempt + 1}/3); sleeping {delay:.1f}s before retry."
                )
                time.sleep(delay)
                # after last attempt, give up on this user but keep the overall stream alive
                if attempt == 2:
                    logger.error(
                        f"Skipping {char_name} after repeated deadlocks."
                    )
                continue
            # not a deadlock → re-raise and let outer handler deal with it
            raise
        except Exception as e:
            logger.error(f"Failed to update user {char_name}: {e}", exc_info=True)
            raise


@shared_task
def BB_run_regular_updates():
    """
    Main scheduled job that refreshes BigBrother cache entries.

    Workflow:
      1. Ensure the singleton config exists and derive the primary corp/alliance
         from a superuser alt.
      2. Iterate through every user returned by `get_users()`.
      3. For each user, recalculates every signal (awox, cyno, skills, hostiles,
         etc.), compares against the previous snapshot, and appends human-readable
         change notes to `changes`.
      4. When certain checks flip (clone state, skill injections, awox kills),
         Discord notifications and optional compliance tickets are issued.
      5. Persist the updated `UserStatus` row so the dashboard stays in sync.

    Section overview:
      • Config bootstrap: lines 22–58 – ensure `BigBrotherConfig` is populated.
      • User iteration: lines 60–138 – loop through every member returned by
        `get_users`, fetch all relevant check data, and compute summary flags.
      • Change detection: lines 140 onwards – compare each check’s result with the
        previous values stored on `UserStatus` (clone states, SP injection, awox
        kills, cyno readiness, skill summaries, hostile contacts, etc.). Each block
        builds `changes` entries and updates the `UserStatus` fields accordingly.
      • Notifications/tickets: sprinkled throughout the change detection case
        statements (e.g., awox block) – when a change warrants action a Discord
        webhook is pinged via `get_pings` and compliance tickets may be opened.
      • Persistence: after all comparisons, save `status` so the UI reflects the
        latest state even if no Discord messages were sent this run.
    """
    instance = BigBrotherConfig.get_solo()
    instance.is_active = True

    try:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        # find a superuser’s main to anchor corp/alliance fields
        superusers = User.objects.filter(is_superuser=True)
        char = EveCharacter.objects.filter(
            character_ownership__user__in=superusers
        ).first()

        if not char:  # no superuser alt yet → fall back to first available character
            char = EveCharacter.objects.all().first()
        if char:  # only populate config when a character is available to inspect
            corp_name = char.corporation_name
            alliance_id = char.alliance_id or None
            alliance_name = char.alliance_name if alliance_id else None  # unaffiliated corps report None for alliance

            instance.main_corporation_id = char.corporation_id
            instance.main_corporation = corp_name
            instance.main_alliance_id = alliance_id
            instance.main_alliance = alliance_name

        instance.save()

        # walk each eligible user and rebuild their status snapshot
        if instance.is_active:  # skip user iteration entirely when plugin disabled/unlicensed
            users = list(get_users())
            total_users = len(users)
            logger.info(
                f"BB_run_regular_updates: Dispatching updates for {total_users} users (staggered)."
            )

            if total_users == 0:
                return

            # We want to spread the work roughly across 1 hour (3600s)
            # so we don't spike CPU at the top of the hour.
            from datetime import timedelta

            window_seconds = 3600
            # minimum spacing between users, so tasks don't all land at the same second
            if total_users < 720:
                min_spacing = 5
            elif total_users < 900:
                min_spacing = 4
            elif total_users < 1200:
                min_spacing = 3
            elif total_users < 1800:
                min_spacing = 2
            else: # good upto 3600 users.
                min_spacing = 1

            # Compute spacing so that N users fit into ~window_seconds
            spacing = max(min_spacing, window_seconds // max(total_users, 1))

            now = timezone.now()

            for index, char_name in enumerate(users):
                user_id = get_user_id(char_name)
                if not user_id:  # defensive: skip orphaned mains lacking a user id
                    continue

                # Schedule each user with an increasing ETA so the load is flattened
                offset = index * spacing
                eta = now + timedelta(seconds=offset)

                logger.info(
                    f"Scheduling BB_update_single_user for {char_name} (id={user_id}) "
                    f"in {offset}s at {eta.isoformat()}."
                )

                BB_update_single_user.apply_async(
                    args=(user_id, char_name),
                    eta=eta,
                )

    except Exception as e:
        logger.error("Task failed", exc_info=True)
        instance.is_active = True
        instance.save()
        send_message(
            f"#{get_pings('Error')} Big Brother encountered an unexpected error"
        )

        # send the error in chunks to keep within discord limits and keep it in code blocks

        tb_str = traceback.format_exc()
        max_chunk = 1000
        start = 0
        length = len(tb_str)

        while start < length:
            end = min(start + max_chunk, length)
            if end < length:  # maintain readable chunks whenever possible
                nl = tb_str.rfind('\n', start, end)
                if nl != -1 and nl > start:  # break on newline if it exists inside this chunk
                    end = nl + 1
            chunk = tb_str[start:end]
            time.sleep(0.25)
            send_message(f"```{chunk}```")
            start = end

    from django_celery_beat.models import PeriodicTask
    task_name = 'BB run regular updates'
    task = PeriodicTask.objects.filter(name=task_name).first()
    if not task.enabled:  # inform admins when the periodic task finished its initial run
        send_message("initial run of the Big Brother task has finished, you can now enable the task")

@shared_task()
def BB_send_discord_notifications(subject: str, chunks: list[list[str]]) -> None:
    """
    Dedicated task to send Discord embeds for BigBrother.

    - subject: usually the character name
    - chunks: list of "lines lists" – each inner list becomes one embed body

    Run this on a single-worker queue (concurrency=1) so embeds never
    interleave between users or checks.
    """
    logger.info(
        "[BB_SEND] Dispatching %d embed chunks for %s",
        len(chunks),
        subject,
    )

    for idx, lines in enumerate(chunks):
        logger.debug(
            "[BB_SEND] Sending chunk %d/%d for %s (lines=%d)",
            idx + 1,
            len(chunks),
            subject,
            len(lines),
        )
        send_status_embed(
            subject=subject,
            lines=lines,
            override_title="",  # keep titles minimal; content is in the body
        )
        time.sleep(0.25)  # tiny delay to be nice to the webhook
def _merge_id_text(existing_text: str | None, new_ids: set[int]) -> str:
    existing_ids: set[int] = set()

    if existing_text:
        for part in existing_text.split(","):
            part = part.strip()
            if part.isdigit():
                existing_ids.add(int(part))

    combined = existing_ids | set(new_ids)
    if not combined:
        return ""

    return ",".join(str(i) for i in sorted(combined))


def _parse_id_text(existing_text: str | None) -> set[int]:
    ids: set[int] = set()
    if not existing_text:
        return ids

    for part in str(existing_text).split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def _get_id_set(cfg, field_name: str, id_attr: str) -> set[int]:
    """
    Supports BOTH:
      - ManyToMany manager (has .values_list)
      - TextField CSV of IDs
    """
    val = getattr(cfg, field_name, None)
    if val is None:
        return set()

    if hasattr(val, "values_list"):
        return set(val.values_list(id_attr, flat=True))

    # TextField CSV path
    return _parse_id_text(val)


def _add_ids(cfg, field_name: str, ids: set[int]) -> bool:
    """
    Supports BOTH:
      - ManyToMany manager (has .add)
      - TextField CSV of IDs
    Returns True if it changed something.
    """
    if not ids:
        return False

    val = getattr(cfg, field_name, None)
    if val is None:
        return False

    if hasattr(val, "add"):
        val.add(*list(ids))
        return True

    # TextField CSV path
    current = getattr(cfg, field_name)
    merged = _merge_id_text(current, ids)
    if (current or "") != (merged or ""):
        setattr(cfg, field_name, merged)
        return True

    return False


def _remove_ids(cfg, field_name: str, ids: set[int]) -> bool:
    """
    Remove a set of IDs from a config field that may be:
      - A ManyToMany manager, or
      - A CSV TextField of IDs.

    Only removes the given IDs; anything else (e.g. manually
    added values that were never imported) is preserved.
    """
    if not ids:
        return False

    val = getattr(cfg, field_name, None)
    if val is None:
        return False

    if hasattr(val, "remove"):
        # ManyToMany path: remove by ID
        val.remove(*list(ids))
        return True

    # TextField CSV path
    current_ids = _parse_id_text(getattr(cfg, field_name))
    new_ids = current_ids - set(ids)
    new_text = ",".join(str(i) for i in sorted(new_ids)) if new_ids else ""

    if (getattr(cfg, field_name) or "") != (new_text or ""):
        setattr(cfg, field_name, new_text)
        return True

    return False


@shared_task(bind=True, name="aa_bb.tasks.BB_sync_contacts_from_aa_contacts")
def BB_sync_contacts_from_aa_contacts(self):
    """
    Sync standings from aa-contacts into BigBrother hostiles/members/whitelists.

    Behaviour:
      - No-ops if aa_contacts is not installed or contacts_source_alliances
        is empty / missing.
      - Adds NEW contacts from aa-contacts into the correct sets.
      - Removes contacts that were previously imported from aa-contacts but
        no longer appear there.
      - Never touches IDs that were manually added directly in BigBrother.
    """
    if not AA_CONTACTS_INSTALLED:
        return

    from .models import BigBrotherConfig

    try:
        cfg = BigBrotherConfig.get_solo()
    except Exception:
        return

    source_alliances_field = getattr(cfg, "contacts_source_alliances", None)
    if source_alliances_field is None or not source_alliances_field.exists():
        return

    try:
        from aa_contacts.models import AllianceContact
    except Exception:
        return

    # Existing config sets (works for M2M or CSV TextFields)
    hostile_alliances   = _get_id_set(cfg, "hostile_alliances", "alliance_id")
    hostile_corps       = _get_id_set(cfg, "hostile_corporations", "corporation_id")
    member_alliances    = _get_id_set(cfg, "member_alliances", "alliance_id")
    member_corps        = _get_id_set(cfg, "member_corporations", "corporation_id")
    whitelist_alliances = _get_id_set(cfg, "whitelist_alliances", "alliance_id")
    whitelist_corps     = _get_id_set(cfg, "whitelist_corporations", "corporation_id")

    neutral_mode = getattr(cfg, "contacts_handle_neutrals", "ignore")

    # What aa-contacts says *now*
    new_hostile_alliances: set[int] = set()
    new_hostile_corps: set[int] = set()
    new_member_alliances: set[int] = set()
    new_member_corps: set[int] = set()
    new_whitelist_alliances: set[int] = set()
    new_whitelist_corps: set[int] = set()

    for src_alliance in source_alliances_field.all():
        contacts_qs = AllianceContact.objects.filter(alliance=src_alliance)
        for c in contacts_qs.iterator():
            target_id = int(c.contact_id)

            if c.contact_type == c.ContactTypeOptions.ALLIANCE:
                if c.standing > 0:
                    new_member_alliances.add(target_id)
                elif c.standing < 0:
                    new_hostile_alliances.add(target_id)
                else:
                    if neutral_mode == "hostile":
                        new_hostile_alliances.add(target_id)
                    elif neutral_mode == "whitelist":
                        new_whitelist_alliances.add(target_id)

            elif c.contact_type == c.ContactTypeOptions.CORPORATION:
                if c.standing > 0:
                    new_member_corps.add(target_id)
                elif c.standing < 0:
                    new_hostile_corps.add(target_id)
                else:
                    if neutral_mode == "hostile":
                        new_hostile_corps.add(target_id)
                    elif neutral_mode == "whitelist":
                        new_whitelist_corps.add(target_id)

    # Previous import snapshot (what we imported last time)
    cache = getattr(cfg, "contacts_import_cache", {}) or {}
    old_member_alliances    = set(cache.get("member_alliances", []))
    old_member_corps        = set(cache.get("member_corps", []))
    old_hostile_alliances   = set(cache.get("hostile_alliances", []))
    old_hostile_corps       = set(cache.get("hostile_corps", []))
    old_whitelist_alliances = set(cache.get("whitelist_alliances", []))
    old_whitelist_corps     = set(cache.get("whitelist_corps", []))

    changed = False

    # ---------- ADDITIONS ----------
    add_member_alliances = {
        a for a in new_member_alliances
        if a not in member_alliances and a not in whitelist_alliances
    }
    changed |= _add_ids(cfg, "member_alliances", add_member_alliances)

    add_member_corps = {
        c for c in new_member_corps
        if c not in member_corps and c not in whitelist_corps
    }
    changed |= _add_ids(cfg, "member_corporations", add_member_corps)

    add_hostile_alliances = {
        a for a in new_hostile_alliances
        if a not in hostile_alliances and a not in whitelist_alliances
    }
    changed |= _add_ids(cfg, "hostile_alliances", add_hostile_alliances)

    add_hostile_corps = {
        c for c in new_hostile_corps
        if c not in hostile_corps and c not in whitelist_corps
    }
    changed |= _add_ids(cfg, "hostile_corporations", add_hostile_corps)

    add_whitelist_alliances = {
        a for a in new_whitelist_alliances
        if a not in whitelist_alliances
    }
    changed |= _add_ids(cfg, "whitelist_alliances", add_whitelist_alliances)

    add_whitelist_corps = {
        c for c in new_whitelist_corps
        if c not in whitelist_corps
    }
    changed |= _add_ids(cfg, "whitelist_corporations", add_whitelist_corps)

    # ---------- REMOVALS (ONLY IDs WE PREVIOUSLY IMPORTED) ----------
    remove_member_alliances = old_member_alliances - new_member_alliances
    remove_member_corps     = old_member_corps - new_member_corps
    remove_hostile_alliances = old_hostile_alliances - new_hostile_alliances
    remove_hostile_corps     = old_hostile_corps - new_hostile_corps
    remove_whitelist_alliances = old_whitelist_alliances - new_whitelist_alliances
    remove_whitelist_corps     = old_whitelist_corps - new_whitelist_corps

    changed |= _remove_ids(cfg, "member_alliances", remove_member_alliances)
    changed |= _remove_ids(cfg, "member_corporations", remove_member_corps)
    changed |= _remove_ids(cfg, "hostile_alliances", remove_hostile_alliances)
    changed |= _remove_ids(cfg, "hostile_corporations", remove_hostile_corps)
    changed |= _remove_ids(cfg, "whitelist_alliances", remove_whitelist_alliances)
    changed |= _remove_ids(cfg, "whitelist_corporations", remove_whitelist_corps)

    # ---------- UPDATE IMPORT CACHE ----------
    new_cache = {
        "member_alliances": sorted(new_member_alliances),
        "member_corps": sorted(new_member_corps),
        "hostile_alliances": sorted(new_hostile_alliances),
        "hostile_corps": sorted(new_hostile_corps),
        "whitelist_alliances": sorted(new_whitelist_alliances),
        "whitelist_corps": sorted(new_whitelist_corps),
    }

    if cache != new_cache:
        cfg.contacts_import_cache = new_cache
        changed = True

    if changed:
        cfg.save()
