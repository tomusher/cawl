# Generated manually: durable lifecycle fencing/outbox state.
import uuid

import django.db.models.deletion
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("environments", "0006_ownerquotalock_templatenamelock_namespacelease"),
    ]

    operations = [
        migrations.AddField(
            model_name="environment",
            name="generation",
            field=models.UUIDField(default=uuid.uuid4, editable=False),
        ),
        migrations.AddField(
            model_name="environment",
            name="active_operation",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.CreateModel(
            name="LifecycleOperation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("generation", models.UUIDField(editable=False)),
                ("kind", models.CharField(choices=[("provision", "provision"), ("start", "start"), ("stop", "stop"), ("destroy", "destroy"), ("sync-ingress", "sync-ingress")], max_length=20)),
                ("state", models.CharField(choices=[("queued", "queued"), ("running", "running"), ("succeeded", "succeeded"), ("failed", "failed")], db_index=True, default="queued", max_length=20)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("environment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lifecycle_operations", to="environments.environment")),
            ],
            options={"ordering": ["created_at"]},
        ),
    ]
