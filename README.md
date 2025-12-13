
> [!CAUTION]
> Because this repository is public, anyone can read the detection logic for skills, cyno activity, injected SP, suspicious transactions, hostile assets and clones, and other monitored behaviors. Hostile groups may use this information to avoid detection. Operate with discretion.

# BigBrother
BigBrother is an Alliance Auth plugin, **_originally written by Andrew Xadi_**, that performs continuous pilot auditing, compliance monitoring, intelligence gathering, and behavioral analysis. It monitors activity such as skills, cyno capabilities, SP injections, corporation movement, assets, clones, and more, then delivers structured leadership-focused reports.
> [!IMPORTANT]
> Users who used this tool while it was private can safely upgrade but may run into a rare but serious complication where duplicate tasks are generated preventing the auth from starting.

To correct the above, see instructions [here](#fix-duplicated-tasks-error)

> [!WARNING]
> This is a **beta** release. Please report issues through GitHub.

## Index

- [BigBrother](#bigbrother)
  - [Core Requirements](#core-requirements)
  - [Install Instructions](#install-instructions)
- [Features](#features)
  - [Dashboard](#dashboard)
  - [Corp Dashboard](#corp-dashboard)
  - [Discord Notifications](#discord-notifications)
  - [Ticket System](#ticket-system)
  - [Automated Discord Messages](#automated-discord-messages)
  - [Recurring stats](#recurring-stats)

## Core Requirements
### The following AllianceAuth plugins are **_required_**:

```md
allianceauth-afat >= 4.1.1
allianceauth-blacklist >= 0.1.1
allianceauth-corptools >= 2.12.0 (this app will adopt 3.0.0 as soon as version 3 is out of beta)
allianceauth-discordbot >= 4.1.0
aa-charlink >= 1.11.1
django-esi >= 8.2.0
```

You do not have to use afat, but it does need to be installed.

## Install Instructions
After making sure to add the above prerequisite applications.
```bash
source /home/allianceserver/venv/auth/bin/activate && cd /home/allianceserver/myauth/
```
```bash
pip install aa_bb
```
```bash
vi myauth/settings/local.py
```
add `aa_bb` to installed apps.
```bash
python manage.py migrate && python manage.py collectstatic
```
> [!IMPORTANT]
> It is recommended to use a threaded worker setup with memmon for this application. The following is an example

In your supervisor.conf
```bash
[program:worker]
command=/home/allianceserver/venv/auth/bin/celery -A myauth worker -P threads -c 10 -l INFO -n %(program_name)s_%(process_num)02d
directory=/home/allianceserver/myauth
user=allianceserver
numprocs=2
process_name=%(program_name)s_%(process_num)02d
stdout_logfile=/home/allianceserver/myauth/log/worker.log
stderr_logfile=/home/allianceserver/myauth/log/worker.log
autostart=true
autorestart=true
startsecs=10
stopwaitsecs = 600
killasgroup=true
priority=998

[eventlistener:memmon]
command=/home/allianceserver/venv/auth/bin/memmon -p worker_00=512MB -p worker_01=512MB -p -p gunicorn=512MB
directory=/home/allianceserver/myauth
events=TICK_60
stdout_logfile=/home/allianceserver/myauth/log/memmon.log
stderr_logfile=/home/allianceserver/myauth/log/memmon.log
```

> [!IMPORTANT]
> Failure to follow the next steps before running the initial tasks can cause an undesired result

In your AA Admin navigate to AA_BB
- Navigate to Big Brother Config
  - Under **_Core Activation_**
    - Make sure Warmer Is Active is enabled
    - Enable any features you plan to use
      - paps,
      - loa,
      - daily messages (messages that repeat every 24 hours),
      - recurring stats,
      - optional messages 1-5
      - Set the number of days for an LOA
  - Under Notifications
    - Select if you would like to opt out of any notifications sent to the main Discord Webhook for user changes
    - By default, the app will not send a notification when a new user adds their audit; however, this can be enabled.
      - When enabled, it will treat non-existent data as old data and send a notification to discord on all the user's stats, treating them as if they are changes.
  - Under Ping / Messaging Rules
    - Enter in your desired role ID that you wish to be pinged and select the conditions under which those roles will be pinged.
    - Select any @here conditions
    - Select any @everyone conditions
  - Under Webhooks
  - >Dont forget you can send it to a thread by using `https://discordapp.com/api/webhooks/<url>/<url>?thread_id=<threadid>`\
    The thread must be in the same channel that the webhook is configured to.
    - **The main "Webhook" This is used to send notifications of user and corp changes to Discord**
    - LOA Webhook
    - Daily Webhook
    - Recurring Stats
    - Optional Message Webhooks 1-5
  - Under Schedules
    - Configure specific schedules for daily messages, optional messages, and recurring stats.
  - Under User State and Membership
  >[!WARNING] Failure to configure this will result in AA_BB not working
    - Configure what states you consider "members" you will receive updates on these in discord
    - Configure what states you consider "guest" these will be preloaded into cache, but not notified in discord.
    - Configure what corporations you consider to be members, these are friendly entities.
      - You do not need to configure a corporation if your corporation is inside an alliance that is set as member
    - Configure what alliances you consider to be members.
    - Configure ignore corporations, such as alt corps, that will be ignored when checks are run
  - Under Hostile / Whitelist Rules
    - Configure Alliances you consider hostile
      - Coming Soon(tm) the ability to consider anyone who isn't a member /ignored as hostile
    - Configure Corporations you consider hostile
    - Configure Whitelisted Alliance and Corporations, these act the same as ignored and are... ignored
    - Configure if you consider all null sec, minus what you ignore/whitelist/member, as hostile.
    - Configure if all player structures are hostile, minus what you ignore/whitelist/member.
    - Configure if all npc stations are hostile, minus what you ignore/whitelist/member.
    - Configure Excluded systems and stations, these will be ignored and can be considered the same as "member" "ignored" or "whitelisted"

Once you are satisfied with the configuration, you may explore the other configurations available, such as ticket tool configuration, recurring stats, and daily and optional messages.

Okay, but now you want it to actually do the things, go to `Periodic Tasks` and **Enable** `BB run regular updates`
That's it. Let the timer click over, and it will create any missing tasks and start the process.

# Features

> [!WARNING]
> **_Reddit Support_**:\
> Reddit functionality requires paid API access. Because of this requirement, Reddit posting and monitoring features have not been tested.\
> As such, a list of what it does do will not be included in this readme for now.

## Dashboard

### The BigBrother dashboard provides a unified view of any pilot in your organization.
Selecting a user displays a set of analytical cards that summarize compliance, risk factors, and suspicious activity signals.

Tracked metrics include:

- **Blacklist Status**
  - Whether the pilot or any linked character appears on the blacklist.


- **Audit Completion**
  - Whether all characters and corporations associated with the user have been fully audited.


- **Corporation Stability**
  - Detection of short or erratic corporation history (“corp hopping”) that may indicate instability or intent to evade tracking.


- **AWOX Activity**
  - Identification of kills against friendly entities that may indicate internal security risks.


- **Account State**
  - Whether individual characters are Omega or Alpha, useful for evaluating cyno capability, skill progression, and account investment.


- **Hostile Jump Clone Placement**
  - Detection of jump clones located in regions or structures considered hostile.


- **Hostile Asset Placement**
  - Identification of assets located in hostile regions, including breakdown by character and location.


- **Hostile Contacts**
  - Checks for contacts marked as hostile, which may indicate ties to enemy groups.


- **Hostile Contracts**
  - Detection of contracts sent to or received from hostile entities, helping highlight supply-chain leaks or suspicious ISK movement.


- **Suspicious Mails**
  - Detection of in-game mail to or from entities that we consider hostile.


- **Suspicious Transactions**
  - Checks for transactions, such as player donations and trades, that may be related to hostile entity activity.


- **Cyno Check**
  - Provides a breakdown of what each character belonging to the user is capable of when it comes to cynos.
  - > This includes owning and being able to fly potentially interesting ships


- **Skill Check**
  A breakdown of potentially interesting skills


## Corp Dashboard
> [!WARNING]
> Corp Dashboard has not yet received much love

- **Suspicious Transactions**
  - Checks for transactions, such as corporation donations, that may be related to hostile entity activity.

- **Hostile Contracts**
  - Detection of contracts sent to or received from hostile entities, helping highlight supply-chain leaks or suspicious ISK movement.

- **Hostile Asset Placement**
  - Identification of assets located in hostile regions or structures.


## Discord Notifications
### All outbound Discord notifications are serialized through a dedicated task to ensure messages never overlap and always arrive in chronological order.
- Get instant notifications about any corp or user changes that have been listed above under their respective categories, each part of a user's discord notification is adjustable in the settings.

## Ticket System
### BigBrother can automatically generate tickets to notify leadership when pilots violate compliance or operational rules.

- **Triggers include**:
  - Charlink Compliance Filters
    - Detects when users have not added required applications or connections via aa-charlink.
  - PAP Compliance
    - Flags users who fall below configured PAP or activity thresholds.
  - Character Removal From Auth
    - Creates a ticket when a user removes a character from AllianceAuth, potentially hiding assets or behavior.
  - AWOX Activity
    - Generates a ticket when a pilot AWOXs a friendly character.
  - Missing Corporation Audit (Director Role)
    - Detects directors who have not enabled or completed corporation audits.
  - AFK Detection
    - Flags users who go AFK without registering an LOA in Auth.
  - Missing Discord Link
    - Generates a ticket when a user has not connected their Discord account to Auth.
### Non Specific Ticket Configuration
  - Ping Targets
    - Choose which roles to notify when a ticket is created.
  - Ticket Category
    - Tickets are created as new channels inside a category, deleting the channel will close the ticket
  - Exemptions
    - Users can be marked as exempt from specific checks to avoid ticket spam where it is unnecessary.

## Automated Discord Messages
- Configure an unlimited number of messages to be sent to up to five different discord webhooks, each with their own individual schedules.

## Recurring stats
- Send stats to a webhook that covers interesting statistics from AA


### Fix Duplicated Tasks error
Find the duplicate
```sql
SELECT
  minute,
  hour,
  day_of_week,
  day_of_month,
  month_of_year,
  timezone,
  COUNT(*) AS cnt
FROM django_celery_beat_crontabschedule
GROUP BY
  minute,
  hour,
  day_of_week,
  day_of_month,
  month_of_year,
  timezone
HAVING COUNT(*) > 1;
```
Get the ID, replace the cron with the duplicate values
```sql
SELECT
  id,
  minute,
  hour,
  day_of_week,
  day_of_month,
  month_of_year,
  timezone
FROM django_celery_beat_crontabschedule
WHERE
  minute = '0'
  AND hour = '12'
  AND day_of_week = '0'
  AND day_of_month = '*'
  AND month_of_year = '*'
  AND timezone = 'UTC';
```
Find out if any tasks are using the schedules, replace the numbers with the proper IDs
```sql
SELECT id, name, crontab_id
FROM django_celery_beat_periodictask
WHERE crontab_id IN (5, 12);
```
If some tasks are using both, reassign one of them
```sql
UPDATE django_celery_beat_periodictask
SET crontab_id = 5
WHERE crontab_id = 12;
```
Finally delete the duplicate
```sql
DELETE FROM django_celery_beat_crontabschedule
WHERE id IN (12);
```
