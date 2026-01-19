from django.db.utils import OperationalError
import time
import traceback
import gc
from django.utils import timezone
from django.db.models import Q
from django.db import close_old_connections
from celery import shared_task, current_app

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
    send_status_embed,
    _chunk_embed_lines,
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





@shared_task(time_limit=7200)
def BB_update_single_user(user_id, char_name):
    """
    Process updates for a single user.
    Broken out from BB_run_regular_updates for scalability.
    """
    # Close old DB connections to prevent memory leaks
    close_old_connections()

    logger.debug(f"✅  [AA-BB] - [BB_update_single_user] - START Update for user: {char_name} (ID: {user_id})")

    instance = BigBrotherConfig.get_solo()
    if not instance.is_active:
        logger.info(f"ℹ️  [AA-BB] - [BB_update_single_user] - BigBrother inactive. Skipping update for {char_name}.")
        close_old_connections()
        return

    User = get_user_model()
    try:
        user_obj = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"ℹ️  [AA-BB] - [BB_update_single_user] - User {user_id} not found.")
        close_old_connections()
        return

    limit_notifications = False

    if instance.limit_to_main_corp:
        profile = getattr(user_obj, 'profile', None)
        main_char_obj = getattr(profile, 'main_character', None) if profile else None
        is_main_corp = (main_char_obj and main_char_obj.corporation_id == instance.main_corporation_id)
        limit_notifications = not is_main_corp


    # Retry logic previously inside the main loop
    from .app_settings import get_safe_entities, get_user_characters, get_entity_info
    safe_entities = get_safe_entities()
    user_chars = get_user_characters(user_id)
    entity_info_cache = {}
    now_ts = timezone.now()
    for cid in user_chars.keys():
        entity_info_cache[cid] = get_entity_info(cid, now_ts)

    for attempt in range(3):
        try:
            # pingroleID = instance.pingroleID # Unused variable?

            logger.info(f"✅  [AA-BB] - [BB_update_single_user] - [{char_name}] Fetching Cyno Info...")
            cyno_result = get_user_cyno_info(user_id, cfg=instance)

            logger.info(f"✅  [AA-BB] - [BB_update_single_user] - [{char_name}] Fetching Skill Info...")
            skills_result = get_multiple_user_skill_info(user_id, skill_ids)

            logger.info(f"✅  [AA-BB] - [BB_update_single_user] - [{char_name}] Determining Character State...")
            state_result = determine_character_state(user_id, True, cfg=instance)

            logger.info(f"✅  [AA-BB] - [BB_update_single_user] - [{char_name}] Fetching AWOX Links...")
            awox_data = get_awox_kill_links(user_id, force_refresh=True)
            awox_links = [x["link"] for x in awox_data]
            attacker_links_all = [x["link"] for x in awox_data if x.get("is_attacker")]
            awox_map = {x["link"]: x for x in awox_data}

            logger.info(f"✅  [AA-BB] - [BB_update_single_user] - [{char_name}] Fetching Hostile Clones...")
            hostile_clones_result = get_hostile_clone_locations(user_id, cfg=instance, safe_entities=safe_entities, entity_info_cache=entity_info_cache)

            logger.info(f"✅  [AA-BB] - [BB_update_single_user] - [{char_name}] Fetching Hostile Assets...")

            hostile_assets_result = get_hostile_asset_locations(user_id, cfg=instance, safe_entities=safe_entities, entity_info_cache=entity_info_cache)

            logger.info(f"✅  [AA-BB] - [BB_update_single_user] - [{char_name}] Fetching Sus Contacts/Contracts/Mails/Trans...")
            sus_contacts_result = {str(cid): v for cid, v in get_user_hostile_notifications(user_id, cfg=instance, safe_entities=safe_entities, entity_info_cache=entity_info_cache).items()}
            sus_contracts_result = {str(issuer_id): v for issuer_id, v in get_user_hostile_contracts(user_id, cfg=instance, safe_entities=safe_entities, entity_info_cache=entity_info_cache).items()}
            sus_mails_result = {str(issuer_id): v for issuer_id, v in get_user_hostile_mails(user_id, cfg=instance, safe_entities=safe_entities, entity_info_cache=entity_info_cache).items()}
            sus_trans_result = {str(issuer_id): v for issuer_id, v in get_user_hostile_transactions(user_id, cfg=instance, safe_entities=safe_entities, entity_info_cache=entity_info_cache).items()}

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

            logger.info(f"✅  [AA-BB] - [BB_update_single_user] - [{char_name}] Processing SP Ratios...")
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
                entry.get(sid, {}).get("trained", 0) > 0 or entry.get(sid, {}).get("active", 0) > 0
                for entry in skills_result.values()
                for sid in skill_ids
            )

            has_awox = bool(attacker_links_all)
            has_hostile_clones = bool(hostile_clones_result)
            has_hostile_assets = bool(hostile_assets_result)
            has_sus_contacts = bool(sus_contacts_result)
            has_sus_contracts = bool(sus_contracts_result)
            has_sus_mails = bool(sus_mails_result)
            has_sus_trans = bool(sus_trans_result)

            # load (or create) cached status so diffs apply correctly
            status, created = UserStatus.objects.get_or_create(user_id=user_id)

            logger.info(f"✅  [AA-BB] - [BB_update_single_user] - [{char_name}] Status loaded (created={created}). Calculating changes...")

            changes = []
            status_changed = created  # Track if we need to save (always save if newly created)

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
                    if instance.clone_state_notify:
                        changes.append(f"###{pinggg} Clone state change detected:{''.join(flagggs)}")
                    status.clone_status = state_result
                    status_changed = True

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

            # Only update sp_age_ratio if it actually changed
            if sp_age_ratio_result != (status.sp_age_ratio_result or {}):
                status.sp_age_ratio_result = sp_age_ratio_result
                status_changed = True

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
                                f"   - Date: {details.get('date')}\n"
                                f"   - Value: {details.get('value')} ISK")
                    return f"- {link}"

                link_list = "\n".join(format_awox_line(link) for link in new_links)
                logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} new links {link_list}")
                link_list3 = "\n".join(f"- {link}" for link in awox_links)
                logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} new links {link_list3}")
                link_list2 = "\n".join(f"- {link}" for link in old_links)
                logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} old links {link_list2}")
                if status.has_awox_kills != has_awox:
                    if not has_awox:
                        if instance.awox_notify:
                            changes.append(f"### AWOX Kill Status: 🟢")
                    status.has_awox_kills = has_awox
                    status_changed = True
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} changed awox status to {has_awox}")
                if new_links:  # send notifications only for links not yet alerted on
                    # Identify which of the new links the user was an attacker in
                    attacker_links = [
                        link for link in new_links
                        if awox_map.get(link, {}).get("is_attacker", False)
                    ]
                    attacker_link_list = "\n".join(format_awox_line(link) for link in attacker_links)

                    if attacker_links:  # send notifications only when the user is the aggressor
                        if instance.awox_notify:
                            changes.append(f"###{get_pings('AwoX')} New AWOX Kill(s):\n{attacker_link_list}")
                        logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} new attacker links")
                        tcfg = TicketToolConfig.get_solo()
                        if not limit_notifications and tcfg.awox_monitor_enabled and time_in_corp(
                            user_id) >= 1:  # guardrail: only fire tickets for monitored corps
                            try:
                                from .tasks_tickets import ensure_ticket
                                ensure_ticket(status.user, "awox_kill", details=attacker_link_list)
                            except Exception as e:
                                logger.error(f"ℹ️  [AA-BB] - [BB_update_single_user] - {e}")
                                pass
                old = set(status.awox_kill_links or [])
                new = set(awox_links) - old
                if new:  # merge newly seen links into the cached list
                    # notify
                    status.awox_kill_links = list(old | new)
                    status_changed = True

            if status.has_cyno != has_cyno or norm(cyno_result) != norm(status.cyno or {}):  # cyno readiness changed?

                # 1) Flag change for top-level boolean
                if status.has_cyno != has_cyno:  # flip the top-level boolean when overall readiness changes
                    # User: Only trigger if "Can Light" = True (has_cyno is based on can_light)
                    status.has_cyno = has_cyno
                    status_changed = True

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

                    cyno_updates = []

                    for charname in changed_chars:
                        old_entry = old_cyno.get(charname, {})
                        new_entry = new_cyno.get(charname, {})

                        # User: Only trigger if "Can Light" = True
                        if not new_entry.get("can_light", False):
                            continue

                        # User: Never trigger if one of the "has ship" goes from True to False
                        ship_lost = False
                        for key in cyno_keys:
                            if key.startswith("i_"):
                                if int(old_entry.get(key, 0)) > int(new_entry.get(key, 0)):
                                    ship_lost = True
                                    break
                        if ship_lost:
                            continue

                        anything = any(
                            val in (1, 2, 3, 4, 5)
                            for val in new_entry.values()
                        )
                        if anything == False:  # skip characters that have no meaningful cyno skills
                            continue

                        pingrole = get_pings('Can Light Cyno')
                        cyno_updates.append(f"- **{charname}**{pingrole}:")

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
                                table_lines.append(f"{display.ljust(21)} | {old_val.ljust(7)} | {new_val.ljust(6)}")

                        # Show can_light as a summary at bottom
                        can_light_old = old_entry.get("can_light", False)
                        can_light_new = new_entry.get("can_light", False)
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
                            logger.warning(f"ℹ️  [AA-BB] - [BB_update_single_user] - EveCharacter not found for {charname} (id={cid}), skipping corp time.")
                        except Exception as e:
                            logger.warning(f"ℹ️  [AA-BB] - [BB_update_single_user] - Could not fetch corp time for {charname}: {e}")

                        table_block = "```\n" + "\n".join(table_lines) + "\n```"
                        cyno_updates.append(table_block)

                    if cyno_updates and instance.cyno_notify:
                        now_ts = timezone.now().strftime('%Y-%m-%d %H:%M:%S')
                        changes.append(f"###{get_pings('All Cyno Changes')} Changes in cyno capabilities detected ({now_ts} UTC):")
                        changes.extend(cyno_updates)

                # 4) Save new blob
                status.cyno = new_cyno
                status_changed = True

            if status.has_skills != has_skills or skills_norm(skills_result) != skills_norm(
                status.skills or {}):  # skill list changed?
                # 1) If the boolean flag flipped
                if status.has_skills != has_skills:
                    # User: trigger ONLY for new skills (has_skills crossing 0 to 1 handled in character loop)
                    status.has_skills = has_skills
                    status_changed = True

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

                    skill_updates = []

                    for charname in changed_chars:
                        raw_old = old_skills.get(charname)
                        old_entry = raw_old if isinstance(raw_old, dict) else {}

                        raw_new = new_skills.get(charname)
                        new_entry = raw_new if isinstance(raw_new, dict) else {}

                        char_table_lines = [
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

                            # User: shouldn't trigger unless the skill is new, like T3C goes from 0 to 1
                            if not (old_tr == 0 and new_tr > 0):
                                continue

                            old_fmt = f"{old_tr}/{old_ac}"
                            new_fmt = f"{new_tr}/{new_ac}"
                            name_padded = name.ljust(18)

                            char_table_lines.append(
                                f"{name_padded} | {old_fmt.ljust(9)} | {new_fmt.ljust(8)}"
                            )

                        if len(char_table_lines) > 2:
                            skill_updates.append(f"- **{charname}**:")
                            table_block = "```\n" + "\n".join(char_table_lines) + "\n```"
                            skill_updates.append(table_block)

                    if skill_updates and instance.cyno_notify:
                        now_ts = timezone.now().strftime('%Y-%m-%d %H:%M:%S')
                        changes.append(f"##{get_pings('skills')} Changes in skills detected ({now_ts} UTC):")
                        changes.extend(skill_updates)

                status.skills = new_skills
                status_changed = True
            if status.has_hostile_assets != has_hostile_assets or set(hostile_assets_result) != set(
                status.hostile_assets or []
            ):
                old_systems = set(status.hostile_assets or [])
                new_systems = set(hostile_assets_result) - old_systems

                # Build mapping: char -> list of (system, location, owner, region, ships)
                assets_by_char: dict[str, list[tuple[str, str, str, str, str]]] = {}

                for system in new_systems:
                    data = hostile_assets_result.get(system)
                    if not data or not isinstance(data, dict):
                        continue

                    owner = data.get("owner", "Unresolvable")
                    region = data.get("region", "Unknown Region")
                    records = data.get("records", [])

                    for rec in records:
                        cname = rec.get("char_name", "Unknown Character")
                        loc_name = rec.get("location_name", "Unknown Location")
                        ships = ", ".join(rec.get("ships", []))

                        assets_by_char.setdefault(cname, []).append(
                            (system, loc_name, owner, region, ships)
                        )

                lines: list[str] = []
                for cname in sorted(assets_by_char.keys()):
                    lines.append(f"- {cname}")
                    for system, loc_name, owner, region, ships in assets_by_char[cname]:
                        info = f"{loc_name} ({owner} | Region: {region})"
                        # Use system name if loc_name is not available or too generic
                        if not loc_name or loc_name == "Unknown Location":
                             info = f"{system} ({owner} | Region: {region})"

                        lines.append(f"   - {info}")
                        if ships:
                            lines.append(f"    - {ships}")

                if lines:
                    link_list = "\n".join(lines)
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} new hostile assets:\n{link_list}")

                # Overall boolean flip
                if status.has_hostile_assets != has_hostile_assets:
                    if not has_hostile_assets:
                        if instance.asset_notify:
                            changes.append("### Hostile Asset Status: 🟢")
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} hostile asset status changed")

                # Only add a "New Hostile Assets" section when there are actually new systems
                if new_systems and lines:
                    if instance.asset_notify:
                        changes.append(f"###{get_pings('New Hostile Assets')} New Hostile Assets:\n{link_list}")
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} new hostile asset systems: {', '.join(sorted(new_systems))}")

                status.has_hostile_assets = has_hostile_assets
                status_changed = True
                status.hostile_assets = hostile_assets_result
                status_changed = True

            if status.has_hostile_clones != has_hostile_clones or set(hostile_clones_result) != set(
                status.hostile_clones or []
            ):
                old_systems = set(status.hostile_clones or [])
                new_systems = set(hostile_clones_result) - old_systems

                # Build mapping: char -> list of (system, location, owner, region)
                clones_by_char: dict[str, list[tuple[str, str, str, str]]] = {}

                for system in new_systems:
                    data = hostile_clones_result.get(system)
                    if not data or not isinstance(data, dict):
                        continue

                    system_owner = data.get("owner", "Unresolvable")
                    region = data.get("region", "Unknown Region")
                    records = data.get("records", [])

                    for rec in records:
                        cname = rec.get("char_name", "Unknown Character")
                        loc_name = rec.get("location_name", "Unknown Location")
                        loc_owner = rec.get("owner_name", system_owner)
                        clone_name = rec.get("clone_name", "Jump Clone")

                        clones_by_char.setdefault(cname, []).append(
                            (system, loc_name, loc_owner, region, clone_name)
                        )

                lines: list[str] = []
                for cname in sorted(clones_by_char.keys()):
                    for system, loc_name, loc_owner, region, clone_name in clones_by_char[cname]:
                        lines.append(f"- {cname} [{clone_name}]")
                        info = f"{loc_name} ({loc_owner} | Region: {region})"
                        if not loc_name or loc_name == "Unknown Location":
                            info = f"{system} ({loc_owner} | Region: {region})"
                        lines.append(f"   - {info}")

                if lines:
                    link_list = "\n".join(lines)
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} new hostile clones:\n{link_list}")

                # Overall boolean flip
                if status.has_hostile_clones != has_hostile_clones:
                    if not has_hostile_clones:
                        if instance.clone_notify:
                            changes.append("### Hostile Clone Status: 🟢")
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} hostile clone status changed")

                if new_systems and lines:
                    if instance.clone_notify:
                        changes.append(f"###{get_pings('New Hostile Clones')} New Hostile Clone(s):\n{link_list}")
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} new hostile clone systems: {', '.join(sorted(new_systems))}")

                status.has_hostile_clones = has_hostile_clones
                status_changed = True
                status.hostile_clones = hostile_clones_result
                status_changed = True

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
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} new assets:\n{link_list}")

                if old_ids:  # optional debug log for existing entries
                    old_link_list = "\n".join(
                        f"🔗 {old_contacts[cid]}" for cid in old_ids if cid in old_contacts
                    )
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} old assets:\n{old_link_list}")

                if status.has_sus_contacts != has_sus_contacts:  # flag boolean flip
                    if not has_sus_contacts:
                        if instance.contact_notify:
                            changes.append(f"### Suspicious Contact Status: 🟢")
                logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} status changed")

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
                status_changed = True
                status.sus_contacts = sus_contacts_result
                status_changed = True

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
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} new assets:\n{link_list}")

                if old_ids:  # optional logging for previous entries
                    old_link_list = "\n".join(
                        f"🔗 {old_contracts[issuer_id]}" for issuer_id in old_ids if issuer_id in old_contracts
                    )
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} old assets:\n{old_link_list}")

                if status.has_sus_contracts != has_sus_contracts:  # summarize boolean change
                    if not has_sus_contracts:
                        if instance.contract_notify:
                            changes.append(f"## Suspicious Contract Status: 🟢")
                logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} status changed")

                if new_links:  # write each new contract entry to the report
                    if instance.contract_notify:
                        contract_lines = []
                        for issuer_id in sorted(new_links):
                            res = sus_contracts_result[issuer_id]
                            ping = get_pings('New Suspicious Contracts')
                            if res.startswith("- A -"):  # skip ping for alliance-level alerts
                                ping = ""
                            contract_lines.append(f"{res} {ping}")

                        if contract_lines:
                            changes.append(f"## New Suspicious Contracts:\n" + "\n".join(contract_lines))

                status.has_sus_contracts = has_sus_contracts
                status_changed = True
                status.sus_contracts = sus_contracts_result
                status_changed = True

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
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} new assets:\n{link_list}")

                if old_ids:  # optional logging for previous entries
                    old_link_list = "\n".join(
                        f"🔗 {old_mails[issuer_id]}" for issuer_id in old_ids if issuer_id in old_mails
                    )
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} old assets:\n{old_link_list}")

                if status.has_sus_mails != has_sus_mails:  # summarize boolean change
                    if not has_sus_mails:
                        if instance.mail_notify:
                            changes.append(f"### Suspicious Mail Status: 🟢")
                logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} status changed")

                if new_links:  # enumerate the new mail entries for the report
                    if instance.mail_notify:
                        mail_lines = []
                        for issuer_id in sorted(new_links):
                            res = sus_mails_result[issuer_id]
                            ping = get_pings('New Suspicious Mails')
                            if res.startswith("- A -"):  # skip ping for alliance-level alerts
                                ping = ""
                            mail_lines.append(f"{res} {ping}")

                        if mail_lines:
                            changes.append(f"### New Suspicious Mails:\n" + "\n".join(mail_lines))

                status.has_sus_mails = has_sus_mails
                status_changed = True
                status.sus_mails = sus_mails_result
                status_changed = True

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
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} new trans:\n{link_list}")

                if old_ids:
                    old_link_list = "\n".join(
                        f"{old_trans[issuer_id]}" for issuer_id in old_ids if issuer_id in old_trans
                    )
                    logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} old trans:\n{old_link_list}")

                if status.has_sus_trans != has_sus_trans:
                    if not has_sus_trans:
                        if instance.transaction_notify:
                            changes.append(f"## Suspicious Transactions Status: 🟢")
                logger.info(f"✅  [AA-BB] - [BB_update_single_user] - {char_name} status changed")
                if new_links:
                    if instance.transaction_notify:
                        changes.append(f"### New Suspicious Transactions{get_pings('New Suspicious Transactions')}:\n{link_list}")
                status.has_sus_trans = has_sus_trans
                status_changed = True
                status.sus_trans = sus_trans_result
                status_changed = True

            if not status.baseline_initialized:
                # First time auditing this user - respect new_user_notify setting
                send_notifications = instance.new_user_notify
            else:
                # Existing user - always send notifications for changes
                send_notifications = True

            if not limit_notifications and send_notifications and changes:
                """
                Build all embed chunks and hand them off to a dedicated
                send task so Discord messages are serialized and never
                interleave between users.
                """
                logger.info(
                    "✅  [AA-BB] - [BB_update_single_user] - [%s] baseline data exists. Calculating changes across %d blocks...",
                    char_name,
                    len(changes),
                )

                all_chunks: list[list[str]] = []

                # 1) Overall header – almost always a tiny single-chunk embed
                header_lines = [f"‼️ Status change detected for {char_name}"]

                # Calculate the total combined length to see if we can merge everything into one embed
                total_combined_len = len(header_lines[0]) + sum(len(c) for c in changes) + (len(changes) * 2)

                if total_combined_len < 1000:
                    logger.info(
                        "✅  [AA-BB] - [BB_update_single_user] - [%s] Merging %d changes into a single status embed (%d chars)",
                        char_name,
                        len(changes),
                        total_combined_len
                    )
                    merged_lines = header_lines + [""]
                    for chunk in changes:
                        merged_lines.extend([ln for ln in chunk.split("\n") if ln.strip()])
                        merged_lines.append("") # spacer

                    all_chunks = _chunk_embed_lines(merged_lines, max_chars=1900)
                else:
                    # Separate embeds for the header and each change block
                    for header_chunk in _chunk_embed_lines(header_lines, max_chars=1900):
                        all_chunks.append(header_chunk)

                    # 2) One or more embeds per change block, chunked by char count
                    for chunk in changes:
                        raw_lines = [ln for ln in chunk.split("\n") if ln.strip()]

                        for body_chunk in _chunk_embed_lines(raw_lines, max_chars=1900):
                            all_chunks.append(body_chunk)

                if all_chunks:
                    logger.info(
                        "✅  [AA-BB] - [BB_update_single_user] - [%s] Enqueuing %d embed chunks to BB_send_discord_notifications",
                        char_name,
                        len(all_chunks),
                    )
                    BB_send_discord_notifications.delay(char_name, all_chunks)

            # Only save if something actually changed or this is a new user
            if status_changed or not status.baseline_initialized:
                status.baseline_initialized = True
                status.updated = timezone.now()
                status.save()
                logger.debug(f"✅  [AA-BB] - [BB_update_single_user] - Saved status for {char_name} (changed: {status_changed})")
            else:
                logger.debug(f"✅  [AA-BB] - [BB_update_single_user] - No changes for {char_name}, skipping save")

            logger.info(f"✅  [AA-BB] - [BB_update_single_user] - END Update for user: {char_name} (ID: {user_id}) - Success")

            # Clean up large variables to free memory
            del cyno_result, skills_result, state_result, awox_data, awox_links, awox_map
            del hostile_clones_result, hostile_assets_result
            del sus_contacts_result, sus_contracts_result, sus_mails_result, sus_trans_result
            del sp_age_ratio_result, changes, entity_info_cache, user_chars
            if 'all_chunks' in locals():
                del all_chunks

            # Force garbage collection and close connections
            gc.collect()
            close_old_connections()
            break

        except OperationalError as e:
            code = e.args[0] if e.args else None
            if code == 1213 or "deadlock" in str(e).lower():
                delay = 0.5 * (attempt + 1)
                logger.warning(
                    f"ℹ️  [AA-BB] - [BB_update_single_user] - Deadlock while processing {char_name} "
                    f"(attempt {attempt + 1}/3); sleeping {delay:.1f}s before retry."
                )
                time.sleep(delay)
                # after last attempt, give up on this user but keep the overall stream alive
                if attempt == 2:
                    logger.error(
                        f"ℹ️  [AA-BB] - [BB_update_single_user] - Skipping {char_name} after repeated deadlocks."
                    )
                continue
            # not a deadlock → re-raise and let outer handler deal with it
            raise
        except Exception as e:
            logger.error(f"ℹ️  [AA-BB] - [BB_update_single_user] - Failed to update user {char_name}: {e}", exc_info=True)
            # Clean up on error
            gc.collect()
            close_old_connections()
            raise


