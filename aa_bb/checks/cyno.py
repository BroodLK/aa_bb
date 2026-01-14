"""
Cyno readiness checks.

This module infers whether each character can light standard/covops cynos
by combining skill levels, ship ownership and corporation tenure. The
heavy lifting happens here so templates only need to call render helpers.
"""

from allianceauth.services.hooks import get_extension_logger


from .skills import get_user_skill_info, get_char_age, get_multiple_user_skill_info
from ..app_settings import get_user_characters, get_character_id, corptools_active
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from .corp_changes import get_current_stint_days_in_corp
from aa_bb.models import BigBrotherConfig as bbc

logger = get_extension_logger(__name__)

try:
    if corptools_active():
        from corptools.models import CharacterAudit, CharacterAsset
    else:
        CharacterAudit = None
        CharacterAsset = None
except ImportError:
    CharacterAudit = None
    CharacterAsset = None

skill_ids = {
    "cyno":     21603,  # Cynosural Field Theory
    "recon":    22761,  # Recon Ships
    "hic":      28609,  # Heavy Interdiction Cruisers
    "blops":    28656,  # Black Ops
    "covops":   12093,  # Covert Ops
    "brun":     19719,  # Blockade Runners
    "sbomb":    12093,  # Stealth Bombers
    "calscru":  30651,  # Stategic Cruisers
    "galscru":  30652,  # Stategic Cruisers
    "minscru":  30653,  # Stategic Cruisers
    "amascru":  30650,  # Stategic Cruisers
    "expfrig":  33856,  # Expedition Frigates
    "acarrier": 24311,
    "ccarrier": 24312,
    "gcarrier": 24313,
    "mcarrier": 24314,
    "adread":   20525,
    "cdread":   20530,
    "gdread":   20531,
    "mdread":   20532,
    "tdread":   52997,
    "atitan":   3347,
    "ctitan":   3346,
    "gtitan":   3344,
    "mtitan":   3345,
    "ajf":      20524,
    "cjf":      20526,
    "gjf":      20527,
    "mjf":      20528,
    "jf":       29029,
    "rorq":     28374,
}

