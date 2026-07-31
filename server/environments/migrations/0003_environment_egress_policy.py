from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("environments", "0002_template_owner")]
    operations = [migrations.AddField(
        model_name="environment", name="egress_policy",
        field=models.CharField(blank=True, max_length=100),
    )]
