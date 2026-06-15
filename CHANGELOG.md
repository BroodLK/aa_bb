## [3.3.5] - 2026-06-15

> [!CAUTION]
> This release ends all Alliance Auth 4 compatibility.
> `aa_bb` 3.3.5 requires Alliance Auth 5.1.4 and newer.

### Changed
- Updated Eve Online model usage to be compliant with Alliance Auth 5.1.4.

### Fixed
- AWOX detection no longer triggers when the attacker and victim belong to the same auth user, including self-kills and alt-on-alt kills.
- Fixed the Ticket Tool Config admin page crashing with a 500 error by wiring the compliance filter admin field to the persisted `compliance_filter_id` field.

## [3.3.1] - 2026-03-24

### Changed
- Skill injection detection now supports configurable raw SP delta and SP/age ratio delta modes, with a toggle to switch between them.
- Can now manually set the main corp.

### Fixed
- Prevented crashes when a main character temporarily has no alliance (e.g., during an alliance switch), by avoiding null saves for main alliance fields.
- Issues with Corptools > 3.0.0b6

## [3.3.0] - 2026-03-02
> [!CAUTION]
> This version includes a dependency change, so please make sure to read the update
instructions carefully before updating to this version, otherwise, the app will
not work properly.

### Changed
- Centralized OpenAPI ESI handling via a shared `ESIHandler` wrapper
- Replaced `django-eveuniverse` usage with `django-eveonline-sde` and `modeltranslation` for SDE-backed static data.
- Admin log entries test
- Discord webhook name handling updated to support the new configurable webhook name.
- Optional message streams no longer add the extra titles such as "Optional Message 1".

### Fixed
- Hardened charlink compliance filter handling to be schema-stable regardless of plugin install/uninstall, with runtime checks and admin wiring.
- Added a cross-process zKillboard rate limiter and stopped forced refresh calls to reduce 429s on large installs.
- Capped streaming scans to the most recent 5,000 entries for both user and corp warm jobs.
- The process would fail if the user did not belong to any alliance.

## [3.2.10] - 2026-01-30


### Changed
- ZKill Backoff to comply with 10 requests per second API rule limit, this affects any installation that has more than 36,000 characters total AND any installation that has multiple users that exceed 10 characters on audit.


## [3.2.9] - 2026-01-22

> [!CAUTION]
>
> If you are upgrading from a previous version, you will need to run the `bb_purge_entity_cache` management command to clear the entity cache.
```bash
python manage.py bb_purge_entity_cache
```

### Added
- Compliance tickets now close automatically when a removed character is added back.
- Added a global on/off setting for Discord message activity tracking.

### Changed
- Background update tasks now use less memory and run more efficiently.
- Cached entity data expires sooner to prevent long-term buildup.
- More dashboard sections now load in real time with visible progress bars.
- Dashboard cards now wait for data to be ready before loading.
- Improved Discord bot stability to prevent database disconnects.
- The CT Kicker task no longer enables itself automatically.
- Added management command `bb_purge_entity_cache` for manual cache cleanup with an interactive flow: initial safety confirmation, automatic module deactivation, size-based truncation prompts, and optional table optimization.

### Fixed
- Resolved a dashboard loading error caused by invalid data handling.

## [3.2.8] - 2026-01-12

### Added
- **Compliance Ticket System**:
    - Discord slash commands for ticket management: `/resolve_compliance_ticket` and `/mark_ticket_as_exception`.
    - Automatic resolution of compliance tickets when corresponding Discord threads are archived, locked, or deleted.
    - Exception system to prevent repeated pings for persistent but acknowledged compliance issues.
    - Unified history on the Auth ticket page showing comments from both Discord and Auth.
- **Audit & Hostility Detection**:
    - Unified 23-step priority logic for hostility detection across assets, clones, and transactions.
    - Support for ignoring well-known and custom hauling corporations in courier contracts.
- **Visual & UI Enhancements**:
    - Reorganized BigBrother and TicketTool admin pages

### Fixed
- Refined nullsec hostility checks to prioritize individual structure/station ownership.
- Fixed logic for assets in solar systems (space) to prevent unnecessary warnings.
- Fixed BigBrother sidebar menu showing active when it wasn't issue.
- Refined cyno notifications to only trigger when skills cause ships are present. No more notifications for buying/selling/losing a ship, only when it changes the status from "can light = false" to true
- Improved data hydration for suspicious contracts, including financial details and better handling of public contracts.
- Prevented incorrect superuser fallbacks for pings when a target user has no Discord account linked.

### Other
- Reduced non-debug logging noise.


## [3.2.7] - 2026-01-8
- Removed `Charlink`, `allianceauth-discordbot` and `psycopg2-binary` as required dependencies
- Added rate limiting to Discord thread/channel creation.

