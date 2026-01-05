
## [3.2.6] -
- Fixed issue where users were reported for having hostile standings towards hostiles.
- Fixed issue where Recurring Stats Webhook did not fall back if left empty to the main webhook.
- Fixed issue where clones and assets in friendly citadels were incorrectly flagged as hostile in hostile systems.
- Improved Discord message formatting to remove redundant vertical space in bulleted lists.
- Made `aadiscordbot` an optional dependency.
   - Hidden `aa-afat` and `aadiscordbot` specific compliance settings and UI elements when the respective apps are not installed.
- Tickets
   - Added support for Private Channels (via Bot), Private Threads (via Bot), Public Forum Threads (via Webhook), and Auth-Only ticket modes.
      - Threads persist and are not deleted, instead they are archived or closed.
      - They are reopened if the same user has the same issue again.
      - New threads are created per user and per issue, so one user may have multiple active issues.
    - Auth based UI for managing tickets allows users who do not have `aadiscordbot` installed, or users who simply dont want to use Discord, to manage tickets
       - Messages sent in Discord ticket channel/thread(s) are now relayed back to the Auth UI, and staff comments made in Auth are automatically forwarded to the corresponding Discord channel/thread(s). (via Bot)
    - Added !resolved and /resolve-ticket commands to allow staff to archive or close ticket channels and threads directly from Discord. (via Bot)
    - Added configurable message templates for AWOX kills and Character Removal events in the TicketToolConfig.
    - Fixed an issue where ticket reminders were sent every hour; they now correctly respect the configured daily interval.

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
