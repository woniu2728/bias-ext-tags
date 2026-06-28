from django.db import migrations, models


def sync_primary_flags(apps, schema_editor):
    Tag = apps.get_model("tags", "Tag")
    Tag.objects.filter(position__isnull=False).update(is_primary=True)


class Migration(migrations.Migration):

    dependencies = [
        ("tags", "0005_nullable_tag_position"),
    ]

    operations = [
        migrations.AddField(
            model_name="tag",
            name="is_primary",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(sync_primary_flags, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="tag",
            index=models.Index(fields=["is_primary", "parent", "position"], name="tags_primary_parent_pos_idx"),
        ),
        migrations.AlterField(
            model_name="tag",
            name="is_primary",
            field=models.BooleanField(default=True),
        ),
    ]
