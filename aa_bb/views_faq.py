import secrets
import requests
from urllib.parse import quote_plus

from django.apps import apps as django_apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.handlers.wsgi import WSGIRequest
from django.db import OperationalError, ProgrammingError
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django_celery_beat.models import PeriodicTask

from .models import (
    BigBrotherConfig,
    PapsConfig,
    TicketToolConfig,
    BigBrotherRedditSettings,
    BigBrotherRedditMessage
)

from .reddit import (
    reddit_status,
    reddit_app_configured,
)


@login_required
@permission_required("aa_bb.basic_access")
def manual_cards(request: WSGIRequest):
    """Manual tab: card reference."""
    return render(request, "faq/cards.html")


@login_required
@permission_required("aa_bb.basic_access")
def manual_settings(request: WSGIRequest):
    """Manual tab: BigBrotherConfig settings."""
    return render(request, "faq/settings_bigbrother.html")


@login_required
@permission_required("aa_bb.basic_access")
def manual_settings_bb(request: WSGIRequest):
    """Alias for BigBrotherConfig settings."""
    return render(request, "faq/settings_bigbrother.html")


@login_required
@permission_required("aa_bb.basic_access")
def manual_settings_paps(request: WSGIRequest):
    """Manual tab: PapsConfig settings."""
    return render(request, "faq/settings_paps.html")


@login_required
@permission_required("aa_bb.basic_access")
def manual_settings_tickets(request: WSGIRequest):
    """Manual tab: TicketToolConfig settings."""
    return render(request, "faq/settings_tickets.html")


