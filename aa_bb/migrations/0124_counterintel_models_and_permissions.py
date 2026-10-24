# Django
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aa_bb", "0123_bigbrotherconfig_manual_main_corporation_override"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="general",
            options={
                "default_permissions": (),
                "managed": False,
                "permissions": (
                    ("basic_access", "Can access Big Brother"),
                    ("full_access", "Can view all main characters in Big Brother"),
                    (
                        "recruiter_access",
                        "Can view main characters in Guest state only in Big Brother",
                    ),
                    ("basic_access_cb", "Can access Corp Brother"),
                    ("full_access_cb", "Can view all corps in Corp Brother"),
                    (
                        "recruiter_access_cb",
                        "Can view guest's corps only in Corp Brother",
                    ),
                    ("can_blacklist_characters", "Can add characters to blacklist"),
                    (
                        "can_access_loa",
                        "Can access and submit a Leave Of Absence request",
                    ),
                    ("can_view_all_loa", "Can view all Leave Of Absence requests"),
                    ("can_manage_loa", "Can manage Leave Of Absence requests"),
                    ("can_access_paps", "Can access PAP Stats"),
                    ("can_generate_paps", "Can generate PAP Stats"),
                    ("ticket_manager", "Can manage compliance tickets"),
                    ("can_view_ci", "Can view Counterintelligence information"),
                    (
                        "can_manage_ci",
                        "Can manage Counterintelligence operations and baselines",
                    ),
                    (
                        "can_review_ci",
                        "Can review Counterintelligence investigations",
                    ),
                ),
            },
        ),
        migrations.CreateModel(
            name="CIOperation",
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
                ("name", models.CharField(max_length=255)),
                (
                    "operation_type",
                    models.CharField(default="strategic", max_length=64),
                ),
                ("start_time", models.DateTimeField(db_index=True)),
                (
                    "end_time",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                (
                    "secrecy_level",
                    models.CharField(default="standard", max_length=32),
                ),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "fleet_commander",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ci_commanded_operations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "CI Operation",
                "verbose_name_plural": "CI Operations",
                "ordering": ["-start_time"],
            },
        ),
        migrations.CreateModel(
            name="CIOperationObjective",
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
                ("name", models.CharField(max_length=255)),
                ("objective_type", models.CharField(max_length=64)),
                ("expected_roles", models.JSONField(blank=True, default=list)),
                ("disclosure_time", models.DateTimeField(blank=True, null=True)),
                (
                    "operation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="objectives",
                        to="aa_bb.cioperation",
                    ),
                ),
            ],
            options={
                "verbose_name": "CI Operation Objective",
                "verbose_name_plural": "CI Operation Objectives",
            },
        ),
        migrations.CreateModel(
            name="CIOverride",
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
                ("reason", models.TextField()),
                ("exempt_anomaly_types", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_ci_overrides",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ci_overrides",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "CI Override",
                "verbose_name_plural": "CI Overrides",
            },
        ),
        migrations.CreateModel(
            name="CISession",
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
                ("source", models.CharField(max_length=64)),
                ("start_time", models.DateTimeField(db_index=True)),
                (
                    "end_time",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                ("channel_name", models.CharField(blank=True, max_length=255)),
                ("reconnect_count", models.PositiveIntegerField(default=0)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ci_sessions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "CI Session",
                "verbose_name_plural": "CI Sessions",
                "ordering": ["-start_time"],
            },
        ),
        migrations.CreateModel(
            name="CIInvestigation",
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
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Needs Review"),
                            ("in_review", "Under Investigation"),
                            ("cleared", "Cleared / Explained"),
                            ("escalated", "Escalated"),
                            ("dismissed", "Dismissed"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=32,
                    ),
                ),
                ("agent_summary", models.TextField(blank=True)),
                ("reviewer_notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "assigned_reviewer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="assigned_ci_cases",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "operation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="investigations",
                        to="aa_bb.cioperation",
                    ),
                ),
                (
                    "subject_user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ci_investigations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "CI Investigation",
                "verbose_name_plural": "CI Investigations",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="CIIdentity",
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
                    "character_id",
                    models.BigIntegerField(blank=True, db_index=True, null=True),
                ),
                (
                    "discord_id",
                    models.BigIntegerField(blank=True, db_index=True, null=True),
                ),
                (
                    "mumble_username",
                    models.CharField(
                        blank=True, db_index=True, max_length=255, null=True
                    ),
                ),
                (
                    "effective_from",
                    models.DateTimeField(
                        db_index=True, default=django.utils.timezone.now
                    ),
                ),
                (
                    "effective_until",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                ("is_verified", models.BooleanField(default=False)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ci_identities",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "CI Identity",
                "verbose_name_plural": "CI Identities",
            },
        ),
        migrations.CreateModel(
            name="CIEvent",
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
                ("source", models.CharField(db_index=True, max_length=64)),
                (
                    "source_event_id",
                    models.CharField(db_index=True, max_length=255),
                ),
                ("timestamp", models.DateTimeField(db_index=True)),
                ("payload", models.JSONField(default=dict)),
                ("dedup_hash", models.CharField(max_length=64, unique=True)),
                (
                    "operation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="events",
                        to="aa_bb.cioperation",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ci_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "CI Event",
                "verbose_name_plural": "CI Events",
                "ordering": ["-timestamp"],
            },
        ),
        migrations.CreateModel(
            name="CICorrelation",
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
                ("anomaly_type", models.CharField(max_length=64)),
                ("confidence", models.FloatField(default=0.0)),
                ("data_completeness", models.FloatField(default=1.0)),
                ("algorithm_version", models.CharField(max_length=32)),
                ("source_event_ids", models.JSONField(default=list)),
                ("explanation", models.TextField()),
                ("computed_at", models.DateTimeField(auto_now_add=True)),
                (
                    "operation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="correlations",
                        to="aa_bb.cioperation",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ci_correlations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "CI Correlation",
                "verbose_name_plural": "CI Correlations",
                "ordering": ["-computed_at"],
            },
        ),
        migrations.CreateModel(
            name="CIOperationParticipant",
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
                ("character_id", models.BigIntegerField(db_index=True)),
                ("confirmed_by_fc", models.BooleanField(default=True)),
                (
                    "recorded_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                (
                    "operation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="participants",
                        to="aa_bb.cioperation",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ci_participations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "CI Operation Participant",
                "verbose_name_plural": "CI Operation Participants",
            },
        ),
        migrations.AddIndex(
            model_name="cioperationparticipant",
            index=models.Index(
                fields=["operation", "user"],
                name="aa_bb_cioper_operati_8f51ab_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="cioperationparticipant",
            index=models.Index(
                fields=["character_id"], name="aa_bb_cioper_charact_62b1af_idx"
            ),
        ),
        migrations.AlterUniqueTogether(
            name="cioperationparticipant",
            unique_together={("operation", "character_id")},
        ),
        migrations.AddIndex(
            model_name="cioperation",
            index=models.Index(
                fields=["start_time", "end_time"],
                name="aa_bb_cioper_start_t_f4c9c8_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="cioperation",
            index=models.Index(
                fields=["operation_type"], name="aa_bb_cioper_operati_21cd7f_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="cisession",
            index=models.Index(
                fields=["user", "start_time"],
                name="aa_bb_cises_user_id_4dc810_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="cisession",
            index=models.Index(
                fields=["source", "start_time"],
                name="aa_bb_cises_source_5d4d3c_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="ciinvestigation",
            index=models.Index(
                fields=["status", "created_at"],
                name="aa_bb_ciinve_status_f3d523_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="ciinvestigation",
            index=models.Index(
                fields=["subject_user", "status"],
                name="aa_bb_ciinve_subject_9bc21a_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="ciidentity",
            index=models.Index(
                fields=["user", "effective_from", "effective_until"],
                name="aa_bb_ciiden_user_id_e054a8_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="ciidentity",
            index=models.Index(
                fields=["character_id", "effective_from"],
                name="aa_bb_ciiden_charact_0d2e8b_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="ciidentity",
            index=models.Index(
                fields=["discord_id", "effective_from"],
                name="aa_bb_ciiden_discord_cbe839_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="ciidentity",
            index=models.Index(
                fields=["mumble_username", "effective_from"],
                name="aa_bb_ciiden_mumble__c9eef5_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="cievent",
            index=models.Index(
                fields=["source", "timestamp"],
                name="aa_bb_cieven_source_04724a_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="cievent",
            index=models.Index(
                fields=["user", "timestamp"],
                name="aa_bb_cieven_user_id_0df111_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="cievent",
            index=models.Index(
                fields=["operation", "timestamp"],
                name="aa_bb_cieven_operati_ceebba_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="cicorrelation",
            index=models.Index(
                fields=["operation", "user"],
                name="aa_bb_cicorr_operati_e26601_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="cicorrelation",
            index=models.Index(
                fields=["anomaly_type", "confidence"],
                name="aa_bb_cicorr_anomaly_ea531c_idx",
            ),
        ),
    ]
