from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('aa_bb', '0121_bigbrotherconfig_sp_inject_threshold'),
    ]

    operations = [
        migrations.AddField(
            model_name='bigbrotherconfig',
            name='sp_inject_detection_mode',
            field=models.CharField(
                choices=[('raw', 'Raw SP delta'), ('ratio', 'SP/age ratio delta')],
                default='raw',
                help_text='How to detect skill injections',
                max_length=16,
                verbose_name='Skill Injection Detection Mode',
            ),
        ),
        migrations.AddField(
            model_name='bigbrotherconfig',
            name='sp_inject_ratio_delta',
            field=models.FloatField(
                default=0.0,
                help_text='Minimum SP/age ratio delta required to flag a skill injection alert',
                verbose_name='Skill Injection Ratio Delta',
            ),
        ),
    ]