@shared_task(time_limit=7200)
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
    # Close old DB connections to prevent memory leaks
    close_old_connections()

    instance = BigBrotherConfig.get_solo()

    try:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        # find a superuser's main to anchor corp/alliance fields
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

        instance.save(update_fields=["main_corporation_id", "main_corporation", "main_alliance_id", "main_alliance"])

        # walk each eligible user and rebuild their status snapshot
        if instance.is_active:  # skip user iteration entirely when plugin disabled/unlicensed
            # Use iterator to prevent loading all users into memory at once
            users = get_users()
            total_users = users.count()
            logger.info(
                f"✅  [AA-BB] - [BB_run_regular_updates] - Dispatching updates for {total_users} users (staggered)."
            )

            # Backlog check
            if instance.update_last_dispatch_count > 0:
                try:
                    inspector = current_app.control.inspect()
                    if inspector:
                        active = inspector.active() or {}
                        reserved = inspector.reserved() or {}

                        task_name = BB_update_single_user.name
                        remaining_count = 0

                        for worker_tasks in active.values():
                            if worker_tasks:
                                for t in worker_tasks:
                                    if t.get('name') == task_name:
                                        remaining_count += 1

                        for worker_tasks in reserved.values():
                            if worker_tasks:
                                for t in worker_tasks:
                                    if t.get('name') == task_name:
                                        remaining_count += 1

                        if remaining_count > 0 and instance.update_backlog_notify:
                            threshold = instance.update_backlog_threshold
                            percent = (remaining_count / instance.update_last_dispatch_count) * 100
                            if percent > threshold:
                                logger.warning(
                                    f"ℹ️  [AA-BB] - [BB_run_regular_updates] - Update backlog detected: {remaining_count} tasks remaining "
                                    f"({percent:.1f}% of last run count {instance.update_last_dispatch_count})"
                                )
                                send_status_embed(
                                    subject="Update Backlog Alert",
                                    lines=[
                                        f"{get_pings('Error')} {remaining_count} users are still being processed from the previous update run ({percent:.1f}% of {instance.update_last_dispatch_count} users).",
                                        "This may indicate that the update stagger window is too short or workers are overloaded."
                                    ],
                                    color=0xFF0000,
                                )
                except Exception as e:
                    logger.error(f"ℹ️  [AA-BB] - [BB_run_regular_updates] - Failed to check for update backlog: {e}", exc_info=True)

            instance.update_last_dispatch_count = total_users
            instance.save(update_fields=["update_last_dispatch_count"])

            if total_users == 0:
                return

            # We want to spread the work roughly across the configured stagger window
            # so we don't spike CPU at the top of the hour.
            from datetime import timedelta

            window_seconds = instance.update_stagger_seconds
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

            # Use .iterator() to avoid loading all users into the queryset cache
            for index, (user_id, char_name) in enumerate(users.iterator()):
                if not user_id:  # defensive: skip orphaned mains lacking a user id
                    continue

                # Schedule each user with an increasing ETA so the load is flattened
                offset = index * spacing
                eta = now + timedelta(seconds=offset)

                logger.info(
                    f"✅  [AA-BB] - [BB_run_regular_updates] - Scheduling BB_update_single_user for {char_name} (id={user_id}) "
                    f"in {offset}s at {eta.isoformat()}."
                )

                BB_update_single_user.apply_async(
                    args=(user_id, char_name),
                    eta=eta,
                )
        else:
            logger.warning("ℹ️  [AA-BB] - [BB_run_regular_updates] - Plugin is disabled (is_active=False), skipping user updates.")

        # Clean up and force garbage collection
        if 'users' in locals():
            del users
        gc.collect()
        close_old_connections()

    except Exception as e:
        logger.error("ℹ️  [AA-BB] - [BB_run_regular_updates] - Task failed", exc_info=True)
        gc.collect()
        close_old_connections()
        tb_str = traceback.format_exc()
        tb_lines = [f"{get_pings('Error')} Big Brother encountered an unexpected error", "```python"] + tb_str.split("\n") + ["```"]
        for chunk in _chunk_embed_lines(tb_lines):
            send_status_embed(
                subject="Big Brother Error",
                lines=chunk,
                color=0xFF0000,
            )

    from .tasks_utils import format_task_name
    task_name = format_task_name('BB run regular updates')
    task = PeriodicTask.objects.filter(name=task_name).first()
    if task and not task.enabled:  # inform admins when the periodic task finished its initial run
        send_status_embed(
            subject="Big Brother",
            lines=["Initial run of the task has finished, you can now enable the task"],
            color=0x00FF00,
        )

