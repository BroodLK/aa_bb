import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from aa_bb.models import EntityInfoCache

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Purges old EntityInfoCache entries using raw SQL for maximum performance'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=2,
            help='Number of days of data to keep (default: 2)'
        )

    def handle(self, *args, **options):
        days = options['days']
        threshold = timezone.now() - timedelta(days=days)
        table_name = EntityInfoCache._meta.db_table

        self.stdout.write(f"Purging {table_name} entries older than {days} days ({threshold})...")

        try:
            with connection.cursor() as cursor:
                # Using raw SQL to bypass ORM overhead and potential memory issues
                # with large querysets.
                cursor.execute(f"DELETE FROM {table_name} WHERE updated < %s", [threshold])
                row_count = cursor.rowcount

            self.stdout.write(self.style.SUCCESS(f"Successfully purged {row_count} entries from {table_name}."))
            logger.info(f"bb_purge_entity_cache: Purged {row_count} entries from {table_name}.")
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Error purging {table_name}: {e}"))
            logger.error(f"bb_purge_entity_cache: Error purging {table_name}: {e}", exc_info=True)
