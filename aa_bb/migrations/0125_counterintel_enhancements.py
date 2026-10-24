# Django
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aa_bb", "0124_counterintel_models_and_permissions"),
    ]

    operations = [
        migrations.CreateModel(
            name="CILogIngestState",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "file_path",
                    models.CharField(db_index=True, max_length=512, unique=True),
                ),
                (
                    "last_inode",
                    models.BigIntegerField(blank=True, null=True),
                ),
                ("last_offset", models.BigIntegerField(default=0)),
                (
                    "last_ingested_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
            ],
            options={
                "verbose_name": "CI Log Ingest State",
                "verbose_name_plural": "CI Log Ingest States",
            },
        ),
        migrations.AddField(
            model_name="cicorrelation",
            name="factors",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="cicorrelation",
            name="risk_score",
            field=models.FloatField(default=0.0),
        ),
        migrations.AddField(
            model_name="ciinvestigation",
            name="dossier_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="ciinvestigation",
            name="resolution_tag",
            field=models.CharField(
                blank=True,
                choices=[
                    ("isp_outage", "ISP / Technical Outage"),
                    ("scout_alt", "Authorized Scout / Cyno Alt"),
                    ("fc_authorized_drop", "FC Authorized Disconnect / Standing Down"),
                    ("unlinked_alt", "Unlinked Alt Identified"),
                    ("confirmed_leak", "Confirmed Information Leak / Hostile Action"),
                    ("benign_coincidence", "Benign Schedule Coincidence"),
                    ("other", "Other"),
                ],
                db_index=True,
                default="",
                max_length=64,
            ),
        ),
        migrations.AddIndex(
            model_name="cicorrelation",
            index=models.Index(
                fields=["risk_score"], name="aa_bb_cicor_risk_sc_1349a1_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="ciinvestigation",
            index=models.Index(
                fields=["resolution_tag"], name="aa_bb_ciinv_resolut_80d720_idx"
            ),
        ),
    ]
