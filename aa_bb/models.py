from django.db import models
from django.core.exceptions import ValidationError
from solo.models import SingletonModel
from django.contrib.auth.models import User
from django.db.models import JSONField
from django_celery_beat.models import CrontabSchedule
from django.utils import timezone

from allianceauth.authentication.models import State
from allianceauth.groupmanagement.models import AuthGroup

import logging

logger = logging.getLogger(__name__)

try:
    from charlink.models import ComplianceFilter
except ImportError:
    logger.warning("charlink not installed")



DEFAULT_CHARACTER_SCOPES = ",".join([
    "publicData",
    "esi-calendar.read_calendar_events.v1",
    "esi-location.read_location.v1",
    "esi-location.read_ship_type.v1",
    "esi-mail.read_mail.v1",
    "esi-skills.read_skills.v1",
    "esi-skills.read_skillqueue.v1",
    "esi-wallet.read_character_wallet.v1",
    "esi-search.search_structures.v1",
    "esi-clones.read_clones.v1",
    "esi-characters.read_contacts.v1",
    "esi-universe.read_structures.v1",
    "esi-killmails.read_killmails.v1",
    "esi-assets.read_assets.v1",
    "esi-fleets.read_fleet.v1",
    "esi-fleets.write_fleet.v1",
    "esi-ui.open_window.v1",
    "esi-ui.write_waypoint.v1",
    "esi-fittings.read_fittings.v1",
    "esi-characters.read_loyalty.v1",
    "esi-characters.read_standings.v1",
    "esi-industry.read_character_jobs.v1",
    "esi-markets.read_character_orders.v1",
    "esi-characters.read_corporation_roles.v1",
    "esi-location.read_online.v1",
    "esi-contracts.read_character_contracts.v1",
    "esi-clones.read_implants.v1",
    "esi-characters.read_fatigue.v1",
    "esi-characters.read_notifications.v1",
    "esi-industry.read_character_mining.v1",
    "esi-characters.read_titles.v1",
])

DEFAULT_CORPORATION_SCOPES = ",".join([
    "esi-corporations.read_corporation_membership.v1",
    "esi-corporations.read_structures.v1",
    "esi-killmails.read_corporation_killmails.v1",
    "esi-corporations.track_members.v1",
    "esi-wallet.read_corporation_wallets.v1",
    "esi-corporations.read_divisions.v1",
    "esi-assets.read_corporation_assets.v1",
    "esi-corporations.read_titles.v1",
    "esi-contracts.read_corporation_contracts.v1",
    "esi-corporations.read_starbases.v1",
    "esi-industry.read_corporation_jobs.v1",
    "esi-markets.read_corporation_orders.v1",
    "esi-industry.read_corporation_mining.v1",
    "esi-planets.read_customs_offices.v1",
    "esi-search.search_structures.v1",
    "esi-universe.read_structures.v1",
    "esi-characters.read_corporation_roles.v1",
])


class General(models.Model):
    """Meta model for app permissions"""

    class Meta:
        """Meta definitions"""

        managed = False
        default_permissions = ()
        permissions = (
            ("basic_access", "Can access Big Brother"),
            ("full_access", "Can view all main characters in Big Brother"),
            ("recruiter_access", "Can view main characters in Guest state only in Big Brother"),
            ("basic_access_cb", "Can access Corp Brother"),
            ("full_access_cb", "Can view all corps in Corp Brother"),
            ("recruiter_access_cb", "Can view guest's corps only in Corp Brother"),
            ("can_blacklist_characters", "Can add characters to blacklist"),
            ("can_access_loa", "Can access and submit a Leave Of Absence request"),
            ("can_view_all_loa", "Can view all Leave Of Absence requests"),
            ("can_manage_loa", "Can manage Leave Of Absence requests"),
            ("can_access_paps", "Can access PAP Stats"),
            ("can_generate_paps", "Can generate PAP Stats"),
            )

