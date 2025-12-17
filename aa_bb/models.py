from datetime import timedelta

from django.db import models
from django.core.exceptions import ValidationError
from solo.models import SingletonModel
from django.contrib.auth.models import User
from django.db.models import JSONField
from django_celery_beat.models import CrontabSchedule
from django.utils import timezone

from allianceauth.authentication.models import State, UserProfile
from allianceauth.groupmanagement.models import AuthGroup
from allianceauth.eveonline.models import EveAllianceInfo

import logging

logger = logging.getLogger(__name__)

try:
    from charlink.models import ComplianceFilter
except ImportError:
    logger.warning("charlink not installed")

from django.apps import apps
AA_CONTACTS_INSTALLED = apps.is_installed("aa_contacts")


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

class NeutralHandling(models.TextChoices):
    HOSTILE = "hostile", "Handle neutrals as hostile"
    WHITELIST = "whitelist", "Handle neutrals as whitelisted"
    IGNORE = "ignore", "Do nothing with neutrals"

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
        help_text="How many PAPs/AFAT per month should a user get?",
        verbose_name="Required PAPs/AFAT per month"
    )

    corp_modifier = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs/AFAT is a corp PAP worth?",
        verbose_name="Corp PAPs/AFAT Modifier"
    )

    max_corp_paps = models.PositiveIntegerField(
        default=1,
        help_text="How many Corp PAPs/AFAT will count?",
        verbose_name="Corp PAPs/AFAT Maximum"
    )

    alliance_modifier = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs/AFAT is an alliance PAP worth?",
        verbose_name="Alliance PAPs/AFAT Modifier"
    )

    coalition_modifier = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs/AFAT is a coalition PAP worth?",
        verbose_name="Coalition PAPs/AFAT Modifier"
    )

    group_paps = models.ManyToManyField(
        AuthGroup,
        related_name="group_paps",
        blank=True,
        help_text="List of groups which give PAPs/AFAT",
        verbose_name="Group that get PAPs/AFAT"
    )

    excluded_groups = models.ManyToManyField(
        AuthGroup,
        related_name="excluded_groups",
        blank=True,
        help_text="List of groups which prevent giving PAPs/AFAT",
        verbose_name="Excluded Groups"
    )

    excluded_groups_get_paps = models.BooleanField(
        default=False,
        editable=True,
        help_text="if user is in a group which prevent other groups from giving PAPs/AFAT, do they get 1x group PAPs/AFAT modifier?",
        verbose_name="Excluded Groups Modifier"
    )

    excluded_users = models.ManyToManyField(
        User,
        related_name="excluded_user",
        blank=True,
        help_text="List of user prevented from getting all PAPs/AFAT",
        verbose_name="Excluded Users"
    )

    excluded_users_paps = models.ManyToManyField(
        User,
        related_name="excluded_users_paps",
        blank=True,
        help_text="List of user prevented from getting PAPs/AFAT from groups",
        verbose_name="Users who don't get PAPs/AFAT from groups"
    )

    group_paps_modifier = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs/AFAT to add per group",
        verbose_name="Group PAPs/AFAT Modifier"
    )

    capital_groups_get_paps = models.BooleanField(
        default=False,
        editable=True,
        help_text="Does being in corp capital groups give out PAPs/AFAT?",
        verbose_name="Cap Group PAPs/AFAT Enabled?"
    )

    cap_group = models.ForeignKey(
        AuthGroup,
        related_name="cap_group",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        help_text="Select your cap group",
        verbose_name="Cap Group"
    )

    cap_group_paps = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs/AFAT to add for being in the cap group",
        verbose_name="Cap Group PAPs/AFAT Configuration"
    )

    super_group = models.ForeignKey(
        AuthGroup,
        related_name="super_group",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        help_text="Select your super group",
        verbose_name="Super Group"
    )

    super_group_paps = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs/AFAT to add for being in the super group",
        verbose_name="Super Group PAPs/AFAT Configuration"
    )

    titan_group = models.ForeignKey(
        AuthGroup,
        related_name="titan_group",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        help_text="Select your titan group",
        verbose_name="Titan Group"
    )

    titan_group_paps = models.PositiveIntegerField(
        default=1,
        help_text="How many PAPs/AFAT to add for being in the titan group",
        verbose_name="Titan Group PAPs/AFAT Configuration"
    )

    class Meta:
        verbose_name = "PAPs/AFAT Configuration"
        verbose_name_plural = "PAPs/AFAT Configuration"


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
    - main_corporation / main_alliance IDs + names, member thresholds, and handshake booleans (is_active) are populated by the updater.
    - bigbrother_tokens, bb_install_token, update timing fields, and reddit/daily message pointers track upstream licensing and version checks.
    """

    cyno_notify = models.BooleanField(
        default=True,
        help_text="Whether to send Cyno Change notifications to discord",
        verbose_name="Cyno ship and skill Discord Notifications"
    )

    sp_inject_notify = models.BooleanField(
        default=True,
        help_text="Whether to send SP Injection notifications to discord",
        verbose_name="Skill Point Injection Discord Notifications"
    )

    clone_notify = models.BooleanField(
        default=True,
        help_text="Whether to send Clone State Change notifications to discord",
        verbose_name="Hostile Jump Clone Location Change Discord Notifications"
    )

    asset_notify = models.BooleanField(
        default=True,
        help_text="Whether to send Asset Change notifications to discord",
        verbose_name="Hostile Asset Location Change Discord Notifications"
    )

    contact_notify = models.BooleanField(
        default=True,
        help_text="Whether to send Contact Change notifications to discord",
        verbose_name="Hostile Contact Change Discord Notifications"
    )

    contract_notify = models.BooleanField(
        default=True,
        help_text="Whether to send Contract Change notifications to discord",
        verbose_name="Hostile Contract Discord Notifications"
    )

    ct_notify = models.BooleanField(
        default=True,
        help_text="Whether to send CT audit completion notifications to discord",
        verbose_name="CorpTool Audit Completion Discord Notifications"
    )

    awox_notify = models.BooleanField(
        default=True,
        help_text="Whether to send AWOX notificaitons to discord",
        verbose_name="AWOX Discord Notifications"
    )

    mail_notify = models.BooleanField(
        default=True,
        help_text="Whether to send Suspicious Mail notifications to discord",
        verbose_name="Suspicious Mail Notifications"
    )

    transaction_notify = models.BooleanField(
        default=True,
        help_text="Whether to send Suspicious Transaction notifications to discord",
        verbose_name="Suspicious Transaction Notifications"
    )

    new_user_notify = models.BooleanField(
        default=False,
        help_text="Whether to send notifications of all previous user history when a user first gets audited, "
                  "this can be VERY spammy on a first time load of the tool",
        verbose_name="New User Notifications"
    )

    ticket_notify_man = models.BooleanField(
        default=True,
        help_text="Whether to send ticket resolution notifications when manually closed to discord",
        verbose_name="Ticket Closed Manually Discord Notification"
    )

    ticket_notify_auto = models.BooleanField(
        default=True,
        help_text="Whether to send ticket resolution notifications when automatically closed to discord",
        verbose_name="Ticket Closed Automatically Discord Notification"
    )

    pingroleID = models.CharField(
        max_length=255,
        null=True,
        blank=False,
        default=0,
        help_text="Input the role ID you want pinged when people need to investigate",
        verbose_name="Pinged Role ID #1"
    )

    pingroleID2 = models.CharField(
        max_length=255,
        null=True,
        blank=False,
        default=0,
        help_text="Input the 2nd role ID you want pinged when people need to investigate",
        verbose_name="Pinged Role ID #2"
    )

    bb_guest_states = models.ManyToManyField(
        State,
        related_name="bb_guest_states_configs",
        blank=True,
        help_text="List of states to be considered guests",
        verbose_name="Guest States"
    )

    bb_member_states = models.ManyToManyField(
        State,
        related_name="bb_member_states_configs",
        blank=True,
        help_text="List of states to be considered members",
        verbose_name="Member States"
    )

    pingrole1_messages = models.ManyToManyField(
        MessageType,
        related_name="pingrole1_configs",
        blank=True,
        help_text="List of message types that should ping the pingrole1",
        verbose_name="Pingrole1 Alert Conditions"
    )

    pingrole2_messages = models.ManyToManyField(
        MessageType,
        related_name="pingrole2_configs",
        blank=True,
        help_text="List of message types that should ping the pingrole2",
        verbose_name="Pingrole2 Alert Conditions"
    )

    here_messages = models.ManyToManyField(
        MessageType,
        related_name="here_configs",
        blank=True,
        help_text="List of message types that should ping @here",
        verbose_name="@here Alert Conditions"
    )

    everyone_messages = models.ManyToManyField(
        MessageType,
        related_name="everyone_configs",
        blank=True,
        help_text="List of message types that should ping @everyone",
        verbose_name="@everyone Alert Conditions"
    )

    auto_import_contacts_enabled = models.BooleanField(
        default=False,
        help_text=(
            "If enabled, BigBrother will periodically read aa-contacts standings "
            "and merge them into member/hostile corp/alliance lists."
        ),
        verbose_name="Auto-import standings from aa-contacts"
    )

    contacts_import_cache = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Internal cache of IDs imported from aa-contacts so that "
            "removed contacts can be pruned without touching manually "
            "added hostiles / members / whitelists."
        ),
    )


    contacts_source_alliances = models.ManyToManyField(
        EveAllianceInfo,
        related_name="bb_contacts_source_configs",
        blank=True,
        help_text=(
            "Alliances whose aa-contacts standings should be imported. "
            "Alliances with positive standing become members; "
            "negative standing become hostile; zero is ignored."
        ),
        verbose_name="aa-contacts source alliance"
    )

    contacts_handle_neutrals = models.CharField(
        max_length=16,
        choices=NeutralHandling.choices,
        default=NeutralHandling.IGNORE,
        help_text=(
            "Controls how BigBrother treats aa-contacts with neutral (0) standing. "
            "'Hostile' = treat neutrals as hostile, "
            "'Whitelist' = treat neutrals as whitelisted/ignored, "
            "'Ignore' = leave neutrals unchanged."
        )
    )

    hostile_alliances = models.TextField(
        default="",
        blank=True,
        null=True,
        help_text="List of alliance IDs considered hostile, separated by ','",
        verbose_name="Hostile Alliances"
    )

    hostile_corporations = models.TextField(
        blank=True,
        null=True,
        help_text="List of corporation IDs considered hostile, separated by ','",
        verbose_name="Hostile Corporations"
    )

    consider_nullsec_hostile = models.BooleanField(
        default=False,
        help_text="Consider all nullsec regions as hostile?",
        verbose_name="Consider Nullsec as Hostile"
    )

    consider_all_structures_hostile = models.BooleanField(
        default=False,
        help_text="Consider all player owned structures that are not listed as 'whitelist, ignored or member' as hostile?",
        verbose_name="Consider Citadels as Hostile"
    )

    consider_npc_stations_hostile = models.BooleanField(
        default=False,
        help_text="Consider assets in any non-player owned (NPC) station as hostile?",
        verbose_name="Consider NPC Stations as Hostile"
    )

    excluded_systems = models.TextField(
        blank=True,
        null=True,
        help_text="List of system IDs excluded from hostile checks, separated by ','",
        verbose_name="Excluded Systems"
    )

    excluded_stations = models.TextField(
        blank=True,
        null=True,
        help_text="List of station/structure IDs excluded from hostile checks, separated by ','",
        verbose_name="Excluded Stations"
    )

    hostile_assets_ships_only = models.BooleanField(
        default=False,
        help_text="Only consider ship assets when checking and rendering hostile asset locations?",
        verbose_name="Only Consider Ships as Hostile Assets"
    )

    whitelist_alliances = models.TextField(
        default="",
        blank=True,
        null=True,
        help_text="List of alliance IDs considered whitelisted, separated by ','",
        verbose_name="Whitelisted Alliances"
    )

    whitelist_corporations = models.TextField(
        blank=True,
        null=True,
        help_text="List of corporation IDs considered whitelisted, separated by ','",
        verbose_name="Whitelisted Corporations"
    )

    ignored_corporations = models.TextField(
        blank=True,
        null=True,
        help_text="List of corporation IDs to be ignored in the corp brother task and to not show up in Corp Brother tab, separated by ','",
        verbose_name="Ignored Corporations"
    )

    member_corporations = models.TextField(
        blank=True,
        null=True,
        help_text="List of corporation IDs to be considered members, separated by ','",
        verbose_name="Member Corporations"
    )

    member_alliances = models.TextField(
        blank=True,
        null=True,
        help_text="List of alliance IDs to be considered members, separated by ','",
        verbose_name="Member Alliances"
    )

    character_scopes = models.TextField(
        default=DEFAULT_CHARACTER_SCOPES,
        help_text="Comma-separated list of required character scopes",
        verbose_name="Character Scopes"
    )
    corporation_scopes = models.TextField(
        default=DEFAULT_CORPORATION_SCOPES,
        help_text="Comma-separated list of required corporation scopes",
        verbose_name="Corporation Scopes"
    )

    webhook = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending BB notifications",
        verbose_name="Main Discord Webhook"
    )

    stats_webhook = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for posting recurring stats.",
        verbose_name="Recurring Stats Discord Webhook"
    )

    loawebhook = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending Leave of Absence",
        verbose_name="Leave of Absence Discord WebHhok"
    )

    dailywebhook = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending daily messages",
        verbose_name="Daily Message Discord Webhook"
    )

    optwebhook1 = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending optional messages 1",
        verbose_name="Optional Messages #1 Discord Webhook"
    )

    optwebhook2 = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending optional messages 2",
        verbose_name="Optional Messages #2 Discord Webhook"
    )

    optwebhook3 = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending optional messages 3",
        verbose_name="Optional Messages #3 Discord Webhook"
    )

    optwebhook4 = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending optional messages 4",
        verbose_name="Optional Messages #4 Discord Webhook"
    )

    optwebhook5 = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook for sending optional messages 5",
        verbose_name="Optional Messages #5 Discord Webhook"
    )

    stats_schedule = models.ForeignKey(
        CrontabSchedule,
        on_delete=models.CASCADE,
        related_name="bigbrother_stats_schedule",
        null=True,
        blank=True,
        help_text="Schedule for recurring stats posts.",
        verbose_name="Recurring Stats Schedule"
    )

    dailyschedule = models.ForeignKey(
        CrontabSchedule,
        on_delete=models.CASCADE,
        related_name='bigbrother_dailyschedule',
        null=True,
        blank=True,
        help_text="schedule for daily messages",
        verbose_name="Daily Message Schedule"
    )

    optschedule1 = models.ForeignKey(
        CrontabSchedule,
        on_delete=models.CASCADE,
        related_name='bigbrother_optschedule1',
        null=True,
        blank=True,
        help_text="schedule for optional messages 1",
        verbose_name="Optional Messages #1 Schedule"
    )

    optschedule2 = models.ForeignKey(
        CrontabSchedule,
        on_delete=models.CASCADE,
        related_name='bigbrother_optschedule2',
        null=True,
        blank=True,
        help_text="schedule for optional messages 2",
        verbose_name="Optional Messages #2 Schedule"
    )

    optschedule3 = models.ForeignKey(
        CrontabSchedule,
        on_delete=models.CASCADE,
        related_name='bigbrother_optschedule3',
        null=True,
        blank=True,
        help_text="schedule for optional messages 3",
        verbose_name="Optional Messages #3 Schedule"
    )

    optschedule4 = models.ForeignKey(
        CrontabSchedule,
        on_delete=models.CASCADE,
        related_name='bigbrother_optschedule4',
        null=True,
        blank=True,
        help_text="schedule for optional messages 4",
        verbose_name="Optional Messages #4 Schedule"
    )

    optschedule5 = models.ForeignKey(
        CrontabSchedule,
        on_delete=models.CASCADE,
        related_name='bigbrother_optschedule5',
        null=True,
        blank=True,
        help_text="schedule for optional messages 5",
        verbose_name="Optional Messages #5 Schedule"
    )

    main_corporation_id = models.BigIntegerField(
        default=0,
        editable=False,
        help_text="Your Corporation Id",
        verbose_name="Main Corporation ID"
    )

    main_corporation = models.TextField(
        default=0,
        editable=False,
        help_text="Your Corporation",
        verbose_name="Main Corporation"
    )

    main_alliance_id = models.PositiveIntegerField(
        default=123456789,
        editable=False,
        help_text="Your Alliance ID",
        verbose_name="Main Alliance ID"
    )

    main_alliance = models.TextField(
        default=123456789,
        editable=False,
        help_text="Your Alliance",
        verbose_name="Main Alliance"
    )

    is_active = models.BooleanField(
        default=False,
        editable=False,
        help_text="has the plugin been activated/deactivated?",
        verbose_name="Active?"
    )

    is_loa_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="has the Leave of Absence module been activated/deactivated? (You will need to restart AA for this to take effect)",
        verbose_name="Leave of Absence Active?"
    )

    is_paps_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="has the PAP/AFAT stats module been activated/deactivated? (You will need to restart AA for this to take effect)",
        verbose_name="PAP/AFAT Stats Active?"
    )

    is_warmer_active = models.BooleanField(
        default=True,
        editable=True,
        help_text="has the Cache warmer feature been activated/deactivated? (You need it if you have a gunicorn timeout set in your supervisor.conf, if you want to disable it, set the timeout to 0 first)",
        verbose_name="Cache Warmer Active?"
    )

    loa_max_logoff_days = models.IntegerField(
        default=30,
        help_text="How many days can a user not login w/o a loa request before notifications",
        verbose_name="Max Days before needing LOA"
    )

    are_recurring_stats_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="Are recurring stats posts activated/deactivated?",
        verbose_name="Recurring Stats Active?"
    )

    are_daily_messages_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="are daily messages activated/deactivated?",
        verbose_name="Daily Messages Active?"
    )

    are_opt_messages1_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="are optional messages 1 activated/deactivated?",
        verbose_name="Optional Messages 1 Activated?"
    )

    are_opt_messages2_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="are optional messages 2 activated/deactivated?",
        verbose_name="Optional Messages 2 Activated?"
    )

    are_opt_messages3_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="are optional messages 3 activated/deactivated?",
        verbose_name="Optional Messages 3 Activated?"
    )

    are_opt_messages4_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="are optional messages 4 activated/deactivated?",
        verbose_name="Optional Messages 4 Activated?"
    )

    are_opt_messages5_active = models.BooleanField(
        default=False,
        editable=True,
        help_text="are optional messages 5 activated/deactivated?",
        verbose_name="Optional Messages 5 Activated?"
    )

    def __str__(self):
        return "BigBrother Configuration"

    def save(self, *args, **kwargs):
        if not self.pk and BigBrotherConfig.objects.exists():
            raise ValidationError(
                'Only one BigBrotherConfig instance is allowed!'
            )
        return super().save(*args, **kwargs)

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

class Meta:
    verbose_name = "Big Brother Configuration"
    verbose_name_plural = "Big Brother Configuration"

class BigBrotherRedditSettings(SingletonModel):
    """
    Stores OAuth credentials, scheduling, and webhook templates for the Reddit
    recruitment publisher. The numerous fields map directly to the admin UI
    (client id/secret, scopes, target subreddit, cadence, Discord notifications,
    and the stored tokens/permalinks fetched during runtime).
    """
    enabled = models.BooleanField(
        default=False,
        help_text="Toggle after configuration to allow reddit automation to run."
    )
    reddit_client_id = models.CharField(
        max_length=128,
        blank=True,
        help_text="Application client ID from reddit."
    )
    reddit_client_secret = models.CharField(
        max_length=255,
        blank=True,
        help_text="Application secret from reddit."
    )
    reddit_user_agent = models.CharField(
        max_length=255,
        blank=True,
        default="aa-bb-scheduler/1.0",
        help_text="Custom user agent that will appear in reddit API calls."
    )
    reddit_scope = models.CharField(
        max_length=255,
        default="identity submit read",
        help_text="Space separated scopes requested during Reddit OAuth."
    )
    reddit_redirect_override = models.URLField(
        blank=True,
        null=True,
        help_text="Optional custom redirect URI if auto-detected URI is unsuitable."
    )
    reddit_subreddit = models.CharField(
        max_length=64,
        default="evejobs",
        help_text="Name of the subreddit to post to."
    )
    post_interval_days = models.PositiveIntegerField(
        default=8,
        help_text="Minimum delay in days between reddit posts."
    )
    reddit_webhook = models.URLField(
        blank=True,
        null=True,
        help_text="Discord webhook where post confirmations should be sent."
    )
    reddit_webhook_message = models.TextField(
        blank=True,
        default="New reddit post published: {title}\n{url}",
        help_text="Template for Discord notifications. Supports {title}, {url}, {subreddit}."
    )
    reply_message_template = models.TextField(
        blank=True,
        default="New reply by {author}: {url}",
        help_text="Template used when alerting about new replies on reddit."
    )
    reddit_access_token = models.TextField(
        blank=True,
        editable=False,
        help_text="Stored Reddit OAuth access token (hidden in admin)."
    )
    reddit_refresh_token = models.TextField(
        blank=True,
        editable=False,
        help_text="Stored Reddit OAuth refresh token (hidden in admin)."
    )
    reddit_token_type = models.CharField(
        max_length=32,
        blank=True,
        editable=False,
        help_text="Token type returned by Reddit OAuth (hidden in admin)."
    )
    reddit_token_obtained = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text="Timestamp when the Reddit token was stored."
    )
    reddit_account_name = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        help_text="Reddit account authorized via OAuth (hidden in admin)."
    )
    last_submission_id = models.CharField(
        max_length=32,
        blank=True,
        editable=False,
        help_text="Most recent reddit submission id (hidden in admin)."
    )
    last_submission_permalink = models.URLField(
        blank=True,
        null=True,
        editable=False,
        help_text="Link to the most recent reddit submission (hidden in admin)."
    )
    last_submission_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text="When the most recent post was created."
    )
    last_reply_checked_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text="When reddit replies were last scanned."
    )

    class Meta:
        verbose_name = "BigBrother Reddit Settings"

    def __str__(self):
        return "BigBrother Reddit Settings"


class BigBrotherRedditMessage(models.Model):
    """Queue of Markdown job ads reused by the Reddit autoposter."""
    used_in_cycle = models.BooleanField(
        default=False,
        help_text="Automatically toggled once the message is used in a cycle."
    )
    title = models.CharField(
        max_length=300,
        help_text="Title that will be used for the reddit submission."
    )
    content = models.TextField(
        help_text="Markdown body shared to reddit."
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = "Reddit Recruitment Message"
        verbose_name_plural = "Reddit Recruitment Messages"

    def __str__(self):
        status = "used" if self.used_in_cycle else "pending"
        return f"{self.title} ({status})"


class PapCompliance(models.Model):
    """Per-user PAP compliance score (cached for dashboards and tickets)."""
    user_profile = models.ForeignKey(
        UserProfile,
        on_delete=models.CASCADE,
        related_name="pap_compliances",
        help_text="The UserProfile this PAP compliance record belongs to",
    )
    pap_compliant = models.IntegerField(
        default=0,
        help_text="Integer flag or score indicating PAP compliance status"
    )
    class Meta:
        verbose_name = "PAP Compliance Score"
        verbose_name_plural = "PAP Compliance Scores"


class TicketToolConfig(SingletonModel):
    """
    Configuration that drives the Discord compliance ticket automation.

    Major sections:
    - compliance_filter: optional charlink filter to scope the population.
    - ticket_counter: sequential number used for naming ticket channels.
    - *_check_enabled, *_check, *_check_frequency, *_reason, *_reminder:
      toggles and thresholds for the corp token compliance, PAP compliance,
      AFK monitoring, and Discord link checks.
    - Max_Afk_Days / afk_check: trailing max and post-ticket grace period.
    - discord_check fields: mirror the AFK logic but for Discord link status.
    - Category_ID / staff_roles / Role_ID: Discord metadata controlling which
      category hosts the ticket, which roles gain access, and which role is pinged.
    - excluded_users: AllianceAuth users that should never receive automated tickets.
    """
    compliance_filter = models.ForeignKey(
        ComplianceFilter,
        related_name="compliance_filter",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        help_text="Select your compliance filter"
    )

    ticket_counter = models.PositiveIntegerField(default=0, help_text="Rolling counter for ticket channel names", editable=False)

    max_months_without_pap_compliance = models.PositiveIntegerField(
        default=1,
        help_text="How many months can a person be in corp w/o meeting the pap requirements? (this is a maximum points a user can get, 1 compliant month = plus 1 point, 1 non compliant = minus 1 point. If user has 0 points they get a ticket)"
    )

    starting_pap_compliance = models.PositiveIntegerField(
        default=1,
        help_text="How many buffer months does a new user get? (starter value of the above)"
    )

    char_removed_enabled = models.BooleanField(
        default=False,
        editable=True,
        help_text="Do you want to check for removed characters?"
    )

    awox_monitor_enabled = models.BooleanField(
        default=False,
        editable=True,
        help_text="Do you want to check for awox kills?"
    )

    corp_check_enabled = models.BooleanField(
        default=False,
        editable=True,
        help_text="Do you want to check for corp auth compliance?"
    )

    corp_check = models.PositiveIntegerField(
        default=30,
        help_text="How many days can a user be non compliant on Corp Auth before he should get kicked?"
    )

    corp_check_frequency = models.PositiveIntegerField(
        default=1,
        help_text="How often should a user be reminded (in days)"
    )

    corp_check_reason = models.TextField(
        default="# <@&{role}>,<@{namee}>\nSome of your characters are missing a valid token on corp auth, go fix it",
        blank=True,
        null=True,
        help_text="Message to send with {role} and {namee} variables"
    )

    corp_check_reminder = models.TextField(
        default="<@&{role}>,<@{namee}>, your compliance issue is still unresolved, you have {days} day(s) to fix it or you'll be kicked out.",
        blank=True,
        null=True,
        help_text="Message to send with {role}, {namee} and {days} variables"
    )

    paps_check_enabled = models.BooleanField(
        default=False,
        editable=True,
        help_text="Do you want to check for pap requirement compliance?"
    )

    paps_check = models.PositiveIntegerField(
        default=45,
        help_text="How many days can a user not meet the PAP requirements before he should get kicked?"
    )

    paps_check_frequency = models.PositiveIntegerField(
        default=1,
        help_text="How often should a user be reminded (in days)"
    )

    paps_check_reason = models.TextField(
        default="<@&{role}>,<@{namee}>, You have fallen below the threshold of months you get to be without meeting the pap requirements, fix it.",
        blank=True,
        null=True,
        help_text="Message to send with {role} and {namee} variables"
    )

    paps_check_reminder = models.TextField(
        default="Reminder that if you don't meet the PAP quota this month, you will be kicked out, you have {days} day(s) to fix it.",
        blank=True,
        null=True,
        help_text="Message to send with {days} variable"
    )

    afk_check_enabled = models.BooleanField(
        default=False,
        editable=True,
        help_text="Do you want to check if the user logs into the game??"
    )

    Max_Afk_Days = models.PositiveIntegerField(
        default=7,
        help_text="How many days can a user not login to game before he should get a ticket?"
    )

    afk_check = models.PositiveIntegerField(
        default=7,
        help_text="How many days can a user not login to game after getting a ticket before he should get a ticket?"
    )

    afk_check_frequency = models.PositiveIntegerField(
        default=1,
        help_text="How often should a user be reminded (in days)"
    )

    afk_check_reason = models.TextField(
        default="<@&{role}>,<@{namee}>, you have been inactive for over {days} day(s) without a LoA request, please fix it or submit a LoA request.",
        blank=True,
        null=True,
        help_text="Message to send with {role}, {namee} and {days} variables"
    )

    afk_check_reminder = models.TextField(
        default="<@&{role}>,<@{namee}>, your compliance issue is still unresolved, you have {days} day(s) to fix it or you'll be kicked out.",
        blank=True,
        null=True,
        help_text="Message to send with {role}, {namee} and {days} variables"
    )

    discord_check_enabled = models.BooleanField(
        default=False,
        editable=True,
        help_text="Do you want to check for discord activity?"
    )

    discord_check = models.PositiveIntegerField(
        default=2,
        help_text="How many days can a user not be on corp discord before he should get kicked?"
    )

    discord_check_frequency = models.PositiveIntegerField(
        default=1,
        help_text="How often should a user be reminded (in days)"
    )

    discord_check_reason = models.TextField(
        default="<@&{role}>,<@{namee}>, doesn't have their discord linked on corp auth, try to contact them and if unable, kick them out",
        blank=True,
        null=True,
        help_text="Message to send with {role} and {namee} variables"
    )

    discord_check_reminder = models.TextField(
        default="<@&{role}>,<@{namee}>'s compliance issue is still unresolved, try to contact them and if unable within {days} day(s) kick them out.",
        blank=True,
        null=True,
        help_text="Message to send with {role}, {namee} and {days} variables"
    )

    Category_ID = models.PositiveBigIntegerField(
        default=0,
        null=True,
        blank=True,
        help_text="Category ID to create the tickets in"
    )

    staff_roles = models.TextField(
        blank=True,
        help_text="Comma-separated list of staff role IDs allowed on tickets"
    )

    Role_ID = models.PositiveBigIntegerField(
        default=0,
        null=True,
        blank=True,
        help_text="Role ID to get pinged alongside the non compliant user"
    )

    excluded_users = models.ManyToManyField(
        User,
        related_name="excluded_users",
        blank=True,
        help_text="List of users to ignore when checking for compliance"
    )

    class Meta:
        verbose_name = "Ticket Tool Configuration"
        verbose_name_plural = "Ticket Tool Configuration"


class BBUpdateState(SingletonModel):
    """Singleton to persist BB update check timing/version across restarts."""
    update_check_time = models.DateTimeField(null=True, blank=True)
    latest_version = models.CharField(max_length=50, null=True, blank=True)

    def __str__(self):
        ts = self.update_check_time.isoformat() if self.update_check_time else "None"
        ver = self.latest_version or "None"
        return f"BBUpdateState(time={ts}, version={ver})"


class CharacterEmploymentCache(models.Model):
    """Cache of character employment timeline (intended 4h TTL)."""
    char_id = models.BigIntegerField(primary_key=True)
    data = models.JSONField()
    updated = models.DateTimeField(auto_now=True)
    last_accessed = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "character_employment_cache"
        indexes = [
            models.Index(fields=["updated"]),
            models.Index(fields=["last_accessed"]),
        ]


class FrequentCorpChangesCache(models.Model):
    """Cache of pre-rendered frequent corp changes HTML per user (intended 4h TTL)."""
    user_id = models.BigIntegerField(primary_key=True)
    html = models.TextField()
    updated = models.DateTimeField(auto_now=True)
    last_accessed = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "frequent_corp_changes_cache"
        indexes = [
            models.Index(fields=["updated"]),
            models.Index(fields=["last_accessed"]),
        ]


class CurrentStintCache(models.Model):
    """Cache of current stint days per (char, corp) (intended 4h TTL)."""
    char_id = models.BigIntegerField()
    corp_id = models.BigIntegerField()
    days = models.IntegerField(default=0)
    updated = models.DateTimeField(auto_now=True)
    last_accessed = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "current_stint_cache"
        unique_together = ("char_id", "corp_id")
        indexes = [
            models.Index(fields=["char_id", "corp_id"]),
            models.Index(fields=["updated"]),
            models.Index(fields=["last_accessed"]),
        ]


class AwoxKillsCache(models.Model):
    """Indefinite cache of AWOX kills per user; pruned by last_accessed (60d)."""
    user_id = models.BigIntegerField(primary_key=True)
    data = models.JSONField()
    updated = models.DateTimeField(auto_now=True)
    last_accessed = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "awox_kills_cache"
        indexes = [
            models.Index(fields=["updated"]),
            models.Index(fields=["last_accessed"]),
        ]

class LeaveRequest(models.Model):
    """
    Leave of Absence request stored in Auth so staff can audit time away.

    Fields:
    - user: AllianceAuth user submitting the request.
    - main_character: snapshot of the main character name at submission time.
    - start_date / end_date: requested AFK window.
    - reason: free-form explanation supplied by the user.
    - status: workflow flag (pending → approved/in_progress/finished/denied).
    - created_at: timestamp when the request was filed.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ("in_progress","In Progress"),
        ("finished",   "Finished"),
        ('denied', 'Denied'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='leave_requests')
    main_character = models.CharField(
        max_length=100,
        blank=True,
        help_text="The user's primary character when they made the request"
    )
    start_date = models.DateField()
    end_date   = models.DateField()
    reason     = models.TextField()
    status     = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Leave of Absence Request"
        verbose_name_plural = "Leave of Absence Requests"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username}: {self.start_date} → {self.end_date} ({self.status})"


