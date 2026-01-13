# Generated migration for contact and courier contract exclusions

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('aa_bb', '0107_remove_tickettoolconfig_role_id_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='bigbrotherconfig',
            name='exclude_neutral_contacts',
            field=models.BooleanField(default=False, help_text='If enabled, contacts with neutral standing (0) to hostile entities will be excluded from user checks and notifications', verbose_name='Exclude Neutral Contacts from Checks'),
        ),
        migrations.AddField(
            model_name='bigbrotherconfig',
            name='exclude_hauling_corps_from_courier',
            field=models.BooleanField(default=False, help_text='If enabled, courier contracts handled by major hauling corporations will be excluded from checks', verbose_name='Exclude Hauling Corps from Courier Contracts'),
        ),
        migrations.AddField(
            model_name='bigbrotherconfig',
            name='custom_hauling_corps',
            field=models.TextField(blank=True, default='', help_text='Additional corporation IDs (comma-separated) to exclude from courier contract checks when the above setting is enabled. Built-in: MOONFIRE (98681117), Push Industries (98079862), Purple Frog (98421812), Black Frog (384667640), Red Frog (1495741119)', verbose_name='Custom Hauling Corporation IDs'),
        ),
    ]
