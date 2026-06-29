from __future__ import annotations

from urllib.parse import unquote

from django.utils.text import slugify


class TagSlugDriver:
    def generate(self, source, *, explicit_slug: str = "", context: dict | None = None) -> str:
        value = str(explicit_slug or source or "").strip()
        return slugify(value, allow_unicode=True).strip()

    def to_slug(self, instance, *, context: dict | None = None) -> str:
        return str(getattr(instance, "slug", "") or "").strip()

    def from_slug(self, slug: str, *, context: dict | None = None):
        from bias_ext_tags.backend.models import Tag
        from bias_ext_tags.backend.services import TagService

        try:
            return TagService.filter_tags_for_user(
                Tag.objects.all(),
                (context or {}).get("user"),
                action="view",
            ).get(slug__iexact=unquote(str(slug or "").strip()))
        except Tag.DoesNotExist:
            return None

    def from_slugs(self, slugs, *, context: dict | None = None) -> dict[str, object]:
        from bias_ext_tags.backend.models import Tag
        from bias_ext_tags.backend.services import TagService

        decoded_to_input = {
            unquote(str(slug or "").strip()).lower(): str(slug or "").strip()
            for slug in slugs or ()
            if str(slug or "").strip()
        }
        tags = TagService.filter_tags_for_user(
            Tag.objects.filter(slug__in=decoded_to_input.keys()),
            (context or {}).get("user"),
            action="view",
        )
        return {
            decoded_to_input[tag.slug.lower()]: tag
            for tag in tags
            if tag.slug.lower() in decoded_to_input
        }


class TagIdWithSlugDriver:
    def to_slug(self, instance, *, context: dict | None = None) -> str:
        tag_id = str(getattr(instance, "id", "") or "").strip()
        if not tag_id:
            return ""
        slug = str(getattr(instance, "slug", "") or "").strip()
        return f"{tag_id}-{slug}" if slug else tag_id

    def from_slug(self, slug: str, *, context: dict | None = None):
        from bias_ext_tags.backend.models import Tag
        from bias_ext_tags.backend.services import TagService

        tag_id = self._id(slug)
        if tag_id is None:
            return None
        try:
            return TagService.filter_tags_for_user(
                Tag.objects.all(),
                (context or {}).get("user"),
                action="view",
            ).get(id=tag_id)
        except Tag.DoesNotExist:
            return None

    def from_slugs(self, slugs, *, context: dict | None = None) -> dict[str, object]:
        from bias_ext_tags.backend.models import Tag
        from bias_ext_tags.backend.services import TagService

        id_to_input = {}
        for slug in slugs or ():
            normalized = str(slug or "").strip()
            tag_id = self._id(normalized)
            if tag_id is not None:
                id_to_input[tag_id] = normalized
        if not id_to_input:
            return {}
        tags = TagService.filter_tags_for_user(
            Tag.objects.filter(id__in=id_to_input.keys()),
            (context or {}).get("user"),
            action="view",
        )
        return {
            id_to_input[tag.id]: tag
            for tag in tags
            if tag.id in id_to_input
        }

    @staticmethod
    def _id(slug: str) -> int | None:
        value = str(slug or "").strip().split("-", 1)[0]
        try:
            tag_id = int(value)
        except (TypeError, ValueError):
            return None
        return tag_id if tag_id > 0 else None
