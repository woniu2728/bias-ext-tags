from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tags", "0006_tag_is_primary"),
    ]

    operations = [
        migrations.AddField(
            model_name="tag",
            name="default_sort",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
    ]
