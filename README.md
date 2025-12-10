# BigBrother

BigBrother is an Alliance Auth plugin for EVE Online corps and alliances.
It gathers the intel you already have—ESI, zKillboard, Corptools, Charlink, FATs—and turns it into one actionable dashboard so leadership can act fast without digging through spreadsheets.

## What BigBrother Does for You

- **See everything about a pilot in one place** – Pick a member and their page lights up with cards for corp history, compliance, suspicious mails/contracts/transactions, Cyno state, clones and assets in hostile space, skills, killboard hits, etc. No more jumping between tools.
- **Watch entire corporations (CorpBrother)** – Flip to the corp-level dashboard to view the same intel rolled up by corporation so recruiters and diplo staff can vet groups just as quickly.
- **Self-service Leave of Absence** – Members submit LoA requests in-app, managers approve/deny from their own view, and Discord/webhook pings keep everyone informed.
- **Track PAP goals** – Enter PAP counts manually or pull them from FATs, then show members their totals, history charts, and compliance scores.
- **Automated compliance tickets** – Define rules (corp auth, PAPs, AFK, Discord link, removed alts, awox kills, etc.) and BigBrother opens/updates Discord tickets automatically until the user is compliant again.
- **Optional recruitment autopilot** – Rotate Reddit recruitment posts, respect cooldowns, and alert when replies land (feature currently paused until Reddit’s API settles but ready for reuse).
- **Constant reminders, zero button mashing** – Scheduled Celery jobs warm caches, refresh statuses, and send preconfigured Discord notifications so officers only step in when something needs a human decision. The flagship task, `BB_run_regular_updates`, is enabled through Celery Beat when the app starts; it loops through every member, refreshes their status cards, and fires the right Discord alerts (pinging the message types configured in `BigBrotherConfig`) whenever clones move, Cynos change, hostiles appear, or compliance slips.

## Feature Highlights (under the hood)

- **Member security cockpit** – The `aa_bb` dashboard renders collapsible “cards” (see `views.py`) covering compliance, corp switches, awox kills, clone states, cyno capability, hostile assets/clones, suspicious mails/contracts/transactions, IMP & LAWN blacklist hits, skills, and more. Data is sourced via the modules in `aa_bb/checks/*`.
- **CorpBrother** – The sister site (`views_cb.py` + `urls_cb.py`) looks at corporations instead of individuals, sharing the same warm-cache + SSE loaders to spot hostile assets, transactions, or contracts at the corp level.
- **Leave of Absence (LoA)** – Users can file, edit, and track LoA requests (`LeaveRequest` in `modelss.py`, templates under `templates/loa`). Recruiters and directors get dedicated admin views plus Discord/webhook notifications for state changes.
- **PAP statistics** – The `paps` module (`views_paps.py`) pulls FAT data, group membership, and manual inputs to calculate corp/alliance/coalition PAPs, generates Matplotlib charts, and stores compliance scores in `PapCompliance`.
- **Ticket & compliance automation** – `tasks_tickets.py` ties Charlink `ComplianceFilter`, PAP targets, AFK status, corp membership, and Discord linkage together. Non-compliant users automatically receive Discord tickets via `aadiscordbot`, and tickets close themselves when the checks pass again.
- **Recruitment automation** – `modelss.BigBrotherRedditSettings` and `tasks_reddit.py` run a full Reddit posting workflow: OAuth, rotating message bank, cooldown enforcement, and reply monitoring with webhook pings. (currently not working due to reddit api changes)
- **Background workers with guardrails** – Celery tasks (`tasks*.py`) are pre-registered in `apps.py` with `django-celery-beat` schedules. They warm caches (`WarmProgress`), collect ESI scopes defined in `models.py`, sync corptools data, and rate-limit zKillboard calls so leadership sees near-real-time intel without overwhelming third-party APIs.

## Architecture at a Glance

