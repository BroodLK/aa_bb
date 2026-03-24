from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('aa_bb', '0120_rename_alliance_hi_updated_df5e96_idx_aa_bb_allia_updated_732c6f_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='bigbrotherconfig',
            name='sp_inject_threshold',
            field=models.PositiveIntegerField(
                default=300000,
                help_text='Minimum SP delta required to flag a skill injection alert',
                verbose_name='Skill Injection Threshold (SP)',
            ),
        ),
    ]
