# Standard Library
import time
from datetime import timedelta

# Django
from django.core.management.base import BaseCommand
from django.db import close_old_connections, connection
from django.utils import timezone

# Alliance Auth
from allianceauth.services.hooks import get_extension_logger

# AA BigBrother
from aa_bb.models import BigBrotherConfig, EntityInfoCache

logger = get_extension_logger(__name__)


class Command(BaseCommand):
    help = "Purges old EntityInfoCache entries using raw SQL for maximum performance"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=2, help="Number of days of data to keep (default: 2)")
        parser.add_argument(
            "--optimize", action="store_true", help="Optimize the table after purging to reclaim space"
        )
        parser.add_argument("--truncate", action="store_true", help="Truncate the table entirely (ignores --days)")

    def get_table_size(self, cursor, table_name):
        vendor = connection.vendor
        try:
            if vendor in ["mysql", "mariadb"]:
                cursor.execute(
                    """
                    SELECT (data_length + index_length)
                    FROM information_schema.TABLES
                    WHERE table_schema = DATABASE()
                    AND table_name = %s
                """,
                    [table_name],
                )
                row = cursor.fetchone()
                return row[0] if row else 0
            elif vendor == "postgresql":
                cursor.execute("SELECT pg_total_relation_size(%s)", [table_name])
                row = cursor.fetchone()
                return row[0] if row else 0
            elif vendor == "sqlite":
                # Standard Library
                import os

                db_path = connection.settings_dict["NAME"]
                if os.path.exists(db_path):
                    return os.path.getsize(db_path)
        except Exception as e:
            logger.warning(f"Could not determine table size: {e}")
        return 0

    def handle(self, *args, **options):
        days = options["days"]
        optimize = options["optimize"]
        truncate = options["truncate"]
        threshold = timezone.now() - timedelta(days=days)
        table_name = EntityInfoCache._meta.db_table

        # Step 1: Initial warning and confirmation
        confirm = input(
            "Purging the EntityCache may take awhile if it's a large DB, BB will be disabled during the process, continue? (y/n): "
        )
        if confirm.lower() != "y":
            self.stdout.write("Aborting.")
            return

        # Step 2: Check DB size
        with connection.cursor() as cursor:
            size_bytes = self.get_table_size(cursor, table_name)
        size_gb = size_bytes / (1024**3)

        # Step 3: Check for large table and suggest truncate
        if size_gb > 2 and not truncate:
            self.stdout.write(self.style.WARNING(f"Your database is exceedingly large: ({size_gb:.2f} GB)"))
            confirm_truncate = input(
                "Did you want to truncate instead? While this may still take awhile it will be faster than a purge (y/n): "
            )
            if confirm_truncate.lower() == "y":
                truncate = True

        # Step 4: Ask about optimization if not truncating and not already requested via flag
        if not truncate and not optimize:
            confirm_optimize = input(
                "After the purge did you want to optimize the db? This may add more time to the overall process, but will reallocate space back to the OS. (y/n): "
            )
            if confirm_optimize.lower() == "y":
                optimize = True

        config = BigBrotherConfig.get_solo()
        original_status = config.is_active

        self.stdout.write("Disabling BigBrother...")
        BigBrotherConfig.objects.filter(pk=config.pk).update(is_active=False)

        close_old_connections()
        try:
            with connection.cursor() as cursor:
                if truncate:
                    self.stdout.write(f"Truncating {table_name}...")
                    vendor = connection.vendor
                    if vendor == "sqlite":
                        cursor.execute(f"DELETE FROM {table_name}")
                    else:
                        cursor.execute(f"TRUNCATE TABLE {table_name}")

                    self.stdout.write(self.style.SUCCESS(f"Successfully truncated {table_name}."))
                    logger.info(f"bb_purge_entity_cache: Truncated {table_name}.")
                else:
                    self.stdout.write(f"Purging {table_name} entries older than {days} days ({threshold})...")
                    # Using raw SQL to bypass ORM overhead and potential memory issues
                    # with large querysets. We chunk this to prevent long-held locks.
                    row_count = 0
                    batch_size = 10000
                    vendor = connection.vendor
                    while True:
                        if vendor in ["mysql", "mariadb"]:
                            cursor.execute(
                                f"DELETE FROM {table_name} WHERE updated < %s LIMIT %s", [threshold, batch_size]
                            )
                            batch_count = cursor.rowcount
                        elif vendor == "postgresql":
                            cursor.execute(
                                f"DELETE FROM {table_name} WHERE id IN (SELECT id FROM {table_name} WHERE updated < %s LIMIT %s)",
                                [threshold, batch_size],
                            )
                            batch_count = cursor.rowcount
                        else:
                            cursor.execute(f"DELETE FROM {table_name} WHERE updated < %s", [threshold])
                            batch_count = cursor.rowcount

                        row_count += batch_count
                        if batch_count < batch_size or vendor not in ["mysql", "mariadb", "postgresql"]:
                            break

                        self.stdout.write(f"Purged {row_count} entries...")
                        time.sleep(0.1)  # Yield to other connections

                    self.stdout.write(
                        self.style.SUCCESS(f"Successfully purged {row_count} entries from {table_name}.")
                    )
                    logger.info(f"bb_purge_entity_cache: Purged {row_count} entries from {table_name}.")

                if optimize:
                    self.stdout.write(f"Optimizing {table_name}...")
                    vendor = connection.vendor
                    close_old_connections()  # Ensure fresh state before long optimization
                    with connection.cursor() as opt_cursor:
                        if vendor in ["mysql", "mariadb"]:
                            opt_cursor.execute(f"OPTIMIZE TABLE {table_name}")
                        elif vendor == "postgresql":
                            opt_cursor.execute(f"VACUUM ANALYZE {table_name}")
                        elif vendor == "sqlite":
                            opt_cursor.execute("VACUUM")
                        else:
                            self.stdout.write(
                                self.style.WARNING(f"Optimization not implemented for database vendor: {vendor}")
                            )

                    self.stdout.write(self.style.SUCCESS(f"Optimization complete for {table_name}."))
                    logger.info(f"bb_purge_entity_cache: Optimized {table_name} ({vendor}).")

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Error during purge/optimization of {table_name}: {e}"))
            logger.error(f"bb_purge_entity_cache: Error during purge/optimization of {table_name}: {e}", exc_info=True)
        finally:
            close_old_connections()
            self.stdout.write(f"Restoring BigBrother status to {original_status}...")
            BigBrotherConfig.objects.filter(pk=config.pk).update(is_active=original_status)