def get_user_cyno_info(user_id: int, cfg: bbc = None) -> dict:
    """
    Given an AllianceAuth user ID, returns for each of that user's characters:
      - s_<skill>: 1 if trained_skill_level >= required_levels[skill] else 0
      - i_<skill>: 1 if active_skill_level  >= required_levels[skill] else 0 (except for cyno where only s_cyno)

    `required_levels` is an optional dict mapping skill keys ("cyno", "recon", etc.) to the minimum trained/active level required.
    If not provided, defaults to 1 for all skills.
    """
    if not corptools_active() or CharacterAudit is None:
        return {}
    # default required levels
    required_levels = {
        "cyno":      1,  # Cynosural Field Theory
        "recon":     1,  # Recon Ships
        "hic":       1,  # Heavy Interdiction Cruisers
        "blops":     1,  # Black Ops
        "covops":    1,  # Covert Ops
        "brun":      1,  # Blockade Runners
        "sbomb":     1,  # Stealth Bombers
        "calscru":   1,  # Stategic Cruisers
        "galscru":   1,  # Stategic Cruisers
        "minscru":   1,  # Stategic Cruisers
        "amascru":   1,  # Stategic Cruisers
        "expfrig":   1,  # Expedition Frigates
        "acarrier":  1,
        "ccarrier":  1,
        "gcarrier":  1,
        "mcarrier":  1,
        "adread":    1,
        "cdread":    1,
        "gdread":    1,
        "mdread":    1,
        "tdread":    1,
        "atitan":    1,
        "ctitan":    1,
        "gtitan":    1,
        "mtitan":    1,
        "ajf":       4,
        "cjf":       4,
        "gjf":       4,
        "mjf":       4,
        "jf":        1,
        "rorq":      1,
    }

    # 1) grab all of this user's owned characters
    ownership_map = get_user_characters(user_id)
    if not ownership_map:
        return {}

    # 2) pre-fetch audits for only those characters
    audits = (
        CharacterAudit.objects
        .filter(character__character_id__in=ownership_map.keys())
    )

    # 3) fetch all skills once
    all_skill_ids = list(skill_ids.values())
    raw_skills = get_multiple_user_skill_info(user_id, all_skill_ids)
    # Re-map raw_skills into the key-based structure the rest of the function expects
    skill_data = {}
    for key, sid in skill_ids.items():
        skill_data[key] = {}
        for name, char_data in raw_skills.items():
            s_info = char_data.get(sid, {"trained": 0, "active": 0})
            skill_data[key][name] = {
                "trained_skill_level": s_info["trained"],
                "active_skill_level": s_info["active"]
            }

    # 4) pre-fetch owned asset groups for all characters
    asset_groups = {}
    if corptools_active() and CharacterAsset is not None:
        qs = (
            CharacterAsset.objects
            .filter(character__character__character_id__in=ownership_map.keys())
            .values('character__character__character_id', 'type_name__group_id')
            .distinct()
        )
        for row in qs:
            cid_val = row['character__character__character_id']
            gid_val = row['type_name__group_id']
            if cid_val not in asset_groups:
                asset_groups[cid_val] = set()
            asset_groups[cid_val].add(gid_val)

    result = {}

    for audit in audits:
        name = ownership_map[audit.character.character_id]
        cid = audit.character.character_id
        age = get_char_age(cid)

        char_asset_groups = asset_groups.get(cid, set())

        i_recon = 833 in char_asset_groups
        i_hic = 894 in char_asset_groups
        i_blops = 898 in char_asset_groups
        i_covops = 830 in char_asset_groups
        i_brun = 1202 in char_asset_groups
        i_sbomb = 834 in char_asset_groups
        i_scru = 963 in char_asset_groups
        i_expfrig = 1283 in char_asset_groups
        i_carrier = 547 in char_asset_groups
        i_dread = 485 in char_asset_groups
        i_fax = 1538 in char_asset_groups
        i_super = 659 in char_asset_groups
        i_titan = 30 in char_asset_groups
        i_jf = 902 in char_asset_groups
        i_rorq = 883 in char_asset_groups

        # initialize all flags to 0
        char_dic = {
            "s_cyno":    0,
            "s_cov_cyno":0,
            "s_recon":   0,
            "s_hic":     0,
            "s_blops":   0,
            "s_covops":  0,
            "s_brun":    0,
            "s_sbomb":   0,
            "s_scru":    0,
            "s_expfrig": 0,
            "s_carrier": 0,
            "s_dread":   0,
            "s_fax":     0,
            "s_super":   0,
            "s_titan":   0,
            "s_jf":      0,
            "s_rorq":    0,
            "i_recon":   i_recon,
            "i_hic":     i_hic,
            "i_blops":   i_blops,
            "i_covops":  i_covops,
            "i_brun":    i_brun,
            "i_sbomb":   i_sbomb,
            "i_scru":    i_scru,
            "i_expfrig": i_expfrig,
            "i_carrier": i_carrier,
            "i_dread": i_dread,
            "i_fax": i_fax,
            "i_super": i_super,
            "i_titan": i_titan,
            "i_jf": i_jf,
            "i_rorq": i_rorq,
            "age":       age,
            "can_light": False,
        }
        jfff = 0

        # Pre-calculate JF status to avoid ordering issues
        jf_info = skill_data.get("jf", {}).get(name, {"trained_skill_level": 0, "active_skill_level": 0})
        jf_req = required_levels.get("jf", 1)
        if jf_info["trained_skill_level"] >= jf_req:
            jfff = 1
        if jf_info["active_skill_level"] >= jf_req:
            jfff = 2

        # set flags based on required_levels
        for key, data in skill_data.items():
            lvl_req = required_levels.get(key, 1)
            info = data.get(name, {"trained_skill_level": 0, "active_skill_level": 0})

            if key == "jf":
                continue

            elif key in ["acarrier", "ccarrier", "gcarrier", "mcarrier"]:  # Any racial carrier skill maps to generic carrier/fax/super flags.
                if info["trained_skill_level"] >= lvl_req:  # Carrier skills also imply Super/FAX hull capabilities.
                    char_dic[f"s_carrier"] = 1
                    char_dic[f"s_super"] = 1
                    char_dic[f"s_fax"] = 1
                if info["active_skill_level"] >= lvl_req:  # Active level 2 indicates Omega-ready for all related hulls.
                    char_dic[f"s_carrier"] = 2
                    char_dic[f"s_super"] = 2
                    char_dic[f"s_fax"] = 2

            elif key in ["adread", "cdread", "gdread", "mdread", "tdread"]:  # Aggregate dreadnought skills.
                if info["trained_skill_level"] >= lvl_req:  # Any dread skill counts toward the generic dread flag.
                    char_dic[f"s_dread"] = 1
                if info["active_skill_level"] >= lvl_req:  # Active = Omega-ready dread pilot.
                    char_dic[f"s_dread"] = 2

            elif key in ["atitan", "ctitan", "gtitan", "mtitan"]:  # Any titan racial skill toggles titan readiness.
                if info["trained_skill_level"] >= lvl_req:  # Any titan racial skill unlocks titan flag.
                    char_dic[f"s_titan"] = 1
                if info["active_skill_level"] >= lvl_req:  # Active implies Omega titan readiness.
                    char_dic[f"s_titan"] = 2

            elif key in ["ajf", "cjf", "gjf", "mjf"]:  # Racial jump freighter hull skills require base JF.
                if info["trained_skill_level"] >= lvl_req and jfff >= 1:  # Only mark JF ready when base JF skill is satisfied.
                    char_dic[f"s_jf"] = 1
                    if info["active_skill_level"] >= lvl_req and jfff >= 2:  # Active racial JF skill plus base skill unlocks JF flag.
                        char_dic[f"s_jf"] = 2

            elif key == "rorq":  # Rorqual specific handling.
                if info["trained_skill_level"] >= lvl_req:  # Rorqual skill acts like other hull checks.
                    char_dic[f"s_rorq"] = 1
                if info["active_skill_level"] >= lvl_req:  # Active skill toggles Rorqual omega-ready flag.
                    char_dic[f"s_rorq"] = 2

            elif key in ["calscru", "amascru", "galscru", "minscru"]:  # T3 cruiser subsystems map to generic T3 status.
                if info["trained_skill_level"] >= lvl_req:  # Any T3 subsystem skill enables the general T3 flag.
                    char_dic[f"s_scru"] = 1
                    if info["active_skill_level"] >= lvl_req:  # Active T3 subsystem skill grants omega-ready status.
                        char_dic[f"s_scru"] = 2

            elif key == "cyno":  # Base cyno skill tracks both standard and covert cyno readiness.
                if info["trained_skill_level"] >= lvl_req:  # Cyno 1 unlocks standard cyno ability.
                    char_dic[f"s_{key}"] = 1
                if info["active_skill_level"] >= lvl_req:  # Active cyno skill sets standard cyno flag.
                    char_dic[f"s_{key}"] = 2
                if info["trained_skill_level"] == 5:  # Level 5 training unlocks covert cyno flag as well.
                    char_dic[f"s_cov_{key}"] = 1
                if info["active_skill_level"] == 5:  # Active level 5 indicates cov cyno ready even as alpha (rare).
                    char_dic[f"s_cov_{key}"] = 2

            else:
                if info["trained_skill_level"] >= lvl_req:  # Generic hull/skill gating.
                    char_dic[f"s_{key}"] = 1
                if info["active_skill_level"] >= lvl_req:  # Active skill yields omega-ready status for generic hulls.
                    char_dic[f"s_{key}"] = 2

        if char_dic[f"s_cyno"] > 0 and char_dic[f"s_recon"] > 0 and char_dic[f"i_recon"] == True:  # Standard cyno + recon hull.
            char_dic[f"can_light"] = True
        if char_dic[f"s_cyno"] > 0 and char_dic[f"s_hic"] > 0 and char_dic[f"i_hic"] == True:  # Standard cyno + HIC hull.
            char_dic[f"can_light"] = True
        if char_dic[f"s_cyno"] > 0 and char_dic[f"s_blops"] > 0 and char_dic[f"i_blops"] == True:  # Standard cyno + black ops.
            char_dic[f"can_light"] = True
        if char_dic[f"s_cov_cyno"] > 0 and char_dic[f"s_covops"] > 0 and char_dic[f"i_covops"] == True:  # Covert cyno + covops hull.
            char_dic[f"can_light"] = True
        if char_dic[f"s_cov_cyno"] > 0 and char_dic[f"s_brun"] > 0 and char_dic[f"i_brun"] == True:  # Cov cyno + blockade runner.
            char_dic[f"can_light"] = True
        if char_dic[f"s_cov_cyno"] > 0 and char_dic[f"s_sbomb"] > 0 and char_dic[f"i_sbomb"] == True:  # Cov cyno + bomber.
            char_dic[f"can_light"] = True
        if char_dic[f"s_cov_cyno"] > 0 and char_dic[f"s_scru"] > 0 and char_dic[f"i_scru"] == True:  # Cov cyno + T3 cruiser.
            char_dic[f"can_light"] = True
        if char_dic[f"s_cov_cyno"] > 0 and char_dic[f"s_expfrig"] > 0 and char_dic[f"i_expfrig"] == True:  # Cov cyno + exploration frig.
            char_dic[f"can_light"] = True
        result[name] = char_dic

    return result


