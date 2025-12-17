import os
import matplotlib.pyplot as plt
import calendar
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required, permission_required
from django.views.decorators.http import require_POST
from django.conf import settings
from django.utils.timezone import now
from django.urls import reverse

from .models import BigBrotherConfig, PapsConfig, PapCompliance, TicketToolConfig, LeaveRequest
from .app_settings import get_user_profiles, get_user_characters, afat_active

try:
    from afat.models import Fat
except ImportError:
    logger.error("afat not installed; PAP generation will not work.")

@login_required
@permission_required("aa_bb.can_generate_paps")
def index(request):
    """Render the PAP entry form allowing recruiters to input monthly stats."""
    cfg = BigBrotherConfig.get_solo()
    if not cfg.is_paps_active:  # Disable UI when PAP module turned off.
        return render(request, "paps/disabled.html")

    today = now()
    month = int(request.GET.get("month", today.month-1))
    year = int(request.GET.get("year", today.year))

    users_data = []
    profiles = get_user_profiles()
    profile_dict = {p.main_character.character_name: p for p in profiles}

    bulk_data = ""
    error_messages = []

    # Handle bulk data POST
    if request.method == "POST":  # Bulk upload mode; parse pasted values into POST fields.
        bulk_data = request.POST.get("bulk_data", "")
        lines = bulk_data.strip().split("\n")
        for line in lines:
            parts = line.strip().split()
            if len(parts) < 3:  # Need at least name + two columns.
                continue
            player_name = " ".join(parts[:-2])
            try:
                alliance = int(parts[-2])
                coalition  = int(parts[-1])
            except ValueError:
                error_messages.append(player_name)
                continue

            profile = profile_dict.get(player_name)
            if not profile:  # Unknown character name in bulk list.
                error_messages.append(player_name)
                continue

            # Save PAPs to POST fields so table inputs get prefilled
            request.POST = request.POST.copy()
            request.POST[f"alliance_paps_{profile.user.id}"] = alliance
            request.POST[f"coalition_paps_{profile.user.id}"] = coalition

    # Build table data
    for profile in profiles:
        excluded_users = PapsConfig.get_solo().excluded_users.all()
        if profile.user in excluded_users:  # Skip explicitly excluded users.
            continue
        user_id = profile.user.id
        characters = get_user_characters(user_id)
        corp_paps = 0
        alliance_paps = 0
        coalition_paps = 0
        user_groups = profile.user.groups.values_list("name", flat=True)
        auth_groups = PapsConfig.get_solo().group_paps.all()
        excluded_groups = PapsConfig.get_solo().excluded_groups.all()
        group_names = [ag.group.name for ag in auth_groups]
        user_groups_set = set(user_groups)
        auth_groups_set = set(group_names)
        excluded_group_names = [eg.group.name for eg in excluded_groups]
        excluded_groups_set = set(excluded_group_names)

        # Count how many overlap
        matching_count = len(user_groups_set & auth_groups_set)
        excluded = bool(user_groups_set & excluded_groups_set)  # True if user belongs to any excluded group.
        excluded_users_paps = PapsConfig.get_solo().excluded_users_paps.all()
        if profile.user not in excluded_users_paps:  # Only award group PAPs when user not excluded.
            if not excluded:  # Normal path: add modifier times group overlaps.
                corp_paps = corp_paps + matching_count * PapsConfig.get_solo().group_paps_modifier
            else:
                if PapsConfig.get_solo().excluded_groups_get_paps:  # Optionally still award excluded groups.
                    corp_paps = corp_paps + PapsConfig.get_solo().group_paps_modifier

        def _group_name(g):
            if not g:
                return None
            # if the FK points to a wrapper that has `.group`, use that; else assume it's auth.Group
            target = getattr(g, "group", g)
            return getattr(target, "name", None)

        cfg = PapsConfig.get_solo()

        if cfg.capital_groups_get_paps:  # Optional extra PAP points for capital pilots.

            cap_name   = _group_name(cfg.cap_group)
            super_name = _group_name(cfg.super_group)
            titan_name = _group_name(cfg.titan_group)

            paps_to_add = 0
            if cap_name and cap_name in user_groups_set:
                paps_to_add = cfg.cap_group_paps
            if super_name and super_name in user_groups_set:
                paps_to_add = cfg.super_group_paps
            if titan_name and titan_name in user_groups_set:
                paps_to_add = cfg.titan_group_paps

            if paps_to_add:
                corp_paps += paps_to_add


        if afat_active():  # Count Alliance FATs per character for corp PAP totals.
            for char in characters:
                fats = Fat.objects.filter(
                    character__character_id=char,
                    fatlink__created__month=month,
                    fatlink__created__year=year,
                )
                corp_paps += fats.count()

        # Override with POSTed values (manual or bulk)
        if request.method == "POST":  # Manual overrides from form submission.
            corp_paps = int(request.POST.get(f"corp_paps_{user_id}", corp_paps))
            alliance_paps = int(request.POST.get(f"alliance_paps_{user_id}", alliance_paps))
            coalition_paps = int(request.POST.get(f"coalition_paps_{user_id}", coalition_paps))

        users_data.append({
            "user": profile,
            "corp_paps": corp_paps,
            "alliance_paps": alliance_paps,
            "coalition_paps": coalition_paps,
        })

    return render(
        request,
        "paps/index.html",
        {
            "users_data": users_data,
            "month": month,
            "year": year,
            "bulk_data": bulk_data,
            "error_messages": error_messages,
        },
    )


