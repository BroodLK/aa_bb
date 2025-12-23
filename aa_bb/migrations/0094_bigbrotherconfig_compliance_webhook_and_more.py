from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('aa_bb', '0093_eveitemprice_delete_bigbrotherredditmessage_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='bigbrotherconfig',
            name='compliance_webhook',
            field=models.URLField(blank=True, help_text='Discord webhook for sending compliance notifications', null=True, verbose_name='Compliance Discord Webhook'),
        ),
        migrations.AddField(
            model_name='bigbrotherconfig',
            name='corp_compliance_webhook',
            field=models.URLField(blank=True, help_text='Discord webhook for sending Corp compliance notifications', null=True, verbose_name='Corp Compliance Discord Webhook'),
        ),
        migrations.AddField(
            model_name='bigbrotherconfig',
            name='user_compliance_webhook',
            field=models.URLField(blank=True, help_text='Discord webhook for sending User compliance notifications', null=True, verbose_name='User Compliance Discord Webhook'),
        ),
    ]