class UserStatus(models.Model):
    """
    Cached snapshot of every per-user signal displayed on BigBrother.

    Fields:
    - user: AllianceAuth user whose data is tracked.
    - has_awox_kills / awox_kill_links: whether friendly-fire kills were found and the link payload.
    - has_cyno / cyno: readiness summary for cyno-capable characters.
    - has_skills / skills: results from the skill checklist (SP, ratios, etc.).
    - has_hostile_assets / hostile_assets: systems where the user owns assets in hostile space.
    - has_hostile_clones / hostile_clones: hostile clone locations.
    - has_coalition_blacklist / has_alliance_blacklist: booleans for coalition blacklist hits.
    - has_game_time_notifications / has_skill_injected: notification flags coming from the ESI feed.
    - has_sus_contacts / sus_contacts: contacts that matched corporate/blacklist criteria.
    - has_sus_contracts / sus_contracts: hostile contract summaries.
    - has_sus_mails / sus_mails: hostile mail summaries.
    - has_sus_trans / sus_trans: hostile wallet transactions.
    - sp_age_ratio_result: cached SP-per-day data for the skill card.
    - clone_status: cached alpha/omega detection results.
    - updated: Django-managed timestamp for when this row last changed.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    has_awox_kills = models.BooleanField(default=False)
    awox_kill_links = JSONField(default=dict, blank=True)
    has_cyno = models.BooleanField(default=False)
    cyno = JSONField(default=dict, blank=True)
    has_skills = models.BooleanField(default=False)
    skills = JSONField(default=dict, blank=True)
    has_hostile_assets = models.BooleanField(default=False)
    hostile_assets = JSONField(default=dict, blank=True)
    has_hostile_clones = models.BooleanField(default=False)
    hostile_clones = JSONField(default=dict, blank=True)
    has_coalition_blacklist = models.BooleanField(default=False)
    has_alliance_blacklist = models.BooleanField(default=False)
    has_game_time_notifications = models.BooleanField(default=False)
    has_skill_injected = models.BooleanField(default=False)
    has_sus_contacts = models.BooleanField(default=False)
    sus_contacts = JSONField(default=dict, blank=True)
    has_sus_contracts = models.BooleanField(default=False)
    sus_contracts = JSONField(default=dict, blank=True)
    has_sus_mails = models.BooleanField(default=False)
    sus_mails = JSONField(default=dict, blank=True)
    has_sus_trans = models.BooleanField(default=False)
    sus_trans = JSONField(default=dict, blank=True)
    sp_age_ratio_result = JSONField(default=dict, blank=True)
    clone_status = JSONField(default=dict, blank=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User Status"
        verbose_name_plural = "User Statuses"

class CorpStatus(models.Model):
    """
    CorpBrother equivalent of UserStatus.

    Fields:
    - corp_id / corp_name: EVE corporation identity being summarized.
    - has_hostile_assets / hostile_assets: hostile staging systems for corp assets.
    - has_sus_contracts / sus_contracts: hostile contracts involving the corp.
    - has_sus_trans / sus_trans: suspicious corp wallet transactions.
    - updated: when the cache row last changed.
    """
    corp_id = models.PositiveIntegerField(default=1)
    corp_name = models.TextField(max_length=50)
    has_hostile_assets = models.BooleanField(default=False)
    hostile_assets = JSONField(default=dict, blank=True)
    has_sus_contracts = models.BooleanField(default=False)
    sus_contracts = JSONField(default=dict, blank=True)
    has_sus_trans = models.BooleanField(default=False)
    sus_trans = JSONField(default=dict, blank=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Corp Status"
        verbose_name_plural = "Corp Statuses"

class Messages(models.Model):
    """Pool of daily Discord messages (text plus `sent_in_cycle` flag)."""
    text = models.TextField(max_length=2000)
    sent_in_cycle = models.BooleanField(default=False)
    def __str__(self):
        return self.text
    class Meta:
        verbose_name = "Daily Message"
        verbose_name_plural = "Daily Messages"

class OptMessages1(models.Model):
    """Optional message stream #1 (text plus cycle flag)."""
    text = models.TextField(max_length=2000)
    sent_in_cycle = models.BooleanField(default=False)
    def __str__(self):
        return self.text
    class Meta:
        verbose_name = "Optional Message 1"
        verbose_name_plural = "Optional Messages 1"

class OptMessages2(models.Model):
    """Optional message stream #2."""
    text = models.TextField(max_length=2000)
    sent_in_cycle = models.BooleanField(default=False)
    def __str__(self):
        return self.text
    class Meta:
        verbose_name = "Optional Message 2"
        verbose_name_plural = "Optional Messages 2"

class OptMessages3(models.Model):
    """Optional message stream #3."""
    text = models.TextField(max_length=2000)
    sent_in_cycle = models.BooleanField(default=False)
    def __str__(self):
        return self.text
    class Meta:
        verbose_name = "Optional Message 3"
        verbose_name_plural = "Optional Messages 3"