@login_required
@permission_required("aa_bb.can_access_paps")
def history(request):
    """Show previously generated PAP charts (if they exist)."""
    cfg = BigBrotherConfig.get_solo()
    if not cfg.is_paps_active:  # Respect module toggle.
        return render(request, "paps/disabled.html")

    today = now()
    month = int(request.GET.get("month", today.month-1))
    year = int(request.GET.get("year", today.year))

    # Runtime chart folder
    runtime_dir = os.path.join(settings.MEDIA_ROOT, "paps")
    filename = f"pap_chart_{year}_{month}.png"
    chart_full_path = os.path.join(runtime_dir, filename)
    chart_exists = os.path.isfile(chart_full_path)

    # URL to serve in template
    chart_url = f"{settings.MEDIA_URL}paps/{filename}"

    return render(request, "paps/history.html", {
        "month": month,
        "year": year,
        "chart_exists": chart_exists,
        "chart_url": chart_url,
    })


@require_POST
@login_required
@permission_required("aa_bb.can_generate_paps")
def generate_pap_chart(request):
    """Process the submitted PAP form and produce a stacked contribution chart."""
    month = int(request.POST.get("month"))
    year = int(request.POST.get("year"))

    max_compliance = TicketToolConfig.get_solo().max_months_without_pap_compliance or 0
    starting_compliance = TicketToolConfig.get_solo().starting_pap_compliance or 1

    # Gather submitted PAP values
    users_data = []
    excluded_users = PapsConfig.get_solo().excluded_users.all()
    conf = PapsConfig.get_solo()
    for profile in get_user_profiles():
        if profile.user in excluded_users:  # Skip excluded pilots entirely.
            continue
        lr_qs = LeaveRequest.objects.filter(
                user=profile.user,
                status="in_progress",
            ).exists()
        if lr_qs:  # Ignore LoA members still on leave.
            continue
        user_id = profile.user.id
        corp_raw = int(request.POST.get(f"corp_paps_{user_id}", 0)) * conf.corp_modifier
        corp_paps = min(corp_raw, conf.max_corp_paps)  # cap at configured maximum
        alliance_paps = int(request.POST.get(f"alliance_paps_{user_id}", 0)) * conf.alliance_modifier
        coalition_paps = int(request.POST.get(f"coalition_paps_{user_id}", 0)) * conf.coalition_modifier
        corp_ab = corp_raw - conf.max_corp_paps
        if corp_ab < 0:
            corp_ab = 0
        users_data.append({
            "name": profile.main_character.character_name,
            "corp": corp_paps,
            "corp_ab": corp_ab,
            "alliance": alliance_paps,
            "coalition": coalition_paps,
        })
        # ✅ Update PapCompliance
        if max_compliance != 0:  # Update PAP compliance meter when feature enabled.
            pc, _ = PapCompliance.objects.get_or_create(
                user_profile=profile,
                defaults={"pap_compliant": starting_compliance},
            )
            total_capped = corp_paps + alliance_paps + coalition_paps
            if total_capped >= conf.required_paps:
                pc.pap_compliant = min(pc.pap_compliant + 1, max_compliance)
            else:
                pc.pap_compliant = max(pc.pap_compliant - 1, 0)  # keep it non-negative
            pc.save(update_fields=["pap_compliant"])


    # Chart save path
    app_static_dir = os.path.join(settings.MEDIA_ROOT, "paps")
    os.makedirs(app_static_dir, exist_ok=True)
    filename = f"pap_chart_{year}_{month}.png"
    filepath = os.path.join(app_static_dir, filename)

    # Generate stacked chart
    fig, ax = plt.subplots(figsize=(12, 6))  # Build stacked bar chart in neutral dark theme.
    fig.patch.set_facecolor('#4B4B4B')  # Dark grey background
    ax.set_facecolor('#4B4B4B')

    names = [u["name"] for u in users_data]
    corp = [u["corp"] for u in users_data]
    corp_abo = [u["corp_ab"] for u in users_data]
    alliance = [u["alliance"] for u in users_data]
    coalition  = [u["coalition"] for u in users_data]

    x = range(len(names))
    corp_m = conf.corp_modifier
    corp_max = conf.max_corp_paps
    alliance_m = conf.alliance_modifier
    coalition_m = conf.coalition_modifier




    # Bottom: Alliance
    ax.bar(x, alliance, label=f"Alliance Paps(x{alliance_m})", color="#58D68D")

    # Next: Coalition
    ax.bar(x, coalition, bottom=alliance, label=f"Coalition Paps(x{coalition_m})", color="#F5B041")
    bottom_stack = [l + im for l, im in zip(alliance, coalition)]

    # Next: Corp (capped part)
    ax.bar(x, corp, bottom=bottom_stack, label=f"Corp Paps(x{corp_m}, max {corp_max})", color="#5DADE2")
    corp_stack = [b + c for b, c in zip(bottom_stack, corp)]

    # Top: Corp above cap
    ax.bar(x, corp_abo, bottom=corp_stack, label=f"Corp Paps above {corp_max}(x{corp_m})", color="#9EC5DF")

    # Horizontal red dashed line at y=6
    ax.axhline(y=conf.required_paps, color='red', linestyle='--', linewidth=2, label='PAP Requirement')

    # Labels and style
    ax.set_xticks(x)
    ax.set_xticklabels(names, rotation=45, ha="right", color='white')
    ax.set_ylabel("Total Paps", color='white')
    month_name = calendar.month_name[month]
    main_corporation = BigBrotherConfig.get_solo().main_corporation
    ax.set_title(f"{main_corporation} Fleet Breakdown for {month_name} {year}", color='white', fontweight='bold')

    # All spines and ticks in white
    ax.spines['bottom'].set_color('black')
    ax.spines['top'].set_color('black')
    ax.spines['left'].set_color('black')
    ax.spines['right'].set_color('black')
    ax.tick_params(axis='x', colors='white')
    ax.tick_params(axis='y', colors='white')

    # Legend in top-right
    ax.legend(loc='upper right', facecolor='#4B4B4B', edgecolor='white', labelcolor='white')

    # Add labels above the stacked bars
    for i, (l, im, c) in enumerate(zip(alliance, coalition, corp)):  # Label capped totals in contrasting color.
        total = l + im + c
        coll = 'red'
        if total >= conf.required_paps:
            coll = 'white'
        ax.text(i, total, str(total), ha='center', va='bottom', color=coll, fontsize=10)

    for i, (l, im, c, ca) in enumerate(zip(alliance, coalition, corp, corp_abo)):  # Label above-cap contributions.
        total = l + im + c
        total_c = total + ca
        coll = 'white'
        if total != total_c:
            ax.text(i, total_c, str(total_c), ha='center', va='bottom', color=coll, fontsize=10)

    # Determine the max total height of stacked bars
    max_total = max([l + im + c + ca for l, im, c, ca in zip(alliance, coalition, corp, corp_abo)])
    if max_total < conf.required_paps:
        max_total = conf.required_paps

    # Add some padding (like 10% extra)
    ax.set_ylim(0, max_total *  1.1)

    plt.tight_layout()
    plt.savefig(filepath, dpi=150, facecolor=fig.get_facecolor())  # save with background
    plt.close(fig)

    # Redirect to history page
    return redirect(f"{reverse('paps:history')}?month={month}&year={year}")
