from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tags", "0004_tagstate"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tag",
            name="position",
            field=models.IntegerField(blank=True, default=0, null=True),
        ),
        migrations.AddIndex(
            model_name="tag",
            index=models.Index(fields=["parent", "position", "name"], name="tags_parent_pos_name_idx"),
        ),
        migrations.AddIndex(
            model_name="tag",
            index=models.Index(fields=["position", "parent"], name="tags_position_parent_idx"),
        ),
    ]