class OptMessages4(models.Model):
    """Optional message stream #4."""
    text = models.TextField(max_length=2000)
    sent_in_cycle = models.BooleanField(default=False)
    def __str__(self):
        return self.text
    class Meta:
        verbose_name = "Optional Message 4"
        verbose_name_plural = "Optional Messages 4"

class OptMessages5(models.Model):
    """Optional message stream #5."""
    text = models.TextField(max_length=2000)
    sent_in_cycle = models.BooleanField(default=False)
    def __str__(self):
        return self.text
    class Meta:
        verbose_name = "Optional Message 5"
        verbose_name_plural = "Optional Messages 5"


class MessageType(models.Model):
    """Lookup table for the named message categories referenced in hooks/config."""
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name

class PapsConfig(SingletonModel):
    """
    Singleton storing how PAP compliance is calculated.

    Fields:
    - required_paps: baseline PAPs per month for compliance.
    - corp_modifier / alliance_modifier / coalition_modifier: weights for PAPs earned through each source.
    - max_corp_paps: cap on corp PAPs counted after modifiers.
    - group_paps / group_paps_modifier: AA groups that grant bonus PAPs and how many each is worth.
    - excluded_groups / excluded_groups_get_paps: groups that block other awards and whether they still grant a single bonus.
    - excluded_users / excluded_users_paps: user-specific overrides that disable all PAPs or only group-derived ones.
    - capital_groups_get_paps, cap_group/cap_group_paps, super_group/super_group_paps, titan_group/titan_group_paps:
      toggles and per-capital-group bonuses for members flagged as capital, super, or titan pilots.
    """
    required_paps = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs per month should a user get?"
    )

    corp_modifier = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs is a corp PAP worth?"
    )

    max_corp_paps = models.PositiveIntegerField(
        default=1,
        help_text="How many Corp PAPs will count?"
    )

    alliance_modifier = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs is an alliance PAP worth?"
    )

    coalition_modifier = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs is a coalition PAP worth?"
    )

    group_paps = models.ManyToManyField(
        AuthGroup,
        related_name="group_paps",
        blank=True,
        help_text="List of groups which give paps"
    )

    excluded_groups = models.ManyToManyField(
        AuthGroup,
        related_name="excluded_groups",
        blank=True,
        help_text="List of groups which prevent giving paps"
    )

    excluded_groups_get_paps = models.BooleanField(
        default=False,
        editable=True,
        help_text="if user is in a group which prevent other groups from giving paps, do they get 1x group paps modifier?"
    )

    excluded_users = models.ManyToManyField(
        User,
        related_name="excluded_user",
        blank=True,
        help_text="List of user prevented from getting all paps"
    )

    excluded_users_paps = models.ManyToManyField(
        User,
        related_name="excluded_users_paps",
        blank=True,
        help_text="List of user prevented from getting paps from groups"
    )

    group_paps_modifier = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs to add per group"
    )

    capital_groups_get_paps = models.BooleanField(
        default=False,
        editable=True,
        help_text="Does being in corp capital groups give out paps?"
    )

    cap_group = models.ForeignKey(
        AuthGroup,
        related_name="cap_group",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        help_text="Select your cap group"
    )

    cap_group_paps = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs to add for being in the cap group"
    )

    super_group = models.ForeignKey(
        AuthGroup,
        related_name="super_group",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        help_text="Select your super group"
    )

    super_group_paps = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs to add for being in the super group"
    )

    titan_group = models.ForeignKey(
        AuthGroup,
        related_name="titan_group",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        help_text="Select your titan group"
    )

    titan_group_paps = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs to add for being in the titan group"
    )


