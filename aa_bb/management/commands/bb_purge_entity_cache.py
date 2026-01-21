import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from aa_bb.models import BigBrotherConfig, EntityInfoCache

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
        parser.add_argument(
            '--optimize',
            action='store_true',
            help='Optimize the table after purging to reclaim space'
        )

    def handle(self, *args, **options):
        days = options['days']
        optimize = options['optimize']
        threshold = timezone.now() - timedelta(days=days)
        table_name = EntityInfoCache._meta.db_table

        confirm_msg = "This will disable AA BB during the purge and may take a long time if the DB is large."
        if optimize:
            confirm_msg += " Optimization is also enabled and will increase execution time."
        confirm_msg += " Do you wish to continue y/n: "

        confirm = input(confirm_msg)
        if confirm.lower() != 'y':
            self.stdout.write("Aborting.")
            return

        config = BigBrotherConfig.get_solo()
        original_status = config.is_active

        self.stdout.write("Disabling BigBrother during purge...")
        config.is_active = False
        config.save()

        try:
            self.stdout.write(f"Purging {table_name} entries older than {days} days ({threshold})...")

            with connection.cursor() as cursor:
                # Using raw SQL to bypass ORM overhead and potential memory issues
                # with large querysets.
                cursor.execute(f"DELETE FROM {table_name} WHERE updated < %s", [threshold])
                row_count = cursor.rowcount

                self.stdout.write(self.style.SUCCESS(f"Successfully purged {row_count} entries from {table_name}."))
                logger.info(f"bb_purge_entity_cache: Purged {row_count} entries from {table_name}.")

                if optimize:
                    self.stdout.write(f"Optimizing {table_name}...")
                    vendor = connection.vendor
                    if vendor in ['mysql', 'mariadb']:
                        cursor.execute(f"OPTIMIZE TABLE {table_name}")
                    elif vendor == 'postgresql':
                        # Note: VACUUM cannot run inside a transaction.
                        # Django management commands don't wrap handle() in a transaction by default.
                        cursor.execute(f"VACUUM ANALYZE {table_name}")
                    elif vendor == 'sqlite':
                        cursor.execute("VACUUM")
                    else:
                        self.stdout.write(self.style.WARNING(f"Optimization not implemented for database vendor: {vendor}"))

                    self.stdout.write(self.style.SUCCESS(f"Optimization complete for {table_name}."))
                    logger.info(f"bb_purge_entity_cache: Optimized {table_name} ({vendor}).")

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Error during purge/optimization of {table_name}: {e}"))
            logger.error(f"bb_purge_entity_cache: Error during purge/optimization of {table_name}: {e}", exc_info=True)
        finally:
            self.stdout.write(f"Restoring BigBrother status to {original_status}...")
            config.is_active = original_status
            config.save()