@login_required
@permission_required("aa_bb.basic_access")
def manual_modules(request: WSGIRequest):
    """Manual tab: module status overview with live checks."""
    cfg = BigBrotherConfig.get_solo()
    paps_cfg = PapsConfig.get_solo()
    ticket_cfg_error = None
    try:
        ticket_cfg = TicketToolConfig.get_solo()
    except (OperationalError, ProgrammingError) as exc:
        ticket_cfg = None
        ticket_cfg_error = str(exc)

    reddit_cfg = None
    reddit_cfg_error = None
    reddit_messages_error = None
    reddit_messages_count = 0
    reddit_entitled = True
    if reddit_entitled:
        try:
            reddit_cfg = BigBrotherRedditSettings.get_solo()
        except (OperationalError, ProgrammingError) as exc:
            reddit_cfg_error = str(exc)
        else:  # Config exists, so count available recruitment messages.
            try:
                reddit_messages_count = BigBrotherRedditMessage.objects.count()
            except (OperationalError, ProgrammingError) as exc:
                reddit_messages_error = str(exc)

    task_name = "BB run regular updates"
    periodic_task = PeriodicTask.objects.filter(name=task_name).first()

    corptools_installed = django_apps.is_installed("corptools")
    charlink_installed = django_apps.is_installed("charlink")
    blacklist_installed = django_apps.is_installed("blacklist")
    discordbot_installed = django_apps.is_installed("aadiscordbot")

    modules = []

    def code(name: str):
        """Wrap a label/value in <code> for easier reading."""
        return format_html("<code>{}</code>", name)

    def register_issue(issues: list, actions: list, condition: bool, issue_text, action_text=None):
        """Append issue/action entries when the guard condition is met."""
        if condition:  # Only record when the configuration check failed.
            issues.append(issue_text)
            if action_text and action_text not in actions:  # Actions deduped to avoid noise.
                actions.append(action_text)

    def make_module(name, summary, issues, actions, info=None, active_override=None, cta=None):
        """Build a dict consumed by the FAQ template."""
        info = info or []
        issues = list(dict.fromkeys(issues))
        actions = list(dict.fromkeys(actions))
        active = active_override if active_override is not None else (len(issues) == 0)
        if issues:  # When problems exist, show them plus any informational notes.
            details = issues + info
        else:
            details = [format_html("{}", _("All requirements satisfied."))] + info
        if not actions:  # Provide default CTA messaging when nothing actionable supplied.
            actions = [format_html("{}", _("No action needed."))] if not issues else [format_html("{}", _("Review configuration and retry the checks."))]
        return {
            "name": name,
            "summary": summary,
            "active": bool(active),
            "details": details,
            "actions": actions,
            "cta": cta,
        }

    # BigBrother Core Dashboard
    core_issues, core_actions, core_info = [], [], []
    register_issue(
        core_issues,
        core_actions,
        not cfg.is_active,
        format_html("{} reports the plugin as inactive.", code("BigBrotherConfig.is_active")),
        format_html("Validate the token (check Celery logs) and rerun the updater until {} flips to True.", code("is_active")),
    )
    register_issue(
        core_issues,
        core_actions,
        periodic_task is None,
        format_html("Celery periodic task {} is missing.", code(task_name)),
        format_html("Create the periodic task in Django admin → Periodic tasks and restart Celery workers."),
    )
    if periodic_task is not None:  # Provide status info only when the scheduled task exists.
        register_issue(
            core_issues,
            core_actions,
            not periodic_task.enabled,
            format_html("Celery periodic task {} exists but is disabled.", code(task_name)),
            format_html("Enable the task in Django admin → Periodic tasks and restart Celery workers."),
        )
        if periodic_task.last_run_at:  # Surface last successful execution timestamp.
            core_info.append(
                format_html(
                    "Last successful update: {} UTC.",
                    timezone.localtime(periodic_task.last_run_at).strftime("%Y-%m-%d %H:%M"),
                )
            )
    modules.append(
        make_module(
            _("BigBrother Core Dashboard"),
            _("Pilot-focused dashboard that streams compliance cards."),
            core_issues,
            core_actions,
            info=core_info,
        )
    )

    # CorpBrother Dashboard
    corp_issues, corp_actions, corp_info = [], [], []

    register_issue(
        corp_issues,
        corp_actions,
        not cfg.is_active,
        format_html("{} must be True for CorpBrother to load.", code("BigBrotherConfig.is_active")),
        format_html(
            "Validate the token (check Celery logs) and rerun the updater until {} flips to True.",
            code("is_active"),
        ),
    )
    register_issue(
        corp_issues,
        corp_actions,
        not corptools_installed,
        format_html("{} app is not installed.", code("corptools")),
        format_html("Install allianceauth-corp-tools and run migrations."),
    )
    register_issue(
        corp_issues,
        corp_actions,
        not charlink_installed,
        format_html("{} app is not installed.", code("charlink")),
        format_html("Install aa-charlink and run migrations."),
    )

    if corptools_installed:
        corp_info.append(format_html("{} detected.", code("corptools")))
    if charlink_installed:
        corp_info.append(format_html("{} detected.", code("charlink")))

    modules.append(
        make_module(
            _("CorpBrother Dashboard"),
            _("Corporation-wide audit dashboard for recruiters and directors."),
            corp_issues,
            corp_actions,
            info=corp_info,
        )
    )

    modules.append(
        make_module(
            _("CorpBrother Dashboard"),
            _("Corporation-wide audit dashboard for recruiters and directors."),
            corp_issues,
            corp_actions,
            info=corp_info,
        )
    )

    # Leave of Absence
    loa_issues, loa_actions, loa_info = [], [], []

    register_issue(
        loa_issues,
        loa_actions,
        not cfg.is_loa_active,
        format_html("{} is disabled.", code("BigBrotherConfig.is_loa_active")),
        format_html("Enable the toggle in BigBrotherConfig and restart AllianceAuth."),
    )
    if not discordbot_installed:
        register_issue(
            loa_issues,
            loa_actions,
            True,
            format_html("{} app is not installed; Discord notifications will fail.", code("aadiscordbot")),
            format_html("Install and configure aadiscordbot for ticket and LoA notifications."),
        )
    if cfg.loawebhook:
        loa_info.append(format_html("LoA webhook configured: {}", cfg.loawebhook))

    modules.append(
        make_module(
            _("Leave of Absence"),
            _("AllianceAuth LoA request pages and Discord alerts."),
            loa_issues,
            loa_actions,
            info=loa_info,
        )
    )

    # PAP Statistics
    pap_issues, pap_actions, pap_info = [], [], []

    register_issue(
        pap_issues,
        pap_actions,
        not cfg.is_paps_active,
        format_html("{} is disabled.", code("BigBrotherConfig.is_paps_active")),
        format_html("Enable PAPs in BigBrotherConfig and restart AllianceAuth."),
    )
    register_issue(
        pap_issues,
        pap_actions,
        not django_apps.is_installed("afat"),
        format_html("{} app is not installed.", code("afat")),
        format_html("Install allianceauth-afat and run migrations."),
    )

    if django_apps.is_installed("afat"):
        pap_info.append(format_html("{} detected.", code("afat")))

    modules.append(
        make_module(
            _("PAP Statistics"),
            _("Participation statistics integration and summaries."),
            pap_issues,
            pap_actions,
            info=pap_info,
        )
    )

    # Cache Warmer
    warmer_issues, warmer_actions = [], []
    register_issue(
        warmer_issues,
        warmer_actions,
        not cfg.is_warmer_active,
        format_html("{} is disabled.", code("BigBrotherConfig.is_warmer_active")),
        format_html("Enable the cache warmer or increase your gunicorn timeout to avoid stream resets."),
    )
    modules.append(
        make_module(
            _("Cache Warmer"),
            _("Background task that preloads contracts, mails and transactions before streaming cards."),
            warmer_issues,
            warmer_actions,
        )
    )

    # Daily notifications
    daily_issues, daily_actions, daily_info = [], [], []

    register_issue(
        daily_issues,
        daily_actions,
        not cfg.are_daily_messages_active,
        format_html("{} is disabled.", code("BigBrotherConfig.are_daily_messages_active")),
        format_html("Enable daily messages in BigBrotherConfig and restart Celery workers."),
    )
    register_issue(
        daily_issues,
        daily_actions,
        not cfg.dailywebhook,
        format_html("{} is empty.", code("BigBrotherConfig.dailywebhook")),
        format_html("Set a Discord webhook URL in {}.", code("dailywebhook")),
    )
    register_issue(
        daily_issues,
        daily_actions,
        cfg.dailyschedule is None,
        format_html("{} is not linked to a schedule.", code("BigBrotherConfig.dailyschedule")),
        format_html("Create a crontab/interval schedule and assign it to {}.", code("dailyschedule")),
    )
    if not discordbot_installed:
        register_issue(
            daily_issues,
            daily_actions,
            True,
            format_html("{} app is not installed; daily Discord posts will fail.", code("aadiscordbot")),
            format_html("Install and configure aadiscordbot."),
        )
    if cfg.dailyschedule:
        daily_info.append(format_html("Schedule: {}", cfg.dailyschedule))

    modules.append(
        make_module(
            _("Daily Notifications"),
            _("Repeatable daily status messages sent to Discord."),
            daily_issues,
            daily_actions,
            info=daily_info,
        )
    )

    # Optional notification streams
    for idx in range(1, 6):
        stream_name = _("Optional Notification Stream %(number)s") % {"number": idx}
        summary = _("Additional Discord webhook stream number %(number)s.") % {"number": idx}
        issues, actions, info = [], [], []

        flag = getattr(cfg, f"are_opt_messages{idx}_active")
        webhook = getattr(cfg, f"optwebhook{idx}")
        schedule = getattr(cfg, f"optschedule{idx}")

        if flag and not webhook:
            register_issue(
                issues,
                actions,
                True,
                format_html("{} is empty.", code(f"optwebhook{idx}")),
                format_html("Set a Discord webhook URL in {}.", code(f"optwebhook{idx}")),
            )
        if flag and schedule is None:
            register_issue(
                issues,
                actions,
                True,
                format_html("{} is not linked to a schedule.", code(f"optschedule{idx}")),
                format_html("Assign a crontab/interval schedule to {}.", code(f"optschedule{idx}")),
            )
        if flag and not discordbot_installed:
            register_issue(
                issues,
                actions,
                True,
                format_html("{} app is not installed; Discord posts will fail.", code("aadiscordbot")),
                format_html("Install and configure aadiscordbot."),
            )
        if schedule:
            info.append(format_html("Schedule: {}", schedule))

        # active only when toggle + webhook + schedule + bot are all good
        active_override = bool(flag and webhook and schedule and discordbot_installed)

        modules.append(
            make_module(
                stream_name,
                summary,
                issues,
                actions,
                info=info,
                active_override=active_override,
            )
        )

    # LoA inactivity alerts (AFK tickets)
    afk_issues, afk_actions, afk_info = [], [], []

    register_issue(
        afk_issues,
        afk_actions,
        not cfg.is_loa_active,
        format_html("{} is disabled.", code("BigBrotherConfig.is_loa_active")),
        format_html("Enable LoA in BigBrotherConfig and restart AllianceAuth."),
    )

    if ticket_cfg is None:
        register_issue(
            afk_issues,
            afk_actions,
            True,
            format_html(
                "TicketToolConfig could not be loaded ({}).",
                ticket_cfg_error or _("database schema mismatch"),
            ),
            format_html("Run {} to apply pending migrations.", format_html("<code>manage.py migrate aa_bb</code>")),
        )
    else:
        register_issue(
            afk_issues,
            afk_actions,
            not ticket_cfg.afk_check_enabled,
            format_html("{} is disabled.", code("TicketToolConfig.afk_check_enabled")),
            format_html("Toggle AFK checks on in TicketToolConfig."),
        )

    modules.append(
        make_module(
            _("LoA inactivity alerts"),
            _("Automatic AFK ticketing that warns when users stop logging in without an LoA."),
            afk_issues,
            afk_actions,
            info=afk_info,
        )
    )

    # Ticket automation (general)
    ticket_issues, ticket_actions, ticket_info = [], [], []

    if ticket_cfg is None:
        register_issue(
            ticket_issues,
            ticket_actions,
            True,
            format_html(
                "TicketToolConfig could not be loaded ({}).",
                ticket_cfg_error or _("database schema mismatch"),
            ),
            format_html("Run {} to apply pending migrations.", format_html("<code>manage.py migrate aa_bb</code>")),
        )
    else:
        register_issue(
            ticket_issues,
            ticket_actions,
            not discordbot_installed,
            format_html("{} app is not installed.", code("aadiscordbot")),
            format_html("Install aadiscordbot and configure the Discord bot token."),
        )
        register_issue(
            ticket_issues,
            ticket_actions,
            ticket_cfg.Category_ID in (None, 0),
            format_html("{} is not set.", code("TicketToolConfig.Category_ID")),
            format_html("Provide the Discord category ID where tickets should be created."),
        )
        if ticket_cfg.staff_roles:
            ticket_info.append(format_html("Staff roles: {}", ticket_cfg.staff_roles))
        else:
            register_issue(
                ticket_issues,
                ticket_actions,
                True,
                format_html(
                    "{} has not been defined; only the bot will see tickets.",
                    code("TicketToolConfig.staff_roles"),
                ),
                format_html("Configure staff roles in TicketToolConfig so humans see tickets."),
            )
        if not charlink_installed:
            register_issue(
                ticket_issues,
                ticket_actions,
                True,
                format_html("{} is not installed; char → user mapping may be limited.", code("charlink")),
                format_html("Install allianceauth-charlink to improve ticket context."),
            )

    modules.append(
        make_module(
            _("Ticket automation"),
            _("Discord-based compliance ticket workflow driven by TicketToolConfig."),
            ticket_issues,
            ticket_actions,
            info=ticket_info,
        )
    )

    # Blacklist integration
    blacklist_issues, blacklist_actions = [], []
    register_issue(
        blacklist_issues,
        blacklist_actions,
        not blacklist_installed,
        format_html("{} app is not installed; Corp Blacklist features will be unavailable.", code("blacklist")),
        format_html("Install allianceauth-blacklist and add it to {}.", code("INSTALLED_APPS")),
    )
    modules.append(
        make_module(
            _("Blacklist integration"),
            _("Allows BigBrother to add characters to AllianceAuth Blacklist directly from the dashboard."),
            blacklist_issues,
            blacklist_actions,
        )
    )

    # Reddit
    reddit_cfg = None
    reddit_cfg_error = None
    reddit_messages_error = None
    reddit_messages_count = 0
    try:
        reddit_cfg = BigBrotherRedditSettings.get_solo()
    except (OperationalError, ProgrammingError) as exc:
        reddit_cfg_error = str(exc)
    else:
        try:
            reddit_messages_count = BigBrotherRedditMessage.objects.count()
        except (OperationalError, ProgrammingError) as exc:
            reddit_messages_error = str(exc)

    return render(request, "faq/modules.html", {"modules": modules})


