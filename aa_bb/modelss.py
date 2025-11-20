import logging

logger = logging.getLogger(__name__)

from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User
from django.db.models import JSONField

from allianceauth.authentication.models import UserProfile

try:
    from charlink.models import ComplianceFilter
except ImportError:
    logger.warning("charlink not installed")

from solo.models import SingletonModel
from datetime import timedelta

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