- **Django app**: Lives in `aa_bb/aa_bb/` with standard `urls.py` + `views.py` pairs for BigBrother, CorpBrother, LoA, and PAPs.
- **Checks**: Every risk signal is isolated in `aa_bb/checks/` (awox, clone_state, corp_blacklist, cyno, hostile_assets/clones, imp/lawn blacklist, notifications, roles_and_tokens, skills, suspicious contacts/contracts/mails/transactions, etc.). Each module exposes both render helpers and raw data fetchers so they can be reused by views and Celery tasks.
- **Data model**: Configuration uses `solo` singletons (`BigBrotherConfig`, `TicketToolConfig`, `PapsConfig`, `BigBrotherRedditSettings`). Operational data is tracked via `UserStatus`, `CorpStatus`, async progress tables, and message queues for Discord/webhook output.
- **Integrations**: Relies on Alliance Auth ≥ 4.3.1, allianceauth-corptools, django-esi, aadiscordbot, django-celery-beat, Matplotlib, and (optionally) AFAT. Discord pings are routed through `send_message` (see `app_settings.py`), and killmail lookups use zKillboard + ESI hybrids.
- **Front-end**: Templates under `aa_bb/templates/**` ship with Bootstrap-based dashboards plus streamed updates (SSE endpoints in `views.py`) for huge contract/mail datasets. JavaScript in `templates/aa_bb/index.html` mirrors the `CARD_DEFINITIONS` in Python so the UI stays declarative.

## BigBrotherConfig Settings Cheat Sheet

- **Installation & Identity** – `main_corporation`/`main_alliance` and `is_active` are filled in by the updater after it inspects your superusers’ characters. You normally never touch these manually.
- **Module Entitlements** – `dlc_*_active` flags (CorpBrother, LoA, PAPs, Tickets, Reddit, Daily Messages) mirror what your installation unlocks. They flip automatically each time “BB: Run regular updates” executes.
- **Access Control** – `bb_guest_states`/`bb_member_states` define who counts as a guest vs. member; `member_*` fields let you whitelist outside corps/alliances; `ignored_corporations` hides IDs from CorpBrother menus and compliance checks.
- **Hostile & Whitelists** – `hostile_alliances`/`hostile_corporations` power the red highlights in cards, mails, contracts, and transactions; `whitelist_*` keeps friendly IDs safe even if the default data marks them as hostile.
- **Discord Notifications** – `pingroleID`, `pingroleID2`, and the `pingrole*_messages`/`here_messages`/`everyone_messages` relationships decide which MessageTypes mention which roles; `webhook`, `loawebhook`, `dailywebhook`, and `optwebhook1-5` route the alerts to different channels.
- **ESI Scope Enforcement** – `character_scopes` and `corporation_scopes` are comma-separated requirements; any missing scope is surfaced in the Compliance card so you can chase the pilot.
- **Scheduling & Toggles** – `dailyschedule` plus `optschedule1-5` pick which celery-beat schedule objects drive the daily/optional digests; `is_loa_active`, `is_paps_active`, `is_warmer_active`, and `are_*_messages_active` let you pause LoA/PAP modules, cache warming, or outbound messages without ripping out cron entries; `loa_max_logoff_days` sets how long someone can stay offline before LoA reminders escalate.

## PapsConfig Settings Cheat Sheet

- **Monthly Targets & Modifiers** – `required_paps`, `corp_modifier`, `max_corp_paps`, `alliance_modifier`, and `coalition_modifier` define how many PAPs matter per source each month.
- **Group-Based Awards** – `group_paps` plus `group_paps_modifier` grant bonus PAPs for specified Auth groups, while `capital_groups_get_paps` and the `cap/super/titan` group fields let you award fixed PAPs to capital programs.
- **Exclusions & Overrides** – `excluded_groups` and `excluded_groups_get_paps` control whether conflicting groups zero-out or cap the modifier, and `excluded_users` / `excluded_users_paps` let you opt individuals in or out of the different calculators without deleting data.