## [3.2.6] - 2026-01-08
- ** Fixed an issue that prevented fresh installations from working.**
- Fixed an issue where users were reported for having hostile standings towards hostile entities.
- Fixed an issue where Recurring Stats Webhook did not fall back if left empty to the main webhook.
- Fixed an issue where clones and assets in friendly citadels were incorrectly flagged as hostile in hostile systems.
- Improved Discord message formatting to remove redundant vertical space in bulleted lists.
- Improved logging to track down module conflicts.
- Made `aadiscordbot` an optional dependency.
   - Hidden `aa-afat` and `aadiscordbot` specific ticket settings when the respective apps are not installed.
- Tickets
   - Added support for Private Channels (via Bot), Private Threads (via Bot), Public Forum Threads (via Webhook), and Auth-Only ticket modes.
      - Threads persist and are not deleted, instead they are archived or closed.
      - They are reopened if the same user has the same issue again.
      - New threads are created per user and per issue, so one user may have multiple active issues.
    - Auth based UI for managing tickets allows users who do not have `aadiscordbot` installed, or users who simply don't want to use Discord, to manage tickets
       - Messages sent in Discord ticket channel/thread(s) are now relayed back to the Auth UI, and staff comments made in Auth are automatically forwarded to the corresponding Discord channel/thread(s). (via Bot)
       - Tickets can be resolved via the management UI
    - Added configurable message templates for AWOX kills and Character Removal events in the TicketToolConfig.
    - Fixed an issue where ticket reminders were sent every hour; they now correctly respect the configured daily interval.
    - Added a background tracker that monitors the last message sent time (not the contents of the message) by users on Discord.
       - Discord Inactivity Check: New compliance check that can trigger tickets if a user has not spoken on Discord for a configured number of days.
    - Ability to either include, or not, the offending users in all tickets. (with exception to the discord not linked tickets)
    - Fixed an issue where after a set amount of time of the ticket not being resolved, the app would send a hardcoded message. This message was removed as it is redundant.

## [3.2.5] - 2025-12-28
- More robust hostile state handling, with the ability to add everyone who is not friendly to the hostile state.
- More robust awox handling
- Various visual improvements
- Exclude low sec
- Exclude high sec
- Fix typo

## [3.2.4] - 2025-12-24

### Changed
- Ability to ignore or show market transactions
- If showing market transactions:
  - Able to ignore major hubs: Rens, Hek, Dodixie, Jita, Amarr
  - Able to ignore secondary hubs: Oursulaert, Tash-Murkon Prime, Agil, Perimeter
  - Able to ignore custom-set systems
  - Toggle threshold alerts on/off
  - Set threshold percentage
  - Set Janice API key
  - Set Fuzz or Janice as primary (if Janice API is used, they will fall back on each other)
  - Set Fuzz main station (default: Jita)
  - Set whether Fuzz uses instant prices
  - Force updates only every 7 days and only when the alert is triggered
  - Configurable price update interval
- Attempted to fix new user spam again, and added new corp spam protection for corp audits
- Fixed potential bug in clone state
- Refactored some CorpBrother code
- Refactored logs throughout
- Configurable maintenance window
- Alerts when tasks are getting backed up
- Configurable threshold for when backlog alerts fire
- Configurable update window (normal daily updates occur here; default is one hour every hour, with tasks evenly distributed across the window, measured in seconds)
- Configurable cache time for clone states
- Removed Reddit module due to Reddit API changes
- Added tests
- Renamed Corp BL file
- Re-added Alliance and Coalition BL cards if enabled and Blacklist is installed; these are simple external links defined in the admin menu
- Increased searchable accounts on the auth page to 5000
- Ability to force recheck clone state every run
- Ability to use corporations with aa-contacts, not just alliances
- Ability to restrict all checks to the main corporation instead of everyone with the same state
- Added new FAQ page for recurring stats
- Updated FAQs and settings pages to reflect changes
- All messages sent to Discord as embeds
- Ability to mute clone state change notifications

## [3.2.3] - 2025-12-19

### Changed
- Optimize clone state, transactions, mails, contacts and contracts to reduce load times on users that have many characters.
- Fixed issue where notifications were still being sent for users who did not have previous data (new installs or new audits)
- Fixed LOA notifications of users outside LOA maximum without LOA, despite LOA being turned off.
- Hid "is user on blacklist" as this is redundant as we do not currently check any external sources (this is in development)
- Removed blacklist as a requirement, options will auto hide.
- Removed AFAT as a requirement, options will auto hide.
- AWOX performance improvements and logs

## [3.1.1 Beta 2] - 2025-12-17

### Changed
- Fix Notifications

## [3.1.0 Beta 2] - 2025-12-17

### Added
- AA-Contacts Integration
  - If installed and integration enabled, the task will pull contacts into BB to auto-populate hostile and member alliances/corporations
    - Task automatically imports alliances into hostile/member sections of BigBrother, and will automatically remove alliances when you remove them from your contacts.
    - Task creates a cache of what it imports, meaning that you can manually add and delete corporations and alliances not in your contacts and they will not be affected

### Changes
- Changed background colors of the module activation page
- Cleaned up admin menu
- Updated Card Descriptions in the Manual
- Updated Configuration Descriptions in the Manual
- Updated FAQ in the Manual

### Removed
- References and functions related to DLC

## [3.1.0 Beta 1] - 2025-12-12

### Initial Release
