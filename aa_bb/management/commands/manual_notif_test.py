import logging
import time
from django.core.management.base import BaseCommand
from django.utils import timezone

from aa_bb.tasks import send_status_embed
from aa_bb.app_settings import get_pings, send_message

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Test BigBrother Discord notifications with dummy data representing detected changes."

    def handle(self, *args, **options):
        self.stdout.write("Preparing test data...")

        # 1. The Test Data -----------------------------------------------------

        awox_data = ["https://zkillboard.com/kill/64383439/"]

        cyno_data = {
            "Mei Egdald": {
                "s_cyno": 2,
                "s_cov_cyno": 0,
                "s_recon": 0,
                "s_hic": 0,
                "s_blops": 0,
                "s_covops": 0,
                "s_brun": 0,
                "s_sbomb": 0,
                "s_scru": 0,
                "s_expfrig": 0,
                "s_carrier": 0,
                "s_dread": 0,
                "s_fax": 0,
                "s_super": 0,
                "s_titan": 0,
                "s_jf": 0,
                "s_rorq": 0,
                "i_recon": False,
                "i_hic": False,
                "i_blops": False,
                "i_covops": False,
                "i_brun": False,
                "i_sbomb": False,
                "i_scru": False,
                "i_expfrig": False,
                "i_carrier": False,
                "i_dread": False,
                "i_fax": False,
                "i_super": False,
                "i_titan": False,
                "i_jf": False,
                "i_rorq": False,
                "age": 4643,
                "can_light": False,
            },
            "BioBrute": {
                "s_cyno": 2,
                "s_cov_cyno": 0,
                "s_recon": 2,
                "s_hic": 2,
                "s_blops": 2,
                "s_covops": 2,
                "s_brun": 2,
                "s_sbomb": 2,
                "s_scru": 2,
                "s_expfrig": 0,
                "s_carrier": 2,
                "s_dread": 2,
                "s_fax": 2,
                "s_super": 2,
                "s_titan": 0,
                "s_jf": 0,
                "s_rorq": 0,
                "i_recon": False,
                "i_hic": False,
                "i_blops": True,
                "i_covops": True,
                "i_brun": True,
                "i_sbomb": True,
                "i_scru": True,
                "i_expfrig": False,
                "i_carrier": True,
                "i_dread": True,
                "i_fax": False,
                "i_super": False,
                "i_titan": False,
                "i_jf": False,
                "i_rorq": False,
                "age": 6621,
                "can_light": True,
            },
            "Silken Lace": {
                "s_cyno": 2,
                "s_cov_cyno": 0,
                "s_recon": 0,
                "s_hic": 0,
                "s_blops": 0,
                "s_covops": 0,
                "s_brun": 2,
                "s_sbomb": 0,
                "s_scru": 2,
                "s_expfrig": 2,
                "s_carrier": 0,
                "s_dread": 0,
                "s_fax": 0,
                "s_super": 0,
                "s_titan": 0,
                "s_jf": 0,
                "s_rorq": 0,
                "i_recon": False,
                "i_hic": False,
                "i_blops": False,
                "i_covops": False,
                "i_brun": True,
                "i_sbomb": False,
                "i_scru": False,
                "i_expfrig": True,
                "i_carrier": False,
                "i_dread": False,
                "i_fax": False,
                "i_super": False,
                "i_titan": False,
                "i_jf": False,
                "i_rorq": False,
                "age": 5555,
                "can_light": False,
            },
            "Ima Spying": {
                "s_cyno": 2,
                "s_cov_cyno": 2,
                "s_recon": 2,
                "s_hic": 0,
                "s_blops": 0,
                "s_covops": 2,
                "s_brun": 2,
                "s_sbomb": 2,
                "s_scru": 0,
                "s_expfrig": 2,
                "s_carrier": 2,
                "s_dread": 2,
                "s_fax": 2,
                "s_super": 2,
                "s_titan": 0,
                "s_jf": 2,
                "s_rorq": 0,
                "i_recon": True,
                "i_hic": False,
                "i_blops": False,
                "i_covops": False,
                "i_brun": True,
                "i_sbomb": True,
                "i_scru": False,
                "i_expfrig": True,
                "i_carrier": False,
                "i_dread": False,
                "i_fax": False,
                "i_super": False,
                "i_titan": False,
                "i_jf": True,
                "i_rorq": False,
                "age": 5988,
                "can_light": True,
            },
            "Heroclees": {
                "s_cyno": 0,
                "s_cov_cyno": 0,
                "s_recon": 0,
                "s_hic": 0,
                "s_blops": 0,
                "s_covops": 0,
                "s_brun": 2,
                "s_sbomb": 0,
                "s_scru": 0,
                "s_expfrig": 0,
                "s_carrier": 0,
                "s_dread": 0,
                "s_fax": 0,
                "s_super": 0,
                "s_titan": 0,
                "s_jf": 0,
                "s_rorq": 0,
                "i_recon": False,
                "i_hic": False,
                "i_blops": False,
                "i_covops": False,
                "i_brun": True,
                "i_sbomb": False,
                "i_scru": False,
                "i_expfrig": False,
                "i_carrier": False,
                "i_dread": False,
                "i_fax": False,
                "i_super": False,
                "i_titan": False,
                "i_jf": False,
                "i_rorq": False,
                "age": 6227,
                "can_light": False,
            },
            "Mei Fen": {
                "s_cyno": 2,
                "s_cov_cyno": 2,
                "s_recon": 2,
                "s_hic": 0,
                "s_blops": 2,
                "s_covops": 2,
                "s_brun": 2,
                "s_sbomb": 2,
                "s_scru": 2,
                "s_expfrig": 2,
                "s_carrier": 2,
                "s_dread": 2,
                "s_fax": 2,
                "s_super": 2,
                "s_titan": 0,
                "s_jf": 2,
                "s_rorq": 2,
                "i_recon": True,
                "i_hic": False,
                "i_blops": False,
                "i_covops": False,
                "i_brun": True,
                "i_sbomb": False,
                "i_scru": True,
                "i_expfrig": True,
                "i_carrier": False,
                "i_dread": False,
                "i_fax": False,
                "i_super": False,
                "i_titan": False,
                "i_jf": False,
                "i_rorq": True,
                "age": 6029,
                "can_light": True,
            },
            "Brood2": {
                "s_cyno": 0,
                "s_cov_cyno": 0,
                "s_recon": 0,
                "s_hic": 0,
                "s_blops": 0,
                "s_covops": 0,
                "s_brun": 2,
                "s_sbomb": 0,
                "s_scru": 0,
                "s_expfrig": 0,
                "s_carrier": 0,
                "s_dread": 0,
                "s_fax": 0,
                "s_super": 0,
                "s_titan": 0,
                "s_jf": 0,
                "s_rorq": 0,
                "i_recon": False,
                "i_hic": False,
                "i_blops": False,
                "i_covops": False,
                "i_brun": True,
                "i_sbomb": False,
                "i_scru": False,
                "i_expfrig": False,
                "i_carrier": False,
                "i_dread": False,
                "i_fax": False,
                "i_super": False,
                "i_titan": False,
                "i_jf": False,
                "i_rorq": False,
                "age": 1777,
                "can_light": False,
            },
        }

        skills_data = {
            "Mei Egdald": {
                "total_sp": 329504,
                "3426": {"trained": 5, "active": 5},
                "21603": {"trained": 2, "active": 2},
                "22761": {"trained": 0, "active": 0},
                "28609": {"trained": 0, "active": 0},
                "28656": {"trained": 0, "active": 0},
                "12093": {"trained": 0, "active": 0},
                "20533": {"trained": 0, "active": 0},
                "19719": {"trained": 0, "active": 0},
                "30651": {"trained": 0, "active": 0},
                "30652": {"trained": 0, "active": 0},
                "30653": {"trained": 0, "active": 0},
                "30650": {"trained": 0, "active": 0},
                "33856": {"trained": 0, "active": 0},
            },
            "BioBrute": {
                "total_sp": 228724829,
                "3426": {"trained": 5, "active": 5},
                "21603": {"trained": 4, "active": 4},
                "22761": {"trained": 4, "active": 4},
                "28609": {"trained": 4, "active": 4},
                "28656": {"trained": 5, "active": 5},
                "12093": {"trained": 5, "active": 5},
                "20533": {"trained": 5, "active": 5},
                "19719": {"trained": 4, "active": 4},
                "30651": {"trained": 5, "active": 5},
                "30652": {"trained": 5, "active": 5},
                "30653": {"trained": 5, "active": 5},
                "30650": {"trained": 5, "active": 5},
                "33856": {"trained": 0, "active": 0},
            },
            "Silken Lace": {
                "total_sp": 42839412,
                "3426": {"trained": 5, "active": 5},
                "21603": {"trained": 4, "active": 4},
                "22761": {"trained": 0, "active": 0},
                "28609": {"trained": 0, "active": 0},
                "28656": {"trained": 0, "active": 0},
                "12093": {"trained": 0, "active": 0},
                "20533": {"trained": 0, "active": 0},
                "19719": {"trained": 4, "active": 4},
                "30651": {"trained": 4, "active": 4},
                "30652": {"trained": 0, "active": 0},
                "30653": {"trained": 0, "active": 0},
                "30650": {"trained": 0, "active": 0},
                "33856": {"trained": 5, "active": 5},
            },
            "Ima Spying": {
                "total_sp": 128821874,
                "3426": {"trained": 5, "active": 5},
                "21603": {"trained": 5, "active": 5},
                "22761": {"trained": 4, "active": 4},
                "28609": {"trained": 0, "active": 0},
                "28656": {"trained": 0, "active": 0},
                "12093": {"trained": 4, "active": 4},
                "20533": {"trained": 5, "active": 5},
                "19719": {"trained": 4, "active": 4},
                "30651": {"trained": 0, "active": 0},
                "30652": {"trained": 0, "active": 0},
                "30653": {"trained": 0, "active": 0},
                "30650": {"trained": 0, "active": 0},
                "33856": {"trained": 5, "active": 5},
            },
            "Heroclees": {
                "total_sp": 35417380,
                "3426": {"trained": 5, "active": 5},
                "21603": {"trained": 0, "active": 0},
                "22761": {"trained": 0, "active": 0},
                "28609": {"trained": 0, "active": 0},
                "28656": {"trained": 0, "active": 0},
                "12093": {"trained": 0, "active": 0},
                "20533": {"trained": 0, "active": 0},
                "19719": {"trained": 1, "active": 1},
                "30651": {"trained": 0, "active": 0},
                "30652": {"trained": 0, "active": 0},
                "30653": {"trained": 0, "active": 0},
                "30650": {"trained": 0, "active": 0},
                "33856": {"trained": 0, "active": 0},
            },
            "Mei Fen": {
                "total_sp": 169638826,
                "3426": {"trained": 5, "active": 5},
                "21603": {"trained": 5, "active": 5},
                "22761": {"trained": 5, "active": 5},
                "28609": {"trained": 0, "active": 0},
                "28656": {"trained": 4, "active": 4},
                "12093": {"trained": 4, "active": 4},
                "20533": {"trained": 4, "active": 4},
                "19719": {"trained": 5, "active": 5},
                "30651": {"trained": 5, "active": 5},
                "30652": {"trained": 0, "active": 0},
                "30653": {"trained": 0, "active": 0},
                "30650": {"trained": 0, "active": 0},
                "33856": {"trained": 5, "active": 5},
            },
            "Brood2": {
                "total_sp": 26661111,
                "3426": {"trained": 5, "active": 5},
                "21603": {"trained": 0, "active": 0},
                "22761": {"trained": 0, "active": 0},
                "28609": {"trained": 0, "active": 0},
                "28656": {"trained": 0, "active": 0},
                "12093": {"trained": 0, "active": 0},
                "20533": {"trained": 0, "active": 0},
                "19719": {"trained": 1, "active": 1},
                "30651": {"trained": 0, "active": 0},
                "30652": {"trained": 0, "active": 0},
                "30653": {"trained": 0, "active": 0},
                "30650": {"trained": 0, "active": 0},
                "33856": {"trained": 0, "active": 0},
            },
        }

        hostile_assets_data = {}
        hostile_clones_data = {}

        # 2. Build & Send ------------------------------------------------------

        COLOR_RED = 0xE74C3C
        COLOR_GREEN = 0x2ECC71

        char_name = "BioBrute"

        # Overall header (single embed)
        send_status_embed(
            char_name,
            ["‼️ Status change detected for BioBrute"],
            override_title="",
        )
        time.sleep(1)

        # --- AWOX -------------------------------------------------------------
        if awox_data:
            has_awox = True
            color = COLOR_RED if has_awox else COLOR_GREEN

            awox_ping = get_pings("AwoX")
            if awox_ping:
                send_message(f"{awox_ping} New AWOX Kill(s) detected for {char_name}")

            link_list = "\n".join(f"- {link}" for link in awox_data)
            body = [
                f"### AWOX Kill Status: {'🔴' if has_awox else '🟢'}",
                "New AWOX Kill(s):",
                link_list,
            ]
            send_status_embed(char_name, body, override_title="", override_color=color)
            time.sleep(1)

        # --- Cyno -------------------------------------------------------------
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
        }

        cyno_keys = [
            "s_cyno",
            "s_cov_cyno",
            "s_recon",
            "s_hic",
            "s_blops",
            "s_covops",
            "s_sbomb",
            "s_scru",
            "s_expfrig",
            "s_brun",
            "s_carrier",
            "s_dread",
            "s_fax",
            "s_super",
            "s_titan",
            "s_jf",
            "s_rorq",
        ]

        has_cyno = any(entry.get("can_light", False) for entry in cyno_data.values())
        cyno_color = COLOR_RED if has_cyno else COLOR_GREEN

        # Global Cyno ping
        all_cyno_ping = get_pings("All Cyno Changes")
        if all_cyno_ping:
            send_message(f"{all_cyno_ping} Changes in cyno capabilities detected for {char_name}")

        # Cyno section header
        send_status_embed(
            char_name,
            [
                f"### Cyno Status: {'🔴' if has_cyno else '🟢'}",
                "Changes in cyno capabilities detected:",
            ],
            override_title="",
            override_color=cyno_color,
        )
        time.sleep(1)

        # Per-character Cyno tables
        for cname, new_entry in cyno_data.items():
            if not any(val in (1, 2, 3, 4, 5) for key, val in new_entry.items() if key.startswith("s_")):
                continue

            # Per-character ping
            if new_entry.get("can_light", False):
                pingrole = get_pings("Can Light Cyno")
            else:
                pingrole = get_pings("Cyno Update")
            if pingrole:
                send_message(f"{pingrole} Cyno update for {cname}")

            preamble = "1 = trained but alpha, 2 = active"
            header = "Value                     | Old | New"
            sep_line = "-" * len(header)

            table_lines = [preamble, header, sep_line]

            for key in cyno_keys:
                display = cyno_display.get(key, key)
                old_val = "0"
                new_val = str(new_entry.get(key, 0))
                row = f"{display.ljust(25)} | {old_val.ljust(3)} | {new_val.ljust(3)}"
                table_lines.append(row)
                if new_val != old_val:
                    table_lines.append("-" * len(header))

            can_light_new = new_entry.get("can_light", False)
            table_lines.append("")
            table_lines.append(f"{'Can Light':<25} | No  | {'Yes' if can_light_new else 'No'}")
            table_lines.append(f"{'Time in Corp':<25} | ??? days")

            table_block = "```\n" + "\n".join(table_lines) + "\n```"

            send_status_embed(
                char_name,
                [f"- {cname}:", table_block],
                override_title="",
                override_color=cyno_color,
            )
            time.sleep(1)

        # --- Skills -----------------------------------------------------------
        skill_names = {
            3426: "CPU Management",
            21603: "Cynosural Field Theory",
            22761: "Recon Ships",
            28609: "HIC",
            28656: "Black Ops",
            12093: "Covert Ops",
            20533: "Capital Ships",
            19719: "Blockade Runners",
            30651: "Caldari T3C",
            30652: "Gallente T3C",
            30653: "Minmatar T3C",
            30650: "Amarr T3C",
            33856: "Expedition Frigates",
        }

        ordered_skill_ids = [
            3426,
            21603,
            22761,
            28609,
            28656,
            12093,
            20533,
            19719,
            30651,
            30652,
            30653,
            30650,
            33856,
        ]

        has_skills = True
        skills_color = COLOR_RED if has_skills else COLOR_GREEN

        skills_ping = get_pings("skills")
        if skills_ping:
            send_message(f"{skills_ping} Changes in skills detected for {char_name}")

        send_status_embed(
            char_name,
            [
                f"### Skill Status: {'🔴' if has_skills else '🟢'}",
                "Changes in skills detected:",
            ],
            override_title="",
            override_color=skills_color,
        )
        time.sleep(1)

        for cname, new_entry in skills_data.items():
            header = "Skill                           | Old (Trained/Active) | New (Trained/Active)"
            sep_line = "-" * len(header)
            table_lines = [header, sep_line]

            for sid in ordered_skill_ids:
                name = skill_names.get(sid, f"Skill ID {sid}")
                new_skill = new_entry.get(str(sid), {"trained": 0, "active": 0})
                old_fmt = "0/0"
                new_fmt = f"{new_skill.get('trained', 0)}/{new_skill.get('active', 0)}"
                row = f"{name.ljust(30)} | {old_fmt.ljust(9)} | {new_fmt.ljust(9)}"
                table_lines.append(row)
                if new_fmt != old_fmt:
                    table_lines.append("-" * len(header))

            table_block = "```\n" + "\n".join(table_lines) + "\n```"

            send_status_embed(
                char_name,
                [f"- {cname}:", table_block],
                override_title="",
                override_color=skills_color,
            )
            time.sleep(1)

        # --- Hostile Assets ---------------------------------------------------
        if hostile_assets_data:
            has_hostile_assets = True
            assets_color = COLOR_RED if has_hostile_assets else COLOR_GREEN

            ha_ping = get_pings("New Hostile Assets")
            if ha_ping:
                send_message(f"{ha_ping} New hostile assets detected for {char_name}")

            link_list = "\n".join(
                f"- {system} owned by {owner}" for system, owner in hostile_assets_data.items()
            )
            send_status_embed(
                char_name,
                [
                    f"### Hostile Asset Status: {'🔴' if has_hostile_assets else '🟢'}",
                    "New Hostile Assets:",
                    link_list,
                ],
                override_title="",
                override_color=assets_color,
            )
            time.sleep(1)

        # --- Hostile Clones ---------------------------------------------------
        if hostile_clones_data:
            has_hostile_clones = True
            clones_color = COLOR_RED if has_hostile_clones else COLOR_GREEN

            hc_ping = get_pings("New Hostile Clones")
            if hc_ping:
                send_message(f"{hc_ping} New hostile clones detected for {char_name}")

            link_list = "\n".join(
                f"- {system} owned by {owner}" for system, owner in hostile_clones_data.items()
            )
            send_status_embed(
                char_name,
                [
                    f"### Hostile Clone Status: {'🔴' if has_hostile_clones else '🟢'}",
                    "New Hostile Clone(s):",
                    link_list,
                ],
                override_title="",
                override_color=clones_color,
            )

        self.stdout.write(self.style.SUCCESS("Sent."))