@login_required
@permission_required("aa_bb.basic_access")
def manual_faq(request: WSGIRequest):
    """Manual tab: FAQ content."""
    return render(request, "faq/faq.html")


@login_required
@permission_required("aa_bb.basic_access")
def reddit_oauth_login(request: WSGIRequest):
    try:
        reddit_cfg = BigBrotherRedditSettings.get_solo()
    except (OperationalError, ProgrammingError) as exc:
        messages.error(request, _("Could not load reddit settings: {}.").format(exc))
        return redirect("aa_bb:manual_modules")

    if not reddit_app_configured(reddit_cfg):
        messages.error(request, _("Reddit OAuth client id/secret must be configured first."))
        return redirect("aa_bb:manual_modules")

    state = secrets.token_urlsafe(32)
    request.session["bb_reddit_oauth_state"] = state

    redirect_uri = reddit_cfg.reddit_redirect_override or request.build_absolute_uri(
        reverse("aa_bb:reddit_oauth_callback")
    )
    scope = reddit_cfg.reddit_scope or "identity submit read"
    auth_url = (
        "https://www.reddit.com/api/v1/authorize"
        f"?client_id={reddit_cfg.reddit_client_id}"
        "&response_type=code"
        f"&redirect_uri={quote_plus(redirect_uri)}"
        "&duration=permanent"
        f"&scope={quote_plus(scope)}"
        f"&state={quote_plus(state)}"
    )
    return redirect(auth_url)


