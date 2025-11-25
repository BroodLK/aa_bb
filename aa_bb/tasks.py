from .models import UserStatus

from .app_settings import (
    resolve_character_name,
)
from aa_bb.checks.awox import  get_awox_kill_links
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

from .tasks_cb import *
from .tasks_ct import *
from .tasks_tickets import *
import logging
logger = logging.getLogger(__name__)

@shared_task
def BB_run_regular_updates():
    """
    Main scheduled job that refreshes BigBrother cache entries.

    Workflow:
      1. Ensure the singleton config exists and derive the primary corp/alliance
         from a superuser alt (also toggling DLC flags when applicable).
      2. Iterate through every user returned by `get_users()`.
      3. For each user, recalculates every signal (awox, cyno, skills, hostiles,
         etc.), compares against the previous snapshot, and appends human-readable
         change notes to `changes`.
      4. When certain checks flip (clone state, skill injections, awox kills),
         Discord notifications and optional compliance tickets are issued.
      5. Persist the updated `UserStatus` row so the dashboard stays in sync.

    Section overview:
      • Config bootstrap: lines 22–58 – ensure `BigBrotherConfig` is populated and
        that DLC flags mirror the currently discovered corp/alliance.
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

            for field_name in BigBrotherConfig.DLC_FLAG_MAP.values():
                setattr(instance, field_name, True)


        instance.save()

        # walk each eligible user and rebuild their status snapshot
        if instance.is_active:  # skip user iteration entirely when plugin disabled/unlicensed
            users = get_users()

            for char_name in users:
                user_id = get_user_id(char_name)
                if not user_id:  # defensive: skip orphaned mains lacking a user id
                    continue

                pingroleID = instance.pingroleID

                # eager-load all check data so diffing below is cheap
                cyno_result = get_user_cyno_info(user_id)
                skills_result = get_multiple_user_skill_info(user_id, skill_ids)
                state_result = determine_character_state(user_id, True)
                awox_links = get_awox_kill_links(user_id)
                hostile_clones_result = get_hostile_clone_locations(user_id)
                hostile_assets_result = get_hostile_asset_locations(user_id)
                sus_contacts_result = { str(cid): v for cid, v in get_user_hostile_notifications(user_id).items() }
                sus_contracts_result = { str(issuer_id): v for issuer_id, v in get_user_hostile_contracts(user_id).items() }
                sus_mails_result = { str(issuer_id): v for issuer_id, v in get_user_hostile_mails(user_id).items() }
                sus_trans_result = { str(issuer_id): v for issuer_id, v in get_user_hostile_transactions(user_id).items() }
                sp_age_ratio_result: dict[str, dict] = {}

                def norm(d):
                    d = d or {}
                    return {
                        n: {k: v for k, v in (entry if isinstance(entry, dict) else {}).items() if k != 'age'}  # drop 'age' noise when diffing
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

                for char_nameeee, data in skills_result.items():
                    char_id = get_character_id(char_nameeee)
                    char_age = get_char_age(char_id)
                    total_sp = data["total_sp"]
                    sp_days = (total_sp-384000)/64800 if total_sp else 0  # convert SP into training-day equivalent

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
                send_notifications = not created

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
                        if not old_data or old_data.get("state") != new_data.get("state"):  # capture per-character state transitions
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
                        changes.append(f"##{pinggg} Clone state change detected:{''.join(flagggs)}")
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

                            if old_ratio is not None and new_ratio is not None and new_ratio > old_ratio:  # only flag when ratio increased
                                flaggs.append(
                                    f"- **{char_nameee}'s** SP to age ratio went up from **{old_ratio}** to **{new_ratio}**\n"
                                )

                        if flaggs:  # only send notification when at least one character’s ratio increased
                            sp_list = "".join(flaggs)
                            changes.append(f"## {get_pings('SP Injected')} Skill Injection detected:\n{sp_list}")

                status.sp_age_ratio_result = sp_age_ratio_result
                status.save()

                if status.has_awox_kills != has_awox or set(awox_links) != set(status.awox_kill_links or []):  # new awox activity?
                    # detect new AWOX links and optionally raise a ticket
                    # Compare and find new links
                    old_links = set(status.awox_kill_links or [])
                    new_links = set(awox_links) - old_links
                    link_list = "\n".join(f"- {link}" for link in new_links)
                    logger.info(f"{char_name} new links {link_list}")
                    link_list3 = "\n".join(f"- {link}" for link in awox_links)
                    logger.info(f"{char_name} new links {link_list3}")
                    link_list2 = "\n".join(f"- {link}" for link in old_links)
                    logger.info(f"{char_name} old links {link_list2}")
                    if status.has_awox_kills != has_awox and has_awox:  # first time awox kills were spotted for this user
                        changes.append(f"## AWOX Kill Status: {'🔴' if has_awox else '🟢'}")
                        status.has_awox_kills = has_awox
                        logger.info(f"{char_name} changed")
                    if new_links:  # send notifications only for links not yet alerted on
                        changes.append(f"##{get_pings('AwoX')} New AWOX Kill(s):\n{link_list}")
                        logger.info(f"{char_name} new links")
                        tcfg = TicketToolConfig.get_solo()
                        if tcfg.awox_monitor_enabled and time_in_corp(user_id) >= 1:  # guardrail: only fire tickets for monitored corps
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
                        changes.append(f"Cyno Status: {'🔴' if has_cyno else '🟢'}")
                        status.has_cyno = has_cyno

                    # 2) Grab the old vs. new JSON blobs
                    old_cyno: dict = status.cyno or {}
                    new_cyno: dict = cyno_result

                    # Determine which character names actually changed
                    changed_chars = []
                    for char_namee, new_data in new_cyno.items():
                        old_data = old_cyno.get(char_namee, {})
                        old_filtered = {k: v for k, v in old_data.items() if k != 'age'}  # ignore 'age' helper field in comparisons
                        new_filtered = {k: v for k, v in new_data.items() if k != 'age'}  # ignore 'age' helper field in comparisons

                        #logger.info(f"Comparing skills for character '{char_namee}':")
                        #logger.info(f"Old data normalized: {old_filtered}")
                        #logger.info(f"New data normalized: {new_filtered}")
                        #from deepdiff import DeepDiff
                        #diff = DeepDiff(old_filtered, new_filtered, ignore_order=True)
                        #logger.info(f"Diff for '{char_namee}': {diff}")
                        if old_filtered != new_filtered:  # record only characters whose cyno skill blob changed
                            changed_chars.append(char_namee)

                    # 3) If any changed, build one table per character
                    if changed_chars:  # only build the verbose table output when someone’s cyno profile actually changed
                        # Mapping for display names
                        cyno_display = {
                            "s_cyno":    "Cyno Skill",
                            "s_cov_cyno":"CovOps Cyno",
                            "s_recon":   "Recon Ships",
                            "s_hic":     "Heavy Interdiction",
                            "s_blops":   "Black Ops",
                            "s_covops":  "Covert Ops",
                            "s_brun":    "Blockade Runners",
                            "s_sbomb":   "Stealth Bombers",
                            "s_scru":    "Strat Cruisers",
                            "s_expfrig": "Expedition Frigs",
                            "s_carrier": "Carriers",
                            "s_dread":   "Dreads",
                            "s_fax":     "FAXes",
                            "s_super":   "Supers",
                            "s_titan":   "Titans",
                            "s_jf":      "JFs",
                            "s_rorq":    "Rorqs",
                            "i_recon":   "Has a Recon",
                            "i_hic":     "Has a Hic",
                            "i_blops":   "Has a Blops",
                            "i_covops":  "Has a covops",
                            "i_brun":    "Has a blockade Runner",
                            "i_sbomb":   "Has a bomber",
                            "i_scru":    "Has a strat crus",
                            "i_expfrig": "Has a exp frig",
                            "i_carrier": "Has a Carrier",
                            "i_dread":   "Has a Dread",
                            "i_fax":     "Has a FAX",
                            "i_super":   "Has a Super",
                            "i_titan":   "Has a Titan",
                            "i_jf":      "Has a JF",
                            "i_rorq":    "Has a Rorq",
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
                            changes.append(f"##{get_pings('All Cyno Changes')} Changes in cyno capabilities detected:")

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

                            changes.append(f"- **{charname}**{pingrole}:")
                            table_lines = [
                                "Value                  | Old | New (1 = trained but alpha, 2 = active)",
                                "-----------------------------------"
                            ]

                            for key in cyno_keys:
                                display = cyno_display.get(key, key)
                                old_val = str(old_entry.get(key, 0))
                                new_val = str(new_entry.get(key, 0))
                                table_lines.append(f"{display.ljust(22)} | {old_val.ljust(3)} | {new_val.ljust(3)}")

                            # Show can_light as a summary at bottom
                            can_light_old = old_entry.get("can_light", False)
                            can_light_new = new_entry.get("can_light", False)
                            table_lines.append("")
                            table_lines.append(f"{'Can Light':<22} | {'Yes' if can_light_old else 'No'} | {'Yes' if can_light_new else 'No'}")

                            # 👉 Add corp time here
                            try:
                                cid = get_character_id(charname)
                                corp_days = get_current_stint_days_in_corp(cid, BigBrotherConfig.get_solo().main_corporation_id)
                                corp_label = f"Time in {BigBrotherConfig.get_solo().main_corporation}"
                                table_lines.append(f"{corp_label:<22} | {corp_days} days")
                            except Exception as e:
                                logger.warning(f"Could not fetch corp time for {charname}: {e}")


                            table_block = "```\n" + "\n".join(table_lines) + "\n```"
                            changes.append(table_block)

                    # 4) Save new blob
                    status.cyno = new_cyno


                if status.has_skills != has_skills or skills_norm(skills_result) != skills_norm(status.skills or {}):  # skill list changed?
                    # 1) If the boolean flag flipped, append the 🔴 / 🟢 as before
                    if status.has_skills != has_skills:  # emit coarse-grained flag when the threshold crosses zero/any skills
                        changes.append(f"## Skill Status: {'🔴' if has_skills else '🟢'}")
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

                        #logger.info(f"Comparing skills for character '{character_name}':")
                        #logger.info(f"Old data normalized: {old_data_norm}")
                        #logger.info(f"New data normalized: {new_data_norm}")
                        #from deepdiff import DeepDiff
                        #diff = DeepDiff(old_data_norm, new_data_norm, ignore_order=True)
                        #logger.info(f"Diff for '{character_name}': {diff}")

                        if old_data_norm != new_data_norm:  # record only characters whose skill payload changed
                            changed_chars.append(character_name)

                    # 3) If any changed, build one table per character
                    if changed_chars:
                        # A mapping from skill_id → human-readable name
                        skill_names = {
                            3426:   "CPU Management",
                            21603:  "Cynosural Field Theory",
                            22761:  "Recon Ships",
                            28609:  "Heavy Interdiction Cruisers",
                            28656:  "Black Ops",
                            12093:  "Covert Ops / Stealth Bombers",
                            20533:  "Capital Ships",
                            19719:  "Blockade Runners",
                            30651:  "Caldari Strategic Cruisers",
                            30652:  "Gallente Strategic Cruisers",
                            30653:  "Minmatar Strategic Cruisers",
                            30650:  "Amarr Strategic Cruisers",
                            33856:  "Expedition Frigates",
                        }

                        # Keep the same order you gave, but dedupe 12093 once
                        ordered_skill_ids = [
                            3426, 21603, 22761, 28609, 28656,
                            12093, 20533, 19719,
                            30651, 30652, 30653, 30650, 33856,
                        ]

                        if changed_chars:  # preface the per-character tables with a summary line
                            changes.append(f"##{get_pings('skills')} Changes in skills detected:")

                        for charname in changed_chars:
                            # Defensive retrieval of old vs. new
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


                            # 3a) Append the “- **CharacterName**:” header
                            changes.append(f"- **{charname}**:")

                            # 3b) Build the table header and separator
                            table_lines = [
                                "Skill                           | Old (Trained/Active) | New (Trained/Active)",
                                "------------------------------------------------------"
                            ]

                            # 3c) For each skill_id in order, look up old vs. new levels
                            for sid in ordered_skill_ids:
                                name = skill_names.get(sid, f"Skill ID {sid}")

                                # Because JSONField stores keys as strings, do str(sid)
                                old_skill = old_entry.get(str(sid), {"trained": 0, "active": 0})
                                new_skill = new_entry.get(sid, {"trained": 0, "active": 0})

                                # Defensive: if old_skill is not a dict, coerce it
                                if not isinstance(old_skill, dict):  # guard against malformed cache entries
                                    old_skill = {"trained": 0, "active": 0}
                                if not isinstance(new_skill, dict):  # same safeguard for new data
                                    new_skill = {"trained": 0, "active": 0}

                                old_tr = old_skill.get("trained", 0)
                                old_ac = old_skill.get("active", 0)
                                new_tr = new_skill.get("trained", 0)
                                new_ac = new_skill.get("active", 0)

                                old_fmt = f"{old_tr}/{old_ac}"
                                new_fmt = f"{new_tr}/{new_ac}"

                                # Pad the skill name to 30 chars for alignment
                                name_padded = name.ljust(30)

                                # Pad the “trained/active” to at least 9 chars so columns line up
                                table_lines.append(f"{name_padded} | {old_fmt.ljust(9)} | {new_fmt.ljust(9)}")

                            # 3d) Wrap the lines in triple backticks
                            table_block = "```\n" + "\n".join(table_lines) + "\n```"
                            changes.append(table_block)

                    # 4) Finally, write back the new blob so that next time “old” is fresh
                    status.skills = new_skills
                # …rest of your saving logic, e.g. status.save(), etc.


                if status.has_hostile_assets != has_hostile_assets or set(hostile_assets_result) != set(status.hostile_assets or []):  # asset list changed?
                    # Compare and find new links
                    old_links = set(status.hostile_assets or [])
                    new_links = set(hostile_assets_result) - old_links
                    link_list = "\n".join(
                        f"- {system} owned by {hostile_assets_result[system]}"
                        for system in (set(hostile_assets_result) - set(status.hostile_assets or []))
                    )
                    logger.info(f"{char_name} new assets {link_list}")
                    link_list2 = "\n- ".join(f"🔗 {link}" for link in old_links)
                    logger.info(f"{char_name} old assets {link_list2}")
                    if status.has_hostile_assets != has_hostile_assets:  # overall hostiles flag flipped
                        changes.append(f"## Hostile Asset Status: {'🔴' if has_hostile_assets else '🟢'}")
                        logger.info(f"{char_name} changed")
                    if new_links:  # only announce newly discovered systems
                        changes.append(f"##{get_pings('New Hostile Assets')} New Hostile Assets:\n{link_list}")
                        logger.info(f"{char_name} new assets")
                    status.has_hostile_assets = has_hostile_assets
                    status.hostile_assets = hostile_assets_result


                if status.has_hostile_clones != has_hostile_clones or set(hostile_clones_result) != set(status.hostile_clones or []):  # hostile clone locations changed?
                    # Compare and find new links
                    old_links = set(status.hostile_clones or [])
                    new_links = set(hostile_clones_result) - old_links
                    link_list = "\n".join(
                        f"- {system} owned by {hostile_clones_result[system]}"
                        for system in (set(hostile_clones_result) - set(status.hostile_clones or []))
                    )
                    logger.info(f"{char_name} new clones: {link_list}")
                    link_list2 = "\n".join(f"🔗 {link}" for link in old_links)
                    logger.info(f"{char_name} old clones: {link_list2}")
                    if status.has_hostile_clones != has_hostile_clones:  # boolean changed → emit summary
                        changes.append(f"## Hostile Clone Status: {'🔴' if has_hostile_clones else '🟢'}")
                        logger.info(f"{char_name} changed")
                    if new_links:  # list out newly detected clone systems
                        changes.append(f"##{get_pings('New Hostile Clones')} New Hostile Clone(s):\n{link_list}")
                        logger.info(f"{char_name} new clones")
                    status.has_hostile_clones = has_hostile_clones
                    status.hostile_clones = hostile_clones_result

                if status.has_sus_contacts != has_sus_contacts or set(sus_contacts_result) != set(as_dict(status.sus_contacts) or {}):  # suspect contacts changed?
                    old_contacts = as_dict(status.sus_contacts) or {}
                    #normalized_old = { str(cid): v for cid, v in status.sus_contacts.items() }
                    #normalized_new = { str(cid): v for cid, v in sus_contacts_result.items() }

                    old_ids   = set(as_dict(status.sus_contacts).keys())
                    new_ids   = set(sus_contacts_result.keys())
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
                        changes.append(f"## Suspicious Contact Status: {'🔴' if has_sus_contacts else '🟢'}")
                    logger.info(f"{char_name} status changed")

                    if new_links:  # include the new contact entries in the summary
                        changes.append(f"## New Suspicious Contacts:")
                        for cid in new_links:
                            res = sus_contacts_result[cid]
                            ping = get_pings('New Suspicious Contacts')
                            if res.startswith("- A -"):  # skip ping for alliance-only entries
                                ping = ""
                            changes.append(f"{res} {ping}")

                    status.has_sus_contacts = has_sus_contacts
                    status.sus_contacts = sus_contacts_result

                if status.has_sus_contracts != has_sus_contracts or set(sus_contracts_result) != set(as_dict(status.sus_contracts) or {}):  # suspicious contracts changed?
                    old_contracts = as_dict(status.sus_contracts) or {}
                    #normalized_old = { str(cid): v for cid, v in status.sus_contacts.items() }
                    #normalized_new = { str(cid): v for cid, v in sus_contacts_result.items() }

                    old_ids   = set(as_dict(status.sus_contracts).keys())
                    new_ids   = set(sus_contracts_result.keys())
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
                        changes.append(f"## Suspicious Contract Status: {'🔴' if has_sus_contracts else '🟢'}")
                    logger.info(f"{char_name} status changed")

                    if new_links:  # write each new contract entry to the report
                        changes.append(f"## New Suspicious Contracts:")
                        for issuer_id in new_links:
                            res = sus_contracts_result[issuer_id]
                            ping = get_pings('New Suspicious Contracts')
                            if res.startswith("- A -"):  # skip ping for alliance-level alerts
                                ping = ""
                            changes.append(f"{res} {ping}")

                    status.has_sus_contracts = has_sus_contracts
                    status.sus_contracts = sus_contracts_result

                if status.has_sus_mails != has_sus_mails or set(sus_mails_result) != set(as_dict(status.sus_mails) or {}):  # suspicious mails changed?
                    old_mails = as_dict(status.sus_mails) or {}
                    #normalized_old = { str(cid): v for cid, v in status.sus_contacts.items() }
                    #normalized_new = { str(cid): v for cid, v in sus_contacts_result.items() }

                    old_ids   = set(as_dict(status.sus_mails).keys())
                    new_ids   = set(sus_mails_result.keys())
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
                        changes.append(f"## Suspicious Mail Status: {'🔴' if has_sus_mails else '🟢'}")
                    logger.info(f"{char_name} status changed")

                    if new_links:  # enumerate the new mail entries for the report
                        changes.append(f"## New Suspicious Mails:")
                        for issuer_id in new_links:
                            res = sus_mails_result[issuer_id]
                            ping = get_pings('New Suspicious Mails')
                            if res.startswith("- A -"):  # skip ping for alliance-level alerts
                                ping = ""
                            changes.append(f"{res} {ping}")

                    status.has_sus_mails = has_sus_mails
                    status.sus_mails = sus_mails_result

                if status.has_sus_trans != has_sus_trans or set(sus_trans_result) != set(as_dict(status.sus_trans) or {}):  # suspicious wallet txns changed?
                    old_trans = as_dict(status.sus_trans) or {}
                    #normalized_old = { str(cid): v for cid, v in status.sus_contacts.items() }
                    #normalized_new = { str(cid): v for cid, v in sus_contacts_result.items() }

                    old_ids   = set(as_dict(status.sus_trans).keys())
                    new_ids   = set(sus_trans_result.keys())
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
                        changes.append(f"## Suspicious Transactions Status: {'🔴' if has_sus_trans else '🟢'}")
                    logger.info(f"{char_name} status changed")
                    changes.append(f"## New Suspicious Transactions{get_pings('New Suspicious Transactions')}:\n{link_list}")

                    status.has_sus_trans = has_sus_trans
                    status.sus_trans = sus_trans_result

                if send_notifications and changes:  # emit each of the accumulated change summaries
                    for i in range(0, len(changes)):
                        chunk = changes[i]
                        if i == 0:  # the first chunk gets the header
                            msg = f"# 🛑 Status change detected for **{char_name}**:\n" + "\n" + chunk
                        else:
                            msg = chunk
                        logger.info(f"Message: {msg}")
                        send_message(msg)
                        time.sleep(0.03)
                status.updated = timezone.now()
                status.save()

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
            send_message(f"```{chunk}```")
            start = end

    from django_celery_beat.models import PeriodicTask
    task_name = 'BB run regular updates'
    task = PeriodicTask.objects.filter(name=task_name).first()
    if not task.enabled:  # inform admins when the periodic task finished its initial run
        send_message("initial Big Brother task has finished, you can now enable the task")
