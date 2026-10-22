# Django
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aa_bb", "0079_helptext_guidance"),
    ]

    operations = [
        migrations.AddField(
            model_name="bigbrotherconfig",
            name="dlc_corp_brother_active",
            field=models.BooleanField(
                default=False,
                editable=False,
                help_text="Read-only flag showing if the Corp Brother module is enabled for this token.",
            ),
        ),
        migrations.AddField(
            model_name="bigbrotherconfig",
            name="dlc_loa_active",
            field=models.BooleanField(
                default=False,
                editable=False,
                help_text="Read-only flag showing if the Leave of Absence module is enabled for this token.",
            ),
        ),
        migrations.AddField(
            model_name="bigbrotherconfig",
            name="dlc_pap_active",
            field=models.BooleanField(
                default=False,
                editable=False,
                help_text="Read-only flag showing if the PAP module is enabled for this token.",
            ),
        ),
        migrations.AddField(
            model_name="bigbrotherconfig",
            name="dlc_tickets_active",
            field=models.BooleanField(
                default=False,
                editable=False,
                help_text="Read-only flag showing if the Tickets module is enabled for this token.",
            ),
        ),
        migrations.AddField(
            model_name="bigbrotherconfig",
            name="dlc_reddit_active",
            field=models.BooleanField(
                default=False,
                editable=False,
                help_text="Read-only flag showing if the Reddit module is enabled for this token.",
            ),
        ),
        migrations.AddField(
            model_name="bigbrotherconfig",
            name="dlc_daily_messages_active",
            field=models.BooleanField(
                default=False,
                editable=False,
                help_text="Read-only flag showing if the Daily Messages module is enabled for this token.",
            ),
        ),
    ]