class BigBrotherConfig(SingletonModel):
    """
    Master configuration for every BigBrother/CorpBrother feature.

    Key field groups:
    - pingroleID / pingroleID2 and pingrole1_messages / pingrole2_messages /
      here_messages / everyone_messages: map message types to Discord roles or the default @here/@everyone.
    - bb_guest_states / bb_member_states: AllianceAuth states that define who is treated as a guest vs. member.
    - hostile_alliances / hostile_corporations and whitelist_* fields: comma-separated IDs that colour cards red or bypass checks.
    - ignored_corporations / member_corporations / member_alliances: corp/alliance overrides for CorpBrother membership.
    - character_scopes / corporation_scopes: comma-separated ESI scopes required for compliance checks.
    - webhook / loawebhook / dailywebhook / optwebhook1-5: Discord destinations for alerts, LoA notices, daily digests, and optional feeds.
    - dailyschedule / optschedule1-5: celery-beat schedules for those webhooks; paired with `optwebhook*`.
    - is_loa_active / is_paps_active / is_warmer_active / are_daily_messages_active / are_opt_messages*_active:
      feature toggles that gate LoA, PAPs, the cache warmer, and message streams.
    - dlc_* booleans and apply_module_status(): track which optional DLC modules (CorpBrother, LoA, PAPs, Tickets, Reddit, Daily Messages) are licensed.
    - main_corporation / main_alliance IDs + names, member thresholds, and handshake booleans (is_active) are populated by the updater.
    - bigbrother_tokens, bb_install_token, update timing fields, and reddit/daily message pointers track upstream licensing and version checks.
    """

    cyno_notify = models.BooleanField(
        default=True,
        help_text="Whether to send Cyno Change notifications to discord"
    )

    sp_inject_notify = models.BooleanField(
        default=True,
        help_text="Whether to send SP Injection notifications to discord"
    )

    clone_notify = models.BooleanField(
        default=True,
        help_text="Whether to send Clone State Change notifications to discord"
    )

    asset_notify = models.BooleanField(
        default=True,
        help_text="Whether to send Asset Change notifications to discord"
    )

    contact_notify = models.BooleanField(
        default=True,
        help_text="Whether to send Contact Change notifications to discord"
    )

    contract_notify = models.BooleanField(
        default=True,
        help_text="Whether to send Contract Change notifications to discord"
    )

    ct_notify = models.BooleanField(
        default=True,
        help_text="Whether to send CT audit completion notifications to discord"
    )

    awox_notify = models.BooleanField(
        default=True,
        help_text="Whether to send AWOX notificaitons to discord"
    )

    mail_notify = models.BooleanField(
        default=True,
        help_text="Whether to send Suspicious Mail notifications to discord"
    )

    transaction_notify = models.BooleanField(
        default=True,
        help_text="Whether to send Suspicious Transaction notifications to discord"
    )

    new_user_notify = models.BooleanField(
        default=False,
        help_text="Whether to send notifications of all previous user history when a user first gets audited, "
                  "this can be VERY spammy on a first time load of the tool"
    )

    ticket_notify_man = models.BooleanField(
        default=True,
        help_text="Whether to send ticket resolution notifications when manually closed to discord"
    )

    ticket_notify_auto = models.BooleanField(
        default=True,
        help_text="Whether to send ticket resolution notifications when automatically closed to discord"
    )

    pingroleID = models.CharField(
        max_length=255,
        null=True,
        blank=False,
        default=0,
        help_text="Input the role ID you want pinged when people need to investigate"
    )

    pingroleID2 = models.CharField(
        max_length=255,
        null=True,
        blank=False,
        default=0,
        help_text="Input the 2nd role ID you want pinged when people need to investigate"
    )

    bb_guest_states = models.ManyToManyField(
        State,
        related_name="bb_guest_states_configs",
        blank=True,
        help_text="List of states to be considered guests"
    )

    bb_member_states = models.ManyToManyField(
        State,
        related_name="bb_member_states_configs",
        blank=True,
        help_text="List of states to be considered members"
    )

    pingrole1_messages = models.ManyToManyField(
        MessageType,
        related_name="pingrole1_configs",
        blank=True,
        help_text="List of message types that should ping the pingrole1"
    )

    pingrole2_messages = models.ManyToManyField(
        MessageType,
        related_name="pingrole2_configs",
        blank=True,
        help_text="List of message types that should ping the pingrole2"
    )

    here_messages = models.ManyToManyField(
        MessageType,
        related_name="here_configs",
        blank=True,
        help_text="List of message types that should ping @here"
    )

    everyone_messages = models.ManyToManyField(
        MessageType,
        related_name="everyone_configs",
        blank=True,
        help_text="List of message types that should ping @everyone"
    )

    hostile_alliances = models.TextField(
        default="",
        blank=True,
        null=True,
        help_text="List of alliance IDs considered hostile, separated by ','"
    )

    hostile_corporations = models.TextField(
        blank=True,
        null=True,
        help_text="List of corporation IDs considered hostile, separated by ','"
    )

    consider_nullsec_hostile = models.BooleanField(
        default=False,
        help_text="Consider all nullsec regions as hostile?"
    )

    consider_all_structures_hostile = models.BooleanField(
        default=False,
        help_text="Consider all player owned structures that are not listed as 'whitelist, ignored or member' as hostile?"
    )

    consider_npc_stations_hostile = models.BooleanField(
        default=False,
        help_text="Consider assets in any non-player owned (NPC) station as hostile?"
    )

    excluded_systems = models.TextField(
        blank=True,
        null=True,
        help_text="List of system IDs excluded from hostile checks, separated by ','"
    )

    excluded_stations = models.TextField(
        blank=True,
        null=True,
        help_text="List of station/structure IDs excluded from hostile checks, separated by ','"
    )

    hostile_assets_ships_only = models.BooleanField(
        default=False,
        help_text="Only consider ship assets when checking and rendering hostile asset locations?"
    )

    whitelist_alliances = models.TextField(
        default="",
        blank=True,
        null=True,
        help_text="List of alliance IDs considered whitelisted, separated by ','"
    )

    whitelist_corporations = models.TextField(
        blank=True,
        null=True,
        help_text="List of corporation IDs considered whitelisted, separated by ','"
    )

    ignored_corporations = models.TextField(
        blank=True,
        null=True,
        help_text="List of corporation IDs to be ignored in the corp brother task and to not show up in Corp Brother tab, separated by ','"
    )

    member_corporations = models.TextField(
        blank=True,
        null=True,
        help_text="List of corporation IDs to be considered members, separated by ','"
    )

    member_alliances = models.TextField(
        blank=True,
        null=True,
        help_text="List of alliance IDs to be considered members, separated by ','"
    )

    character_scopes = models.TextField(
        default=DEFAULT_CHARACTER_SCOPES,
        help_text="Comma-separated list of required character scopes"
    )
    corporation_scopes = models.TextField(
        default=DEFAULT_CORPORATION_SCOPES,
        help_text="Comma-separated list of required corporation scopes"
    )

    webhook = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending BB notifications"
    )

    stats_webhook = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for posting recurring stats."
    )

    loawebhook = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending Leave of Absence"
    )

    dailywebhook = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending daily messages"
    )

    optwebhook1 = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending optional messages 1"
    )

    optwebhook2 = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending optional messages 2"
    )

    optwebhook3 = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending optional messages 3"
    )

    optwebhook4 = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending optional messages 4"
    )

    optwebhook5 = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending optional messages 5"
    )

    stats_schedule = models.ForeignKey(
        CrontabSchedule,
        on_delete=models.CASCADE,
        related_name="bigbrother_stats_schedule",
        null=True,
        blank=True,
        help_text="Schedule for recurring stats posts."
    )

    dailyschedule = models.ForeignKey(
        CrontabSchedule,
        on_delete=models.CASCADE,
        related_name='bigbrother_dailyschedule',
        null=True,
        blank=True,
        help_text="schedule for daily messages"
    )

    optschedule1 = models.ForeignKey(
        CrontabSchedule,
        on_delete=models.CASCADE,
        related_name='bigbrother_optschedule1',
        null=True,
        blank=True,
        help_text="schedule for optional messages 1"
    )

    optschedule2 = models.ForeignKey(
        CrontabSchedule,
        on_delete=models.CASCADE,
        related_name='bigbrother_optschedule2',
        null=True,
        blank=True,
        help_text="schedule for optional messages 2"
    )

    optschedule3 = models.ForeignKey(
        CrontabSchedule,
        on_delete=models.CASCADE,
        related_name='bigbrother_optschedule3',
        null=True,
        blank=True,
        help_text="schedule for optional messages 3"
    )

    optschedule4 = models.ForeignKey(
        CrontabSchedule,
        on_delete=models.CASCADE,
        related_name='bigbrother_optschedule4',
        null=True,
        blank=True,
        help_text="schedule for optional messages 4"
    )

    optschedule5 = models.ForeignKey(
        CrontabSchedule,
        on_delete=models.CASCADE,
        related_name='bigbrother_optschedule5',
        null=True,
        blank=True,
        help_text="schedule for optional messages 5"
    )

    main_corporation_id = models.BigIntegerField(
        default=0,  # Replace with your actual corp ID
        editable=False,
        help_text="Your Corporation Id"
    )

    main_corporation = models.TextField(
        default=0,  # Replace with your actual corp ID
        editable=False,
        help_text="Your Corporation"
    )

    main_alliance_id = models.PositiveIntegerField(
        default=123456789,  # Replace with your actual corp ID
        editable=False,
        help_text="Your alliance ID"
    )

    main_alliance = models.TextField(
        default=123456789,  # Replace with your actual corp ID
        editable=False,
        help_text="Your alliance"
    )

    is_active = models.BooleanField(
        default=False,
        editable=False,
        help_text="has the plugin been activated/deactivated?"
    )

    dlc_corp_brother_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="Read-only flag showing if the Corp Brother module is enabled for this token."
    )

    dlc_loa_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="Read-only flag showing if the Leave of Absence module is enabled for this token."
    )

    dlc_pap_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="Read-only flag showing if the PAP module is enabled for this token."
    )

    dlc_tickets_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="Read-only flag showing if the Tickets module is enabled for this token."
    )

    dlc_reddit_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="Read-only flag showing if the Reddit module is enabled for this token."
    )

    dlc_daily_messages_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="Read-only flag showing if the Daily Messages module is enabled for this token."
    )

    dlc_are_recurring_stats_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="Read-only flag showing if the recurring stats posts activated/deactivated."
    )

    is_loa_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="has the Leave of Absence module been activated/deactivated? (You will need to restart AA for this to take effect)"
    )

    is_paps_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="has the PAP stats module been activated/deactivated? (You will need to restart AA for this to take effect)"
    )

    is_warmer_active = models.BooleanField(
        default=True,
        editable=True,
        help_text="has the Cache warmer feature been activated/deactivated? (You need it if you have a gunicorn timeout set in your supervisor.conf, if you want to disable it, set the timeout to 0 first)"
    )

    loa_max_logoff_days = models.IntegerField(
        default=30,
        help_text="How many days can a user not login w/o a loa request before notifications"
    )

    are_recurring_stats_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="Are recurring stats posts activated/deactivated?"
    )

    are_daily_messages_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="are daily messages activated/deactivated?"
    )

    are_opt_messages1_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="are optional messages 1 activated/deactivated?"
    )

    are_opt_messages2_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="are optional messages 2 activated/deactivated?"
    )

    are_opt_messages3_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="are optional messages 3 activated/deactivated?"
    )

    are_opt_messages4_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="are optional messages 4 activated/deactivated?"
    )

    are_opt_messages5_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="are optional messages 5 activated/deactivated?"
    )

    def __str__(self):
        return "BigBrother Configuration"

    def save(self, *args, **kwargs):
        if not self.pk and BigBrotherConfig.objects.exists():
            raise ValidationError(
                'Only one BigBrotherConfig instance is allowed!'
            )
        #self.pk = self.id = 1  # Enforce singleton
        return super().save(*args, **kwargs)

    DLC_FLAG_MAP = {
        "corp_brother": "dlc_corp_brother_active",
        "loa": "dlc_loa_active",
        "pap": "dlc_pap_active",
        "tickets": "dlc_tickets_active",
        "reddit": "dlc_reddit_active",
        "daily_messages": "dlc_daily_messages_active",
    }

    def apply_module_status(self, modules):
        """Update DLC flags from module data.

        Returns list of field names that changed.
        """

        changed_fields = []
        for module_key, field_name in self.DLC_FLAG_MAP.items():
            new_value = bool(modules.get(module_key, False))
            if getattr(self, field_name) != new_value:
                setattr(self, field_name, new_value)
                changed_fields.append(field_name)
        return changed_fields