def owns_items_in_group(cid, gid):
    """
    Return True when the character has at least one hull in the given
    EVE group id (e.g. recon ships). Used to gauge practical readiness.
    """
    if not corptools_active() or CharacterAsset is None:
        return False
    exists = CharacterAsset.objects.filter(
        character__character__character_id=cid,
        type_name__group_id=gid
    ).exists()

    return exists


def render_user_cyno_info_html(user_id: int) -> str:
    """
    Returns an HTML snippet showing, for each of the user's characters:
      - for each skill: whether they can use it (no / yes but alpha / yes) and whether they own ships in that group
      - whether they can light cynos (can_light)
      - character age
    """
    data = get_user_cyno_info(user_id)
    html = ""

    for char_name, info in data.items():
        # header
        html += format_html("<h3>{}</h3>", char_name)

        # table start
        html += """
        <table class="table table-striped table-hover stats">
          <thead>
            <tr>
              <th>Name</th><th>Can use</th><th>Owns ships</th>
            </tr>
          </thead>
          <tbody>
        """

        # loop through each skill
        for key, label in (
            ("cyno",     "Cynosural Field"),
            ("cov_cyno", "Cynosural Field 5"),
            ("recon",    "Recon"),
            ("hic",      "HIC"),
            ("blops",    "Black Ops"),
            ("covops",   "Covert Ops"),
            ("brun",     "Blockade Runners"),
            ("sbomb",    "Stealth Bombers"),
            ("scru",     "Stategic Cruisers"),
            ("expfrig",  "Exploration Frigates"),
            ("carrier",  "Carriers"),
            ("dread",    "Dreads"),
            ("fax",      "FAXes"),
            ("super",    "Supers"),
            ("titan",    "Titans"),
            ("jf",       "Jump Freighters"),
            ("rorq",     "Rorquals"),
        ):
            s = info[f"s_{key}"]
            # map trained/active flag to human text
            if s == 0:  # No training whatsoever.
                s_txt = "False"
            elif s == 1:  # Trained but alpha (passive) status.
                s_txt = mark_safe('<span class="text-warning">True (but alpha)</span>')
            else:  # s == 2
                s_txt = mark_safe('<span class="text-danger">True</span>')
            # only cyno has no “owns” flag
            if info.get(f"i_{key}", "") == True:  # Highlight ship ownership in red when true.
                owns = mark_safe(f'<span class="text-danger">{info.get(f"i_{key}", "")}</span>')
            else:
                owns = f'{info.get(f"i_{key}", "")}'
            html += format_html(
                "<tr><td>{}</td><td>{}</td><td>{}</td></tr>",
                label, s_txt, owns
            )

        # add the “can light?” and age rows
        if info["can_light"] == True:  # Emphasize characters that can currently light cynos.
            can_light = mark_safe(f'<span class="text-danger">{info["can_light"]}</span>')
        else:
            can_light = f'{info["can_light"]}'
        if info["age"] < 90:  # Flag younger characters; cyno alts often need vetting.
            age = mark_safe(f'<span class="text-danger">{info["age"]}</span>')
        else:
            age = f'{info["age"]}'
        html += format_html(
            "<tr><td>Can light?</td><td colspan='2'>{}</td></tr>",
            can_light
        )
        html += format_html(
            "<tr><td>Age</td><td colspan='2'>{}</td></tr>",
            age
        )
        cid = get_character_id(char_name)
        corp_label = f"Time in {bbc.get_solo().main_corporation}"
        days_in_corp = get_current_stint_days_in_corp(cid,bbc.get_solo().main_corporation_id)
        days_html = f"{days_in_corp} days"

        html += format_html(
            "<tr><td>{}</td><td colspan='2'>{}</td></tr>",
            corp_label, days_html
        )

        # table end
        html += "</tbody></table>"

    return format_html(html)
