"""
Celery beat configuration for aa_bb.

Each entry mirrors the tasks created in AppConfig.ready so installations that
prefer static CELERY_BEAT_SCHEDULE definitions can reuse this module.
"""

from celery.schedules import crontab, schedule

CELERY_BEAT_SCHEDULE = {
    'BB-run-regular-updates-every-hour': {
        'task': 'aa_bb.tasks.BB_run_regular_updates',
        'schedule': crontab(minute=0, hour='*'),  # Every hour on the hour
    },
    'CB-run-regular-updates-every-hour': {
        'task': 'aa_bb.tasks_cb.CB_run_regular_updates',
        'schedule': crontab(minute=0, hour='*'),  # Every hour on the hour
    },
    'BB-run-regular-loa-updates-every-day': {
        'task': 'aa_bb.tasks_cb.BB_run_regular_loa_updates',
        'schedule': crontab(minute=0, hour=12),  # Every day at 12:00
    },
    'BB-check_member_compliance-every-day': {
        'task': 'aa_bb.tasks_cb.check_member_compliance',
        'schedule': crontab(minute=0, hour=12),  # Every day at 12:00
    },
    'BB-send-daily-message': {
        'task': 'aa_bb.tasks_cb.BB_send_daily_messages',
        'schedule': crontab(minute=0, hour=12),  # Every day at 12:00
    },
    'BB-send-opt-message1': {
        'task': 'aa_bb.tasks_cb.BB_send_opt_message1',
        'schedule': crontab(minute=0, hour=12),  # Every day at 12:00
    },
    'BB-send-opt-message2': {
        'task': 'aa_bb.tasks_cb.BB_send_opt_message2',
        'schedule': crontab(minute=0, hour=12),  # Every day at 12:00
    },
    'BB-send-opt-message3': {
        'task': 'aa_bb.tasks_cb.BB_send_opt_message3',
        'schedule': crontab(minute=0, hour=12),  # Every day at 12:00
    },
    'BB-send-opt-message4': {
        'task': 'aa_bb.tasks_cb.BB_send_opt_message4',
        'schedule': crontab(minute=0, hour=12),  # Every day at 12:00
    },
    'BB-send-opt-message5': {
        'task': 'aa_bb.tasks_cb.BB_send_opt_message5',
        'schedule': crontab(minute=0, hour=12),  # Every day at 12:00
    },
    'BB-daily-DB-cleanup': {
        'task': 'aa_bb.tasks_cb.BB_daily_DB_cleanup',
        'schedule': crontab(minute=0, hour=1),  # Every day at 12:00
    },
    'BB-send-recurring-stats': {
        'task': 'aa_bb.tasks_other.BB_send_recurring_stats',
        'schedule': crontab(minute=0, hour=12, day_of_week=0),
    },
    'BB-sync-contacts-from-aa-contacts': {
        'task': 'aa_bb.tasks.BB_sync_contacts_from_aa_contacts',
        'schedule': crontab(minute=0, hour=1),
    }
}