class Corporation_names(models.Model):
    """
    Permanent store of corporation names resolved via ESI.
    """
    id = models.BigIntegerField(
        primary_key=True,
        help_text="EVE Corporation ID"
    )
    name = models.CharField(
        max_length=255,
        help_text="Resolved corporation name"
    )
    created = models.DateTimeField(
        auto_now_add=True,
        help_text="When this record was first saved"
    )
    updated = models.DateTimeField(
        auto_now=True,
        help_text="When this record was last refreshed"
    )

    class Meta:
        db_table = 'aa_bb_corporations'
        verbose_name = 'Corporation Name'
        verbose_name_plural = 'Corporation Names'

    def __str__(self):
        return f"{self.id}: {self.name}"

class Alliance_names(models.Model):
    """
    Permanent store of alliance/faction names resolved via ESI.
    """
    id = models.BigIntegerField(
        primary_key=True,
        help_text="EVE Alliance or Faction ID"
    )
    name = models.CharField(
        max_length=255,
        help_text="Resolved alliance/faction name"
    )
    created = models.DateTimeField(
        auto_now_add=True,
        help_text="When this record was first saved"
    )
    updated = models.DateTimeField(
        auto_now=True,
        help_text="When this record was last refreshed"
    )

    class Meta:
        db_table = 'aa_bb_alliances'
        verbose_name = 'Alliance Name'
        verbose_name_plural = 'Alliance Names'

    def __str__(self):
        return f"{self.id}: {self.name}"