@login_required
@permission_required("aa_bb.basic_access")
def reddit_oauth_callback(request: WSGIRequest):
    expected_state = request.session.get("bb_reddit_oauth_state")
    returned_state = request.GET.get("state")
    if not expected_state or expected_state != returned_state:
        return HttpResponseBadRequest("Invalid OAuth state.")

    error = request.GET.get("error")
    if error:
        messages.error(request, _("Reddit authorization failed: {}.").format(error))
        return redirect("aa_bb:manual_modules")

    code_value = request.GET.get("code")
    if not code_value:
        messages.error(request, _("Reddit did not return an authorization code."))
        return redirect("aa_bb:manual_modules")

    try:
        reddit_cfg = BigBrotherRedditSettings.get_solo()
    except (OperationalError, ProgrammingError) as exc:
        messages.error(request, _("Could not load reddit settings: {}.").format(exc))
        return redirect("aa_bb:manual_modules")

    if not reddit_app_configured(reddit_cfg):
        messages.error(request, _("Reddit OAuth client id/secret must be configured first."))
        return redirect("aa_bb:manual_modules")

    redirect_uri = reddit_cfg.reddit_redirect_override or request.build_absolute_uri(
        reverse("aa_bb:reddit_oauth_callback")
    )

    data = {
        "grant_type": "authorization_code",
        "code": code_value,
        "redirect_uri": redirect_uri,
    }

    headers = {
        "User-Agent": reddit_cfg.reddit_user_agent or "aa-bb-reddit-oauth/1.0",
    }

    try:
        response = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            data=data,
            headers=headers,
            auth=(reddit_cfg.reddit_client_id, reddit_cfg.reddit_client_secret),
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        messages.error(request, _("Reddit token exchange failed: {}.").format(exc))
        return redirect("aa_bb:manual_modules")
    except ValueError:
        messages.error(request, _("Reddit token exchange returned an unexpected payload."))
        return redirect("aa_bb:manual_modules")

    reddit_cfg.reddit_access_token = payload.get("access_token", "")
    refresh_token = payload.get("refresh_token")
    if refresh_token:
        reddit_cfg.reddit_refresh_token = refresh_token
    reddit_cfg.reddit_token_type = payload.get("token_type", "")
    reddit_cfg.reddit_token_obtained = timezone.now()
    reddit_cfg.save()

    if reddit_cfg.reddit_access_token:
        me_headers = {
            "Authorization": f"bearer {reddit_cfg.reddit_access_token}",
            "User-Agent": headers["User-Agent"],
        }
        try:
            me_resp = requests.get(
                "https://oauth.reddit.com/api/v1/me",
                headers=me_headers,
                timeout=15,
            )
            if me_resp.ok:
                reddit_cfg.reddit_account_name = me_resp.json().get("name", "")
                reddit_cfg.save(update_fields=["reddit_account_name"])
        except requests.RequestException:
            pass

    request.session.pop("bb_reddit_oauth_state", None)
    messages.success(request, _("Reddit token stored successfully for the reddit module."))
    return redirect("aa_bb:manual_modules")
