from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aa_bb", "0122_bigbrotherconfig_sp_inject_detection_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="bigbrotherconfig",
            name="manual_main_corporation_id",
            field=models.BigIntegerField(
                blank=True,
                default=0,
                help_text="Corporation ID to use as the primary/main corporation when manual override is enabled.",
                verbose_name="Manual Main Corporation ID",
            ),
        ),
        migrations.AddField(
            model_name="bigbrotherconfig",
            name="manual_main_corporation_override",
            field=models.BooleanField(
                default=False,
                help_text="If enabled, BigBrother uses the manual corporation ID below instead of auto-detecting the main corporation from a superuser character.",
                verbose_name="Manual Main Corporation Override",
            ),
        ),
    ]