class Character_names(models.Model):
    """
    Permanent store of Character names resolved via ESI.
    """
    id = models.BigIntegerField(
        primary_key=True,
        help_text="EVE Character ID"
    )
    name = models.CharField(
        max_length=255,
        help_text="Resolved Character name"
    )
    created = models.DateTimeField(
        auto_now_add=True,
        help_text="When this record was first saved"
    )
    updated = models.DateTimeField(
        auto_now=True,
        help_text="When this record was last refreshed"
    )

    class Meta:
        db_table = 'aa_bb_characters'
        verbose_name = 'Character Name'
        verbose_name_plural = 'Character Names'

    def __str__(self):
        return f"{self.id}: {self.name}"


class id_types(models.Model):
    """
    Permanent store of Character names resolved via ESI.
    """
    id = models.BigIntegerField(
        primary_key=True,
        help_text="EVE ID"
    )
    name = models.CharField(
        max_length=255,
        help_text="Resolved ID Type"
    )
    created = models.DateTimeField(
        auto_now_add=True,
        help_text="When this record was first saved"
    )
    updated = models.DateTimeField(
        auto_now=True,
        help_text="When this record was last refreshed"
    )
    last_accessed = models.DateTimeField(
        default=timezone.now,
        help_text="When this record was last looked up"
    )

    class Meta:
        db_table = 'aa_bb_ids'
        verbose_name = 'ID Type'
        verbose_name_plural = 'ID Types'

    def __str__(self):
        return f"{self.id}: {self.name}"


