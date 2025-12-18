# Change Log

## [3.1.2 Beta 2] - 2025-12-18

### Changed
- Attempt to optimize clone state, transactions, mails, contacts and contracts to reduce load times on users that have many characters.
- Fixed issue where notifications were still being sent for users who did not have previous data (new installs or new audits)
- Fixed LOA notifications of users outside LOA maximum without LOA, despite LOA being turned off.
- Hid "is user on blacklist" as this is redundant as we do not currently check any external sources (this is in development)
- Removed blacklist as a requirement, options will auto hide.
- Removed AFAT as a requirement, options will auto hide.

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