class CorporationInfoCache(models.Model):
    """
    24h TTL cache of ESI corporation info.

    Fields:
    - corp_id: primary key / EVE corporation id.
    - name: most recently fetched corp name.
    - member_count: current member count snapshot.
    - updated: Django-managed timestamp refreshed on save.
    """
    corp_id = models.BigIntegerField(primary_key=True)
    name = models.CharField(max_length=255)
    member_count = models.IntegerField(default=0)
    updated = models.DateTimeField(auto_now=True)  # auto-updated on save

    class Meta:
        db_table = "corporation_info_cache"
        indexes = [
            models.Index(fields=["updated"]),
        ]

    @property
    def is_fresh(self):
        """Check if cache entry is still valid (24h TTL)."""
        return timezone.now() - self.updated < timedelta(hours=24)


class AllianceHistoryCache(models.Model):
    """
    Cached alliance membership timeline per corporation.

    Fields:
    - corp_id: corporation used for the alliance history fetch.
    - history: serialized list of {alliance_id, start_date} entries.
    - updated: auto timestamp.
    """
    corp_id = models.BigIntegerField(primary_key=True)
    history = JSONField()  # store list of {alliance_id, start_date}
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "alliance_history_cache"
        indexes = [
            models.Index(fields=["updated"]),
        ]

    @property
    def is_fresh(self):
        """Check if data is still within TTL."""
        return timezone.now() - self.updated < timedelta(hours=24)