@shared_task(time_limit=7200)
def BB_send_discord_notifications(subject: str, chunks: list[list[str]]) -> None:
    """
    Dedicated task to send Discord embeds for BigBrother.

    - subject: usually the character name
    - chunks: list of "lines lists" – each inner list becomes one embed body

    Run this on a single-worker queue (concurrency=1) so embeds never
    interleave between users or checks.
    """
    close_old_connections()

    logger.info(
        "✅  [AA-BB] - [BB_send_discord_notifications] - Dispatching %d embed chunks for %s",
        len(chunks),
        subject,
    )

    for idx, lines in enumerate(chunks):
        logger.debug(
            "✅  [AA-BB] - [BB_send_discord_notifications] - Sending chunk %d/%d for %s (lines=%d)",
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

    # Clean up after sending all chunks
    del chunks
    gc.collect()
    close_old_connections()
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


@shared_task(bind=True, name="aa_bb.tasks.BB_sync_contacts_from_aa_contacts", time_limit=7200)
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

    if not cfg.auto_import_contacts_enabled:
        return

    source_alliances_field = getattr(cfg, "contacts_source_alliances", None)
    source_corporations_field = getattr(cfg, "contacts_source_corporations", None)

    has_alliances = source_alliances_field is not None and source_alliances_field.exists()
    has_corporations = source_corporations_field is not None and source_corporations_field.exists()

    if not has_alliances and not has_corporations:
        return

    try:
        from importlib import import_module
        aa_contacts_models = import_module("aa_contacts.models")
        AllianceContact = getattr(aa_contacts_models, "AllianceContact", None)
        CorporationContact = getattr(aa_contacts_models, "CorporationContact", None)
    except (ImportError, ModuleNotFoundError):
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

    def process_contacts(contacts_qs):
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

    if has_alliances and AllianceContact:
        for src_alliance in source_alliances_field.all():
            process_contacts(AllianceContact.objects.filter(alliance=src_alliance))

    if has_corporations and CorporationContact:
        for src_corp in source_corporations_field.all():
            process_contacts(CorporationContact.objects.filter(corporation=src_corp))

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
        cfg.save(update_fields=[
            "member_alliances", "member_corporations",
            "hostile_alliances", "hostile_corporations",
            "whitelist_alliances", "whitelist_corporations",
            "contacts_import_cache"
        ])
