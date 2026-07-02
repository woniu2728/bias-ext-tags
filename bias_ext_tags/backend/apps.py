from django.apps import AppConfig
from django.db.models.signals import post_delete, post_save


class TagsExtensionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    label = "tags"
    name = "bias_ext_tags.backend"
    verbose_name = "Bias Tags Extension"

    def ready(self):
        from bias_ext_tags.backend.models import Tag
        from bias_ext_tags.backend.responses import bump_anonymous_tag_index_cache_version

        def invalidate_anonymous_tag_index(**kwargs):
            bump_anonymous_tag_index_cache_version()

        post_save.connect(
            invalidate_anonymous_tag_index,
            sender=Tag,
            dispatch_uid="bias_ext_tags.invalidate_anonymous_tag_index_on_save",
        )
        post_delete.connect(
            invalidate_anonymous_tag_index,
            sender=Tag,
            dispatch_uid="bias_ext_tags.invalidate_anonymous_tag_index_on_delete",
        )

