from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("environments", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="template",
            name="owner",
            field=models.CharField(blank=True, db_index=True, max_length=200),
        ),
    ]