class ProcessedMail(models.Model):
    """
    Tracks MailMessage IDs that already have generated notes.
    """
    mail_id = models.BigIntegerField(
        primary_key=True,
        help_text="The MailMessage.id_key that has been processed"
    )
    processed_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When this mail was first processed"
    )

    class Meta:
        db_table = "aa_bb_processed_mails"
        verbose_name = "Processed Mail"
        verbose_name_plural = "Processed Mails"

    def __str__(self):
        return f"ProcessedMail {self.mail_id} @ {self.processed_at}"


class SusMailNote(models.Model):
    """
    Stores the summary line (flags) generated for each hostile mail.
    """
    mail = models.OneToOneField(
        ProcessedMail,
        on_delete=models.CASCADE,
        help_text="The mail this note refers to"
    )
    user_id = models.BigIntegerField(
        help_text="The AllianceAuth user ID who owns these characters"
    )
    note = models.TextField(
        help_text="The summary string of flags for this mail"
    )
    created = models.DateTimeField(
        auto_now_add=True,
        help_text="When this note was created"
    )
    updated = models.DateTimeField(
        auto_now=True,
        help_text="When this note was last updated"
    )

    class Meta:
        db_table = "aa_bb_sus_mail_notes"
        verbose_name = "Suspicious Mail Note"
        verbose_name_plural = "Suspicious Mail Notes"

    def __str__(self):
        return f"Mail {self.mail.mail_id} note for user {self.user_id}"


class ProcessedContract(models.Model):
    """
    Tracks Contract IDs that already have generated notes.
    """
    contract_id = models.BigIntegerField(
        primary_key=True,
        help_text="The Contract.contract_id that has been processed"
    )
    processed_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When this contract was first processed"
    )

    class Meta:
        db_table = "aa_bb_processed_contracts"
        verbose_name = "Processed Contract"
        verbose_name_plural = "Processed Contracts"

    def __str__(self):
        return f"ProcessedContract {self.contract_id} @ {self.processed_at}"


class SusContractNote(models.Model):
    """
    Stores the summary line (flags) generated for each hostile contract.
    """
    contract = models.OneToOneField(
        ProcessedContract,
        on_delete=models.CASCADE,
        help_text="The contract this note refers to"
    )
    user_id = models.BigIntegerField(
        help_text="The AllianceAuth user ID who owns these characters"
    )
    note = models.TextField(
        help_text="The summary string of flags for this contract"
    )
    created = models.DateTimeField(
        auto_now_add=True,
        help_text="When this note was created"
    )
    updated = models.DateTimeField(
        auto_now=True,
        help_text="When this note was last updated"
    )

    class Meta:
        db_table = "aa_bb_sus_contract_notes"
        verbose_name = "Suspicious Contract Note"
        verbose_name_plural = "Suspicious Contract Notes"

    def __str__(self):
        return f"Contract {self.contract.contract_id} note for user {self.user_id}"


    from django.db import models