class SovereigntyMapCache(models.Model):
    """Single-row cache storing the ESI sovereignty map JSON."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)  # single row
    data = models.JSONField()
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sovereignty_map_cache"

    @property
    def is_fresh(self):
        return timezone.now() - self.updated < timedelta(hours=24)

class CharacterAccountState(models.Model):
    """Persistent record of whether a character is Alpha, Omega, or Unknown."""
    ALPHA = "alpha"
    OMEGA = "omega"
    UNKNOWN = "unknown"

    STATE_CHOICES = [
        (ALPHA, "Alpha"),
        (OMEGA, "Omega"),
        (UNKNOWN, "Unknown"),
    ]

    char_id = models.BigIntegerField(primary_key=True)
    skill_used = models.BigIntegerField(blank=True, null=True)
    state = models.CharField(max_length=10, choices=STATE_CHOICES)

    def __str__(self):
        return f"{self.char_id} - {self.state}"


class ComplianceTicket(models.Model):
    """
    Discord ticket metadata for compliance automation.

    Fields:
    - user: AllianceAuth user (may be null if deleted).
    - discord_user_id / discord_channel_id / ticket_id: Discord identifiers that receive the ticket.
    - reason: which compliance rule fired (corp/pap/afk/discord/etc.).
    - created_at: timestamp for when the ticket was opened.
    - last_reminder_sent: how many reminders have gone out.
    - is_resolved: boolean to stop further reminders.
    """
    REASONS = [
        ("corp_check", "Corp Compliance"),
        ("paps_check", "PAP Requirements"),
        ("afk_check", "Inactivity"),
        ("discord_check", "User is not on discord"),
        {"char_removed", "Character removed"},
        {"awox_kill", "AWOX kill found"},
    ]

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    discord_user_id = models.BigIntegerField()
    discord_channel_id = models.BigIntegerField(null=True, blank=True)
    ticket_id = models.BigIntegerField(null=True, blank=True)

    reason = models.CharField(max_length=20, choices=REASONS)
    created_at = models.DateTimeField(auto_now_add=True)
    last_reminder_sent = models.IntegerField(default=0)

    is_resolved = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Compliance Ticket"
        verbose_name_plural = "Compliance Tickets"
        ordering = ['-created_at']

    def __str__(self):
        return f"Ticket for {self.user} ({self.reason})"