## TicketToolConfig Settings Cheat Sheet

- **General Filters** – `compliance_filter`, `max_months_without_pap_compliance`, `starting_pap_compliance`, `char_removed_enabled`, and `awox_monitor_enabled` tune who gets checked and which events can open tickets; `ticket_counter` is read-only and just labels Discord channels.
- **Corp Auth Checks** – `corp_check_enabled`, `corp_check`, `corp_check_frequency`, `corp_check_reason`, and `corp_check_reminder` determine when pilots missing corp auth access are warned and how often reminders fire.
- **PAP Compliance** – `paps_check_enabled` with the matching `_check`, `_frequency`, `_reason`, and `_reminder` fields mirrors the corp flow but for PAP scores pulled from `PapsConfig`.
- **AFK Monitoring** – `afk_check_enabled`, `Max_Afk_Days`, `afk_check`, `afk_check_frequency`, `afk_check_reason`, and `afk_check_reminder` leverage Charlink/GunAA logoff data to raise inactivity tickets.
- **Discord Linking** – `discord_check_enabled` plus its `_check`, `_frequency`, `_reason`, and `_reminder` templates enforce “link your Discord” policies when aadiscordbot says the account is missing.
- **Ticket Channels** – `Category_ID`, `staff_roles`, `Role_ID`, and `excluded_users` decide where tickets are created, who can see them, which escalation role gets pinged, and which pilots are ignored entirely.

## BigBrotherRedditSettings Cheat Sheet

- **OAuth & Identity** – `reddit_client_id`, `reddit_client_secret`, `reddit_user_agent`, `reddit_scope`, and `reddit_redirect_override` provide the credentials needed for Reddit OAuth; `reddit_account_name`, `reddit_access_token`, `reddit_refresh_token`, `reddit_token_type`, and `reddit_token_obtained` are filled automatically once you authorise.
- **Posting Behaviour** – `enabled`, `reddit_subreddit`, and `post_interval_days` decide if/where posts go and how long BigBrother waits between submissions; `last_submission_*` fields are read-only history.
- **Discord Notifications** – `reddit_webhook` plus `reddit_webhook_message` send confirmations into Discord, while `reply_message_template` shapes the alerts that fire when `monitor_reddit_replies` sees a new comment.
- **Message Pool** – Recruitment copy lives in `BigBrotherRedditMessage` entries; the scheduler marks `used_in_cycle` as posts go out to avoid repeats until the entire set has been used.

## Repository Map

```
aa_bb/
├── aa_bb/                 # Django app
│   ├── checks/            # All intel and compliance checks
│   ├── tasks*.py          # Celery tasks (core, corp, CT, tickets, reddit, bot)
│   ├── templates/         # BigBrother, CorpBrother, LoA, PAPs, FAQ views
│   ├── urls*.py           # Namespaced URLconfs (BigBrother, CorpBrother, LoA, PAPs)
│   ├── views*.py          # UI controllers and SSE helpers
│   ├── models.py          # Core models, user/corp status, messaging
│   ├── modelss.py         # Singleton configs, PAP + ticket data, Reddit settings
│   └── app_settings*.py   # ESI helpers, Discord webhooks, cache utilities
├── CHANGELOG.md
├── LICENSE
├── Makefile / tox.ini     # Dev workflow helpers
└── pyproject.toml         # Packaging metadata
```

## Requirements & Expectations

- Python 3.10+ / Django 4.2 (Alliance Auth dependency tree).
- Alliance Auth ≥ 4.3.1 with `allianceauth-corptools`, `django-esi`, `aadiscordbot`, and `django-celery-beat`.
- Celery workers + beat scheduler so the periodic tasks declared in `apps.py` can run.
- Access to Eve ESI scopes listed in `models.DEFAULT_CHARACTER_SCOPES` and `.DEFAULT_CORPORATION_SCOPES`, plus zKillboard and Reddit (if the module is enabled).