class ProcessedTransaction(models.Model):
    """
    Tracks WalletJournalEntry IDs that already have generated notes.
    """
    entry_id = models.BigIntegerField(
        primary_key=True,
        help_text="The WalletJournalEntry.entry_id that has been processed"
    )
    processed_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When this transaction was first processed"
    )

    class Meta:
        db_table = "aa_bb_processed_transactions"
        verbose_name = "Processed Transaction"
        verbose_name_plural = "Processed Transactions"

    def __str__(self):
        return f"ProcessedTransaction {self.entry_id} @ {self.processed_at}"


class SusTransactionNote(models.Model):
    """
    Stores the summary line (flags) generated for each hostile transaction.
    """
    transaction = models.OneToOneField(
        ProcessedTransaction,
        on_delete=models.CASCADE,
        help_text="The transaction this note refers to"
    )
    user_id = models.BigIntegerField(
        help_text="The AllianceAuth user ID who owns these characters"
    )
    note = models.TextField(
        help_text="The summary string of flags for this transaction"
    )
    created = models.DateTimeField(
        auto_now_add=True,
        help_text="When this note was created"
    )
    updated = models.DateTimeField(
        auto_now=True,
        help_text="When this note was last updated"
    )

    class Meta:
        db_table = "aa_bb_sus_transaction_notes"
        verbose_name = "Suspicious Transaction Note"
        verbose_name_plural = "Suspicious Transaction Notes"

    def __str__(self):
        return f"Transaction {self.transaction.entry_id} note for user {self.user_id}"


class WarmProgress(models.Model):
    """Tracks cache warmer progress per user (current vs total cards)."""
    user_main = models.CharField(max_length=100, unique=True)
    current   = models.PositiveIntegerField()
    total     = models.PositiveIntegerField()
    updated   = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Warm Preload Progress"
        verbose_name_plural = "Warm Preload Progress"

    def __str__(self):
        return f"{self.user_main}: {self.current}/{self.total}"


class EntityInfoCache(models.Model):
    """Cache of resolved entity info (name + corp/alliance pointers) per timestamp."""
    entity_id  = models.IntegerField()
    as_of      = models.DateTimeField()
    data       = JSONField()
    updated    = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("entity_id", "as_of")
        indexes = [
            models.Index(fields=["entity_id", "as_of"]),
            models.Index(fields=["updated"]),
        ]

class RecurringStatsConfig(SingletonModel):
    """
    Configuration for recurring stats posts.

    - Controls which states are counted
    - Which stats are included
    - Holds the previous snapshot so we can calculate deltas
    """

    enabled = models.BooleanField(
        default=True,
        help_text="Master toggle for recurring stats generation."
    )

    states = models.ManyToManyField(
        State,
        blank=True,
        help_text="States to break out in the recurring stats (e.g. Member, Blue, Alumni)."
    )

    # Toggles for which blocks are included
    include_auth_users = models.BooleanField(
        default=True,
        help_text="Include total users in auth and per-state breakdown."
    )
    include_discord_users = models.BooleanField(
        default=True,
        help_text="Include Discord users totals and per-state breakdown (if Discord service is installed)."
    )
    include_mumble_users = models.BooleanField(
        default=True,
        help_text="Include Mumble users totals and per-state breakdown (if Mumble service is installed)."
    )

    include_characters = models.BooleanField(
        default=True,
        help_text="Include total number of known characters."
    )
    include_corporations = models.BooleanField(
        default=True,
        help_text="Include total number of known corporations."
    )
    include_alliances = models.BooleanField(
        default=True,
        help_text="Include total number of known alliances."
    )

    include_tokens = models.BooleanField(
        default=True,
        help_text="Include total number of ESI tokens."
    )
    include_unique_tokens = models.BooleanField(
        default=True,
        help_text="Include number of unique token owners."
    )

    include_character_audits = models.BooleanField(
        default=True,
        help_text="Include total number of Character Audits (from corptools)."
    )
    include_corporation_audits = models.BooleanField(
        default=True,
        help_text="Include total number of Corporation Audits (from corptools)."
    )

    # Snapshot + timestamp for delta calculations
    last_run_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="When recurring stats were last posted."
    )
    last_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text="Previous stats snapshot for delta calculations."
    )

    def __str__(self) -> str:
        return "Recurring Stats Configuration"

    class Meta:
        verbose_name = "Recurring Stats Configuration"
