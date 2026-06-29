import uuid
from typing import Any, Optional, List
from django.db import transaction
from django.db.models import Count, F, Max, Prefetch, Q, QuerySet, Value, Window
from django.db.models.functions import Coalesce, RowNumber
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.utils.text import slugify
from bias_core.extensions.platform import get_extension_settings
from bias_core.extensions.runtime import (
    generate_runtime_model_slug,
    get_runtime_forum_permissions,
    get_runtime_permission_model,
    has_runtime_forum_permission,
    resolve_runtime_model_slug,
    to_runtime_model_slug,
)
from bias_core.extensions.runtime import apply_runtime_counted_discussion_filter
from bias_ext_tags.backend.models import DiscussionTag, Tag, TagState
from bias_ext_tags.backend.tag_relationships import (
    get_discussion_tag_ids_for_stats,
    get_discussion_tags,
    tag_has_discussions,
)

_UNSET = object()


class TagService:
    """标签服务"""

    ACTION_SCOPE_FIELD = {
        "view": "view_scope",
        "start_discussion": "start_discussion_scope",
        "add_to_discussion": "start_discussion_scope",
        "reply": "reply_scope",
    }
    ACTION_RESTRICTED_PERMISSION = {
        "view": "viewForum",
        "start_discussion": "startDiscussion",
        "add_to_discussion": "startDiscussion",
        "reply": "discussion.reply",
    }
    TAG_ABILITY_PERMISSION = {
        "view": "viewForum",
        "viewForum": "viewForum",
        "start_discussion": "startDiscussion",
        "startDiscussion": "startDiscussion",
        "add_to_discussion": "startDiscussion",
        "addToDiscussion": "startDiscussion",
        "reply": "discussion.reply",
        "discussion.reply": "discussion.reply",
    }
    TAG_MANAGEMENT_PERMISSION_MESSAGES = {
        "tag.create": "没有权限创建标签",
        "tag.edit": "没有权限编辑标签",
        "tag.delete": "没有权限删除标签",
    }

    ACCESS_SCOPE_LABELS = {
        Tag.ACCESS_PUBLIC: "所有人",
        Tag.ACCESS_MEMBERS: "已登录用户",
        Tag.ACCESS_STAFF: "仅管理员",
    }
    ACCESS_SCOPE_LEVELS = {
        Tag.ACCESS_PUBLIC: 0,
        Tag.ACCESS_MEMBERS: 1,
        Tag.ACCESS_STAFF: 2,
    }
    TAG_GLOBAL_POLICY_ABILITIES = {
        "viewForum": "view",
        "startDiscussion": "start_discussion",
    }

    @staticmethod
    def primary_tag_filter(prefix: str = ""):
        return Q(**{f"{prefix}is_primary": True, f"{prefix}parent_id__isnull": True})

    @staticmethod
    def secondary_tag_filter(prefix: str = ""):
        return Q(**{f"{prefix}is_primary": False, f"{prefix}parent_id__isnull": True})

    @staticmethod
    def policy_primary_tag_filter(prefix: str = ""):
        return Q(**{f"{prefix}position__isnull": False})

    @staticmethod
    def policy_secondary_tag_filter(prefix: str = ""):
        return Q(**{f"{prefix}position__isnull": True})

    @staticmethod
    def is_primary_tag(tag: Optional[Tag]) -> bool:
        return TagService.is_primary_root_tag(tag)

    @staticmethod
    def is_primary_root_tag(tag: Optional[Tag]) -> bool:
        return bool(tag and tag.parent_id is None and tag.is_primary)

    @staticmethod
    def is_primary_tree_tag(tag: Optional[Tag]) -> bool:
        return bool(tag and tag.is_primary)

    @staticmethod
    def is_child_tag(tag: Optional[Tag]) -> bool:
        return bool(tag and tag.parent_id is not None)

    @staticmethod
    def structure_order_by(*, include_id: bool = False) -> tuple:
        ordering = (Coalesce("position", Value(2147483647)), "name")
        if include_id:
            return (*ordering, "id")
        return ordering

    @staticmethod
    def child_order_by(*, include_id: bool = False) -> tuple:
        ordering = (Coalesce("position", Value(2147483647)), "name")
        if include_id:
            return (*ordering, "id")
        return ordering

    @staticmethod
    def _next_position(parent_id: Optional[int], *, exclude_tag_id: Optional[int] = None) -> int:
        queryset = Tag.objects.filter(parent_id=parent_id, is_primary=True, position__isnull=False)
        if exclude_tag_id is not None:
            queryset = queryset.exclude(id=exclude_tag_id)
        return (queryset.aggregate(max_position=Max("position")).get("max_position") or 0) + 1

    @staticmethod
    def _normalize_structure_state(tag: Tag) -> None:
        if tag.parent_id is not None:
            tag.is_primary = True
            if tag.position is None:
                tag.position = TagService._next_position(tag.parent_id, exclude_tag_id=tag.id)
            return

        if not tag.is_primary:
            tag.position = None
        elif tag.position is None:
            tag.position = TagService._next_position(None, exclude_tag_id=tag.id)

    @staticmethod
    def _settings_int(settings: dict, key: str, default: int = 0) -> int:
        try:
            return max(0, int(settings.get(key, default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def normalize_access_scope(scope: Optional[str], default: str) -> str:
        normalized = (scope or default).strip() if isinstance(scope, str) else default
        if normalized not in TagService.ACCESS_SCOPE_LABELS:
            raise ValueError("无效的标签访问级别")
        return normalized

    @staticmethod
    def can_manage_tags(user: Optional[Any], permission: str) -> bool:
        return has_runtime_forum_permission(user, permission)

    @staticmethod
    def ensure_can_manage_tags(user: Optional[Any], permission: str, message: Optional[str] = None) -> None:
        if not TagService.can_manage_tags(user, permission):
            raise PermissionDenied(message or TagService.TAG_MANAGEMENT_PERMISSION_MESSAGES.get(permission, "无权限管理标签"))

    @staticmethod
    def has_scope_access(user: Optional[Any], scope: str) -> bool:
        if user and (user.is_staff or user.is_superuser):
            return True
        if scope == Tag.ACCESS_PUBLIC:
            return True
        if scope == Tag.ACCESS_MEMBERS:
            return bool(user and user.is_authenticated)
        if scope == Tag.ACCESS_STAFF:
            return False
        return False

    @staticmethod
    def get_scope_label(scope: str) -> str:
        return TagService.ACCESS_SCOPE_LABELS.get(scope, "未知")

    @staticmethod
    def validate_scope_configuration(
        view_scope: str,
        start_discussion_scope: str,
        reply_scope: str,
    ) -> tuple[str, str, str]:
        normalized_view = TagService.normalize_access_scope(view_scope, Tag.ACCESS_PUBLIC)
        normalized_start = TagService.normalize_access_scope(start_discussion_scope, Tag.ACCESS_MEMBERS)
        normalized_reply = TagService.normalize_access_scope(reply_scope, Tag.ACCESS_MEMBERS)

        if TagService.ACCESS_SCOPE_LEVELS[normalized_start] < TagService.ACCESS_SCOPE_LEVELS[normalized_view]:
            raise ValueError("发帖权限不能比查看权限更宽松")

        if TagService.ACCESS_SCOPE_LEVELS[normalized_reply] < TagService.ACCESS_SCOPE_LEVELS[normalized_view]:
            raise ValueError("回帖权限不能比查看权限更宽松")

        return normalized_view, normalized_start, normalized_reply

    @staticmethod
    def can_view_tag(tag: Tag, user: Optional[Any]) -> bool:
        if tag.parent_id and tag.parent:
            if not TagService.can_view_tag(tag.parent, user):
                return False
        if tag.is_restricted and not TagService.has_restricted_tag_permission(tag, user, "viewForum"):
            return False
        return TagService.has_scope_access(user, tag.view_scope)

    @staticmethod
    def can_start_discussion_in_tag(tag: Tag, user: Optional[Any]) -> bool:
        if tag.parent_id and tag.parent:
            if not TagService.can_start_discussion_in_tag(tag.parent, user):
                return False
        if tag.is_restricted and not TagService.has_restricted_tag_permission(tag, user, "startDiscussion"):
            return False
        return (
            TagService.can_view_tag(tag, user)
            and TagService.has_scope_access(user, tag.start_discussion_scope)
        )

    @staticmethod
    def can_reply_in_tag(tag: Tag, user: Optional[Any]) -> bool:
        if tag.parent_id and tag.parent:
            if not TagService.can_reply_in_tag(tag.parent, user):
                return False
        if tag.is_restricted and not TagService.has_restricted_tag_permission(tag, user, "discussion.reply"):
            return False
        return (
            TagService.can_view_tag(tag, user)
            and TagService.has_scope_access(user, tag.reply_scope)
        )

    @staticmethod
    def can_add_to_discussion(tag: Tag, user: Optional[Any]) -> bool:
        return TagService.can_start_discussion_in_tag(tag, user)

    @staticmethod
    def can_tag_ability(tag: Optional[Tag], user: Optional[Any], ability: str):
        if tag is None or isinstance(tag, type):
            return None

        normalized = str(ability or "").strip()
        if not normalized:
            return None

        if normalized in {"view", "viewForum"}:
            return TagService.can_view_tag(tag, user)
        if normalized in {"start_discussion", "startDiscussion", "add_to_discussion", "addToDiscussion"}:
            return TagService.can_start_discussion_in_tag(tag, user)
        if normalized in {"reply", "discussion.reply"}:
            return TagService.can_reply_in_tag(tag, user)

        permission = TagService.TAG_ABILITY_PERMISSION.get(normalized, normalized)
        return TagService.restricted_tag_ability_decision(tag, user, permission)

    @staticmethod
    def restricted_tag_ability_decision(tag: Tag, user: Optional[Any], permission: str):
        if tag.parent_id:
            parent = getattr(tag, "parent", None)
            if parent is not None:
                parent_decision = TagService.restricted_tag_ability_decision(parent, user, permission)
                if parent_decision is False:
                    return False

        if tag.is_restricted:
            return TagService.has_restricted_tag_permission(tag, user, permission)
        return None

    @staticmethod
    def has_restricted_tag_permission(tag: Tag, user: Optional[Any], ability: str) -> bool:
        if not tag.is_restricted:
            return True
        if user and getattr(user, "is_superuser", False):
            return True
        if not user or not getattr(user, "is_authenticated", False):
            return False
        return has_runtime_forum_permission(user, f"tag{tag.id}.{ability}")

    @staticmethod
    def can_view_discussion_tags(discussion, user: Optional[Any]) -> bool:
        return all(TagService.can_view_tag(tag, user) for tag in get_discussion_tags(discussion))

    @staticmethod
    def can_reply_in_discussion(discussion, user: Optional[Any]) -> bool:
        return all(TagService.can_reply_in_tag(tag, user) for tag in get_discussion_tags(discussion))

    @staticmethod
    def restricted_discussion_ability_decision(
        discussion,
        user: Optional[Any],
        ability: str,
        *,
        deny_without_permission: bool = True,
    ):
        normalized = str(ability or "").strip()
        if not normalized:
            return None
        if discussion is None or isinstance(discussion, type) or getattr(discussion, "pk", None) is None:
            return None

        restricted_tags = [tag for tag in get_discussion_tags(discussion) if tag.is_restricted]
        if not restricted_tags:
            return None

        permission = normalized if normalized.startswith("discussion.") else f"discussion.{normalized}"
        for tag in restricted_tags:
            if not TagService.has_restricted_tag_permission(tag, user, permission):
                return False if deny_without_permission else None
        return True

    @staticmethod
    def can_tag_discussion(discussion, user: Optional[Any]) -> bool:
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_suspended", False):
            return False
        if user.is_staff or has_runtime_forum_permission(user, "discussion.edit"):
            return True
        if getattr(discussion, "user_id", None) != getattr(user, "id", None):
            return False
        if not has_runtime_forum_permission(user, "discussion.reply"):
            return False

        allow_tag_change = TagService.get_allow_tag_change_setting()
        if allow_tag_change == "-1":
            return True
        if allow_tag_change == "reply":
            return getattr(discussion, "participant_count", 0) <= 1
        try:
            allowed_minutes = int(allow_tag_change)
        except (TypeError, ValueError):
            return False
        created_at = getattr(discussion, "created_at", None)
        if created_at is None:
            return False
        return timezone.now() - created_at < timezone.timedelta(minutes=allowed_minutes)

    @staticmethod
    def get_allow_tag_change_setting() -> str:
        value = get_extension_settings("tags").get("allow_tag_change", "reply")
        return str(value if value is not None else "reply").strip()

    @staticmethod
    def delete_tag_permissions(tag: Tag | int) -> int:
        tag_id = int(getattr(tag, "id", tag) or 0)
        if not tag_id:
            return 0
        Permission = get_runtime_permission_model()
        deleted, _ = Permission.objects.filter(permission__startswith=f"tag{tag_id}.").delete()
        return int(deleted or 0)

    @staticmethod
    def state_for_user(tag: Tag, user: Optional[Any]) -> Optional[TagState]:
        if not user or not getattr(user, "is_authenticated", False):
            return None
        prefetched_states = getattr(tag, "actor_states", None)
        if prefetched_states is not None:
            for state in prefetched_states:
                if state.user_id == user.id:
                    return state
            return TagState(tag=tag, user=user)

        state = TagState.objects.filter(tag=tag, user=user).first()
        if state is not None:
            return state
        return TagState(tag=tag, user=user)

    @staticmethod
    def prefetch_state_for_user(queryset: QuerySet, user: Optional[Any]) -> QuerySet:
        if not user or not getattr(user, "is_authenticated", False):
            return queryset
        from django.db.models import Prefetch

        return queryset.prefetch_related(
            Prefetch(
                "user_states",
                queryset=TagState.objects.filter(user=user),
                to_attr="actor_states",
            )
        )

    @staticmethod
    def mark_tag_read(tag: Tag, user: Any, marked_as_read_at=None) -> TagState:
        if not user or not getattr(user, "is_authenticated", False):
            raise PermissionDenied("请先登录")
        state, _ = TagState.objects.get_or_create(tag=tag, user=user)
        state.marked_as_read_at = marked_as_read_at or timezone.now()
        state.save(update_fields=["marked_as_read_at"])
        return state

    @staticmethod
    def global_tag_ability_decision(user: Optional[Any], ability: str):
        normalized = str(ability or "").strip()
        action = TagService.TAG_GLOBAL_POLICY_ABILITIES.get(normalized)
        if not action:
            return None

        settings = get_extension_settings("tags")
        min_primary = TagService._settings_int(settings, "min_primary_tags")
        min_secondary = TagService._settings_int(settings, "min_secondary_tags")

        if normalized == "startDiscussion":
            if has_runtime_forum_permission(user, "startDiscussion") and has_runtime_forum_permission(user, "bypassTagCounts"):
                return True
            if min_primary == 0 and min_secondary == 0:
                return None

        counts = TagService._global_policy_allowed_tag_counts(user, action)
        primary_required = min_primary
        secondary_required = min_secondary
        if normalized == "viewForum":
            primary_required = min(counts["total_primary"], min_primary)
            secondary_required = min(counts["total_secondary"], min_secondary)

        return counts["allowed_primary"] >= primary_required and counts["allowed_secondary"] >= secondary_required

    @staticmethod
    def _global_policy_allowed_tag_counts(user: Optional[Any], action: str) -> dict[str, int]:
        cache_owner = user if user is not None else TagService
        cache = getattr(cache_owner, "_tag_global_policy_counts_cache", None)
        if cache is None:
            cache = {}
            try:
                setattr(cache_owner, "_tag_global_policy_counts_cache", cache)
            except Exception:
                cache = {}
        cache_key = (
            action,
            getattr(user, "id", None),
            bool(getattr(user, "is_authenticated", False)),
            bool(getattr(user, "is_staff", False)),
            bool(getattr(user, "is_superuser", False)),
        )
        if cache_key in cache:
            return dict(cache[cache_key])

        allowed = TagService.filter_tags_for_user(Tag.objects.all(), user, action=action)
        counts = {
            "allowed_primary": allowed.filter(TagService.policy_primary_tag_filter()).count(),
            "allowed_secondary": allowed.filter(TagService.policy_secondary_tag_filter()).count(),
            "total_primary": Tag.objects.filter(TagService.policy_primary_tag_filter()).count(),
            "total_secondary": Tag.objects.filter(TagService.policy_secondary_tag_filter()).count(),
        }
        cache[cache_key] = counts
        return dict(counts)

    @staticmethod
    def filter_tags_for_user(queryset: QuerySet, user: Optional[Any], action: str = "view") -> QuerySet:
        if user and (user.is_staff or user.is_superuser):
            return queryset

        visibility_filter = TagService._tag_visibility_filter(user, action=action)
        if visibility_filter is None:
            return queryset
        return queryset.filter(visibility_filter)

    @staticmethod
    def _tag_visibility_filter(user: Optional[Any], action: str = "view"):
        scope_field = TagService.ACTION_SCOPE_FIELD.get(action)
        if not scope_field:
            return None

        own_visibility = TagService._tag_scope_filter(user, scope_field)
        parent_visibility = Q(parent_id__isnull=True) | TagService._tag_scope_filter(
            user,
            scope_field,
            prefix="parent__",
        )

        restricted_permission = TagService.ACTION_RESTRICTED_PERMISSION.get(action)
        if not restricted_permission:
            return own_visibility & parent_visibility

        allowed_restricted_tag_ids = TagService._restricted_tag_ids_with_permission(
            user,
            restricted_permission,
        )
        own_restricted = TagService._tag_restricted_filter(allowed_restricted_tag_ids)
        parent_restricted = Q(parent_id__isnull=True) | TagService._tag_restricted_filter(
            allowed_restricted_tag_ids,
            prefix="parent__",
        )
        return own_visibility & own_restricted & parent_visibility & parent_restricted

    @staticmethod
    def _tag_scope_filter(user: Optional[Any], scope_field: str, *, prefix: str = ""):
        field = f"{prefix}{scope_field}"
        if user and getattr(user, "is_authenticated", False):
            return Q(**{f"{field}__in": [Tag.ACCESS_PUBLIC, Tag.ACCESS_MEMBERS]})
        return Q(**{field: Tag.ACCESS_PUBLIC})

    @staticmethod
    def _tag_restricted_filter(allowed_restricted_tag_ids: tuple[int, ...], *, prefix: str = ""):
        restricted_field = f"{prefix}is_restricted"
        id_field = f"{prefix}id"
        visibility = Q(**{restricted_field: False})
        if allowed_restricted_tag_ids:
            visibility |= Q(**{f"{id_field}__in": allowed_restricted_tag_ids})
        return visibility

    @staticmethod
    def get_forbidden_tag_ids(user: Optional[Any], action: str = "view") -> List[int]:
        forbidden_tag_ids = TagService._forbidden_tag_ids_queryset(user, action=action)
        if forbidden_tag_ids is None:
            return []
        return list(forbidden_tag_ids.values_list("id", flat=True))

    @staticmethod
    def filter_discussions_for_user(queryset: QuerySet, user: Optional[Any]) -> QuerySet:
        forbidden_tag_ids = TagService._forbidden_tag_ids_queryset(user, action="view")
        if forbidden_tag_ids is None:
            return queryset

        forbidden_discussion_ids = DiscussionTag.objects.filter(
            tag_id__in=forbidden_tag_ids,
        ).values("discussion_id")
        queryset = queryset.exclude(id__in=forbidden_discussion_ids)
        if not TagService._has_global_permission(user, "viewForum"):
            tagged_discussion_ids = DiscussionTag.objects.values("discussion_id")
            queryset = queryset.filter(id__in=tagged_discussion_ids)
        return queryset

    @staticmethod
    def filter_posts_for_user(queryset: QuerySet, user: Optional[Any]) -> QuerySet:
        forbidden_tag_ids = TagService._forbidden_tag_ids_queryset(user, action="view")
        if forbidden_tag_ids is None:
            return queryset

        forbidden_discussion_ids = DiscussionTag.objects.filter(
            tag_id__in=forbidden_tag_ids,
        ).values("discussion_id")
        queryset = queryset.exclude(discussion_id__in=forbidden_discussion_ids)
        if not TagService._has_global_permission(user, "viewForum"):
            tagged_discussion_ids = DiscussionTag.objects.values("discussion_id")
            queryset = queryset.filter(discussion_id__in=tagged_discussion_ids)
        return queryset

    @staticmethod
    def _has_global_permission(user: Optional[Any], ability: str) -> bool:
        if user and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
            return True
        if ability == "viewForum" and (not user or not getattr(user, "is_authenticated", False)):
            return True
        return has_runtime_forum_permission(user, ability)

    @staticmethod
    def _forbidden_tag_ids_queryset(user: Optional[Any], action: str = "view"):
        if user and (user.is_staff or user.is_superuser):
            return None
        visibility_filter = TagService._tag_visibility_filter(user, action=action)
        if visibility_filter is None:
            return None
        return Tag.objects.exclude(visibility_filter).values("id")

    @staticmethod
    def _restricted_tag_ids_with_permission(user: Optional[Any], ability: str) -> tuple[int, ...]:
        if not user or not getattr(user, "is_authenticated", False):
            return ()
        if getattr(user, "is_superuser", False):
            return tuple(Tag.objects.filter(is_restricted=True).values_list("id", flat=True))

        suffix = f".{ability}"
        tag_ids: list[int] = []
        for permission in get_runtime_forum_permissions(user):
            if not permission.startswith("tag") or not permission.endswith(suffix):
                continue
            raw_tag_id = permission[3:-len(suffix)]
            try:
                tag_id = int(raw_tag_id)
            except (TypeError, ValueError):
                continue
            if tag_id > 0:
                tag_ids.append(tag_id)
        return tuple(sorted(set(tag_ids)))

    @staticmethod
    def validate_tag_selection(tag_ids: Optional[List[int]]) -> List[int]:
        if not tag_ids:
            return []
        normalized = []
        for tag_id in tag_ids:
            if not tag_id:
                continue
            if int(tag_id) not in normalized:
                normalized.append(int(tag_id))
        return normalized

    @staticmethod
    def get_tags_for_selection(tag_ids: Optional[List[int]]) -> List[Tag]:
        normalized_ids = TagService.validate_tag_selection(tag_ids)
        if not normalized_ids:
            return []

        tags_by_id = {
            tag.id: tag
            for tag in Tag.objects.filter(id__in=normalized_ids).select_related("parent")
        }
        if len(tags_by_id) != len(normalized_ids):
            raise ValueError("部分标签不存在")

        tags = [tags_by_id[tag_id] for tag_id in normalized_ids]
        primary_tags = [tag for tag in tags if TagService.is_primary_tag(tag)]
        secondary_tags = [tag for tag in tags if not TagService.is_primary_tag(tag)]
        child_tags = [tag for tag in secondary_tags if tag.parent_id is not None]

        primary_tag_ids = {tag.id for tag in primary_tags}
        missing_parent_names = [
            child.name
            for child in child_tags
            if child.parent_id not in primary_tag_ids
        ]
        if missing_parent_names:
            raise ValueError("选择次标签时必须同时选择对应的主标签")

        return primary_tags + secondary_tags

    @staticmethod
    def validate_tag_count_limits(tags: List[Tag], user: Any = None) -> None:
        if has_runtime_forum_permission(user, "bypassTagCounts"):
            return

        settings = get_extension_settings("tags")
        min_primary = TagService._settings_int(settings, "min_primary_tags")
        max_primary = TagService._settings_int(settings, "max_primary_tags", default=1)
        min_secondary = TagService._settings_int(settings, "min_secondary_tags")
        max_secondary = TagService._settings_int(settings, "max_secondary_tags", default=1)
        primary_count = sum(1 for tag in tags if TagService.is_primary_tag(tag))
        secondary_count = len(tags) - primary_count

        TagService._validate_tag_count("主标签", primary_count, min_primary, max_primary)
        TagService._validate_tag_count("次标签", secondary_count, min_secondary, max_secondary)

    @staticmethod
    def _validate_tag_count(label: str, count: int, minimum: int, maximum: int) -> None:
        if count < minimum:
            raise ValueError(f"当前至少需要选择 {minimum} 个{label}")
        if count > maximum:
            raise ValueError(f"当前最多只能选择 {maximum} 个{label}")

    @staticmethod
    def normalize_tag_slug(name: str, slug: Optional[str] = None, *, exclude_tag_id: Optional[int] = None) -> str:
        TagService.validate_slug_value(slug)
        runtime_slug = generate_runtime_model_slug(
            Tag,
            name,
            explicit_slug=slug or "",
            exclude_id=exclude_tag_id,
            context={"source": "tags"},
        )
        if runtime_slug:
            return runtime_slug

        normalized_slug = (slug or "").strip()
        if not normalized_slug:
            normalized_slug = slugify(name, allow_unicode=True)
        if not normalized_slug:
            normalized_slug = str(uuid.uuid4())[:8]

        original_slug = normalized_slug
        counter = 1
        while True:
            queryset = Tag.objects.filter(slug=normalized_slug)
            if exclude_tag_id is not None:
                queryset = queryset.exclude(pk=exclude_tag_id)
            if not queryset.exists():
                return normalized_slug
            normalized_slug = f"{original_slug}-{counter}"
            counter += 1

    @staticmethod
    def validate_slug_value(slug: Optional[str]) -> None:
        if slug is None:
            return
        normalized = str(slug or "").strip()
        if "/" in normalized or any(item.isspace() for item in normalized):
            raise ValueError("标签 slug 不能包含斜杠或空白字符")

    @staticmethod
    def validate_description_value(description: Optional[str]) -> str:
        normalized = description or ""
        if len(normalized) > 700:
            raise ValueError("标签描述不能超过 700 个字符")
        return normalized

    @staticmethod
    def normalize_default_sort(value: Optional[str]) -> Optional[str]:
        normalized = str(value or "").strip()
        return normalized or None

    @staticmethod
    def to_tag_slug(tag: Tag, *, driver: str | None = None) -> str:
        runtime_slug = to_runtime_model_slug(Tag, tag, identifier=driver)
        if runtime_slug:
            return runtime_slug
        return str(getattr(tag, "slug", "") or "").strip()

    @staticmethod
    def ensure_can_start_discussion(user: Any, tag_ids: Optional[List[int]]) -> List[Tag]:
        tags = TagService.get_tags_for_selection(tag_ids)
        TagService.validate_tag_count_limits(tags, user=user)

        for tag in tags:
            if not TagService.can_start_discussion_in_tag(tag, user):
                raise PermissionDenied(f"没有权限在标签“{tag.name}”下发起讨论")

        return tags

    @staticmethod
    def ensure_can_change_discussion_tags(
        user: Any,
        discussion,
        tag_ids: Optional[List[int]],
        *,
        existing_tag_ids: Optional[List[int] | tuple[int, ...] | set[int]] = None,
    ) -> List[Tag]:
        tags = TagService.get_tags_for_selection(tag_ids)
        if not TagService.can_tag_discussion(discussion, user):
            raise PermissionDenied("没有权限修改此讨论的标签")
        TagService.validate_tag_count_limits(tags, user=user)

        old_tag_ids = (
            set(get_discussion_tag_ids_for_stats(discussion))
            if existing_tag_ids is None
            else {int(tag_id) for tag_id in existing_tag_ids if tag_id}
        )
        for tag in tags:
            if tag.id not in old_tag_ids and not TagService.can_add_to_discussion(tag, user):
                raise PermissionDenied(f"没有权限将标签“{tag.name}”添加到讨论")

        return tags

    @staticmethod
    def ensure_can_reply_in_discussion(user: Any, discussion) -> None:
        for tag in get_discussion_tags(discussion):
            if not TagService.can_reply_in_tag(tag, user):
                raise PermissionDenied(f"没有权限在标签“{tag.name}”下回复讨论")

    @staticmethod
    def validate_parent_assignment(tag: Optional[Tag], parent: Optional[Tag]) -> None:
        if parent is None:
            return

        if parent.parent_id is not None:
            raise ValueError("只能选择顶级标签作为父标签")

        if not TagService.is_primary_root_tag(parent):
            raise ValueError("只能选择主标签作为父标签")

        if tag is not None:
            if tag.id == parent.id:
                raise ValueError("标签不能设置自己为父标签")
            if TagService._would_create_cycle(tag, parent):
                raise ValueError("不能设置子标签为父标签（会形成循环）")
            if tag.children.exists() and parent is not None:
                raise ValueError("已有子标签的标签不能再设置为子标签")

    @staticmethod
    def create_tag(
        name: str,
        slug: Optional[str] = None,
        description: str = "",
        color: str = "",
        icon: str = "",
        background_url: str = "",
        position: Optional[int] = 0,
        default_sort: Optional[str] = None,
        is_primary: Optional[bool] = True,
        parent_id: Optional[int] = None,
        is_hidden: bool = False,
        is_restricted: bool = False,
        view_scope: str = Tag.ACCESS_PUBLIC,
        start_discussion_scope: str = Tag.ACCESS_MEMBERS,
        reply_scope: str = Tag.ACCESS_MEMBERS,
        user: Optional[Any] = None,
    ) -> Tag:
        """
        创建标签

        Args:
            name: 标签名称
            slug: 标签slug（可选）
            description: 描述
            color: 颜色
            icon: 图标
            background_url: 背景图片
            position: 排序位置
            parent_id: 父标签ID
            is_hidden: 是否隐藏
            is_restricted: 是否限制发帖
            user: 操作用户

        Returns:
            Tag: 创建的标签对象

        Raises:
            PermissionDenied: 权限不足
            ValueError: 参数错误
        """
        TagService.ensure_can_manage_tags(user, "tag.create")

        # 检查父标签
        parent = None
        if parent_id:
            try:
                parent = Tag.objects.get(id=parent_id)
            except Tag.DoesNotExist:
                raise ValueError("父标签不存在")
            TagService.validate_parent_assignment(None, parent)

        normalized_view, normalized_start, normalized_reply = TagService.validate_scope_configuration(
            view_scope,
            start_discussion_scope,
            reply_scope,
        )

        with transaction.atomic():
            if parent is not None:
                normalized_is_primary = True
                normalized_position = position
                if normalized_position is None:
                    normalized_position = TagService._next_position(parent.id)
            elif is_primary is False:
                normalized_is_primary = False
                normalized_position = None
            elif position is None:
                normalized_is_primary = True
                normalized_position = TagService._next_position(None)
            else:
                normalized_is_primary = True
                normalized_position = position

            tag = Tag.objects.create(
                name=name,
                slug=TagService.normalize_tag_slug(name, slug),
                description=TagService.validate_description_value(description),
                color=color,
                icon=icon,
                background_url=background_url,
                position=normalized_position,
                default_sort=TagService.normalize_default_sort(default_sort),
                is_primary=normalized_is_primary,
                parent=parent,
                is_hidden=is_hidden,
                is_restricted=is_restricted,
                view_scope=normalized_view,
                start_discussion_scope=normalized_start,
                reply_scope=normalized_reply,
            )
            return tag

    @staticmethod
    def get_tag_list(
        parent_id: Optional[int] = None,
        include_hidden: bool = False,
    ) -> List[Tag]:
        """
        获取标签列表

        Args:
            parent_id: 父标签ID（None表示顶级标签）
            include_hidden: 是否包含隐藏标签

        Returns:
            List[Tag]: 标签列表
        """
        child_queryset = Tag.objects.all()
        if not include_hidden:
            child_queryset = child_queryset.filter(is_hidden=False)
        child_queryset = child_queryset.order_by(*TagService.child_order_by())

        queryset = Tag.objects.prefetch_related(
            Prefetch("children", queryset=child_queryset, to_attr="_children_list")
        )
        if parent_id is None:
            queryset = queryset.filter(parent__isnull=True)
        else:
            queryset = queryset.filter(parent_id=parent_id)

        if not include_hidden:
            queryset = queryset.filter(is_hidden=False)

        queryset = queryset.order_by(*TagService.structure_order_by())

        return list(queryset)

    @staticmethod
    def get_tag_by_id(tag_id: int) -> Optional[Tag]:
        """
        获取标签详情

        Args:
            tag_id: 标签ID

        Returns:
            Optional[Tag]: 标签对象
        """
        try:
            return Tag.objects.get(id=tag_id)
        except Tag.DoesNotExist:
            return None

    @staticmethod
    def get_tag_by_slug(slug: str) -> Optional[Tag]:
        """
        通过slug获取标签

        Args:
            slug: 标签slug

        Returns:
            Optional[Tag]: 标签对象
        """
        runtime_tag = resolve_runtime_model_slug(Tag, slug, identifier="default")
        if runtime_tag is not None:
            return runtime_tag

        try:
            return Tag.objects.get(slug__iexact=slug)
        except Tag.DoesNotExist:
            return None

    @staticmethod
    def get_tag_by_url_slug(slug: str, *, driver: str = "default") -> Optional[Tag]:
        runtime_tag = resolve_runtime_model_slug(Tag, slug, identifier=driver)
        if runtime_tag is not None:
            return runtime_tag
        if driver == "default":
            return TagService.get_tag_by_slug(slug)
        return None

    @staticmethod
    def update_tag(
        tag_id: int,
        user: Any,
        name: Optional[str] = None,
        slug: Optional[str] = None,
        description: Optional[str] = None,
        color: Optional[str] = None,
        icon: Optional[str] = None,
        background_url: Optional[str] = None,
        position: Optional[int] = None,
        default_sort: Optional[str] = None,
        is_primary: Optional[bool] = None,
        parent_id: Any = _UNSET,
        is_hidden: Optional[bool] = None,
        is_restricted: Optional[bool] = None,
        view_scope: Optional[str] = None,
        start_discussion_scope: Optional[str] = None,
        reply_scope: Optional[str] = None,
    ) -> Tag:
        """
        更新标签

        Args:
            tag_id: 标签ID
            user: 操作用户
            其他参数: 要更新的字段

        Returns:
            Tag: 更新后的标签对象

        Raises:
            PermissionDenied: 权限不足
            ValueError: 参数错误
        """
        TagService.ensure_can_manage_tags(user, "tag.edit")

        tag = Tag.objects.get(id=tag_id)
        was_restricted = bool(tag.is_restricted)
        next_view_scope = tag.view_scope
        next_start_scope = tag.start_discussion_scope
        next_reply_scope = tag.reply_scope

        with transaction.atomic():
            if name is not None:
                tag.name = name

            if slug is not None:
                tag.slug = slug

            if description is not None:
                tag.description = TagService.validate_description_value(description)

            if color is not None:
                tag.color = color

            if icon is not None:
                tag.icon = icon

            if background_url is not None:
                tag.background_url = background_url

            if default_sort is not None:
                tag.default_sort = TagService.normalize_default_sort(default_sort)

            if parent_id is not _UNSET:
                if parent_id in ("", 0, "0", None):
                    tag.parent = None
                else:
                    try:
                        parent = Tag.objects.get(id=parent_id)
                    except Tag.DoesNotExist:
                        raise ValueError("父标签不存在")
                    TagService.validate_parent_assignment(tag, parent)
                    tag.parent = parent

            if is_primary is not None:
                if tag.parent_id is not None:
                    if not is_primary:
                        raise ValueError("子标签不能设置为次级标签")
                    tag.is_primary = True
                    if position is None and tag.position is None:
                        tag.position = TagService._next_position(tag.parent_id, exclude_tag_id=tag.id)
                elif is_primary:
                    tag.is_primary = True
                    if position is None and tag.position is None:
                        tag.position = TagService._next_position(None, exclude_tag_id=tag.id)
                else:
                    if tag.children.exists():
                        raise ValueError("已有子标签的标签不能设置为次级标签")
                    tag.is_primary = False
                    tag.position = None

            if position is not None:
                tag.position = position
                tag.is_primary = True
            elif parent_id is not _UNSET and tag.parent_id is not None and tag.position is None:
                tag.position = TagService._next_position(tag.parent_id, exclude_tag_id=tag.id)
                tag.is_primary = True
            elif parent_id is not _UNSET and tag.parent_id is None and tag.position is None and is_primary is None:
                tag.is_primary = False

            TagService._normalize_structure_state(tag)

            if is_hidden is not None:
                tag.is_hidden = is_hidden

            if is_restricted is not None:
                tag.is_restricted = is_restricted

            if view_scope is not None:
                next_view_scope = view_scope

            if start_discussion_scope is not None:
                next_start_scope = start_discussion_scope

            if reply_scope is not None:
                next_reply_scope = reply_scope

            (
                tag.view_scope,
                tag.start_discussion_scope,
                tag.reply_scope,
            ) = TagService.validate_scope_configuration(
                next_view_scope,
                next_start_scope,
                next_reply_scope,
            )

            if slug is not None:
                tag.slug = TagService.normalize_tag_slug(tag.name, slug, exclude_tag_id=tag.id)
            elif not tag.slug:
                tag.slug = TagService.normalize_tag_slug(tag.name, tag.slug, exclude_tag_id=tag.id)

            tag.save()
            if was_restricted and not tag.is_restricted:
                TagService.delete_tag_permissions(tag)
            return tag

    @staticmethod
    def move_tag(tag_id: int, direction: str, user: Any) -> bool:
        TagService.ensure_can_manage_tags(user, "tag.edit", "没有权限调整标签排序")

        if direction not in {"up", "down"}:
            raise ValueError("无效的排序方向")

        tag = Tag.objects.get(id=tag_id)

        with transaction.atomic():
            siblings = list(
                Tag.objects.filter(parent_id=tag.parent_id, is_primary=True, position__isnull=False).order_by(
                    *TagService.structure_order_by(include_id=True)
                )
            )
            current_index = next(
                (index for index, sibling in enumerate(siblings) if sibling.id == tag.id),
                None,
            )
            if current_index is None:
                raise ValueError("标签不存在于当前层级")

            target_index = current_index - 1 if direction == "up" else current_index + 1
            if target_index < 0 or target_index >= len(siblings):
                return False

            siblings[current_index], siblings[target_index] = siblings[target_index], siblings[current_index]

            for index, sibling in enumerate(siblings):
                sibling.position = index

            Tag.objects.bulk_update(siblings, ["position"])

        return True

    @staticmethod
    def order_tags(order: List[dict], user: Any) -> List[Tag]:
        TagService.ensure_can_manage_tags(user, "tag.edit", "没有权限调整标签排序")
        if not isinstance(order, list):
            raise ValueError("标签排序数据必须是数组")

        flattened: list[tuple[int, Optional[int], int]] = []
        seen_ids: set[int] = set()

        for parent_index, item in enumerate(order):
            if not isinstance(item, dict):
                raise ValueError("标签排序项格式错误")
            parent_id = TagService._normalize_order_tag_id(item.get("id"))
            if parent_id is None:
                raise ValueError("标签排序项缺少标签 ID")
            if parent_id in seen_ids:
                raise ValueError("标签排序不能包含重复标签")
            seen_ids.add(parent_id)
            flattened.append((parent_id, None, parent_index))

            children = item.get("children", [])
            if children is None:
                children = []
            if not isinstance(children, list):
                raise ValueError("子标签排序数据必须是数组")
            for child_index, child in enumerate(children):
                child_id = TagService._normalize_order_tag_id(child)
                if child_id is None:
                    raise ValueError("子标签排序项缺少标签 ID")
                if child_id == parent_id:
                    raise ValueError("标签不能成为自己的子标签")
                if child_id in seen_ids:
                    raise ValueError("标签排序不能包含重复标签")
                seen_ids.add(child_id)
                flattened.append((child_id, parent_id, child_index))

        if not flattened:
            return list(Tag.objects.select_related("parent").all().order_by(*TagService.structure_order_by(include_id=True)))

        tags_by_id = {
            tag.id: tag
            for tag in Tag.objects.select_related("parent").filter(id__in=seen_ids)
        }
        missing_ids = sorted(seen_ids - set(tags_by_id))
        if missing_ids:
            raise ValueError(f"标签不存在: {', '.join(str(item) for item in missing_ids)}")

        for tag_id, parent_id, _position in flattened:
            tag = tags_by_id[tag_id]
            parent = tags_by_id[parent_id] if parent_id is not None else None
            if parent is not None:
                TagService.validate_parent_assignment(tag, parent)

        with transaction.atomic():
            Tag.objects.update(parent=None, position=None, is_primary=False)
            for tag_id, parent_id, position in flattened:
                tag = tags_by_id[tag_id]
                parent = tags_by_id[parent_id] if parent_id is not None else None
                tag.parent = parent
                tag.position = position
                tag.is_primary = True

            Tag.objects.bulk_update(tags_by_id.values(), ["parent_id", "position", "is_primary"])

        return list(Tag.objects.select_related("parent").all().order_by(*TagService.structure_order_by(include_id=True)))

    @staticmethod
    def _normalize_order_tag_id(value) -> Optional[int]:
        if isinstance(value, dict):
            value = value.get("id")
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return None
        return normalized if normalized > 0 else None

    @staticmethod
    def _compact_positions_for_all_levels() -> None:
        parent_ids = [None, *Tag.objects.exclude(parent_id__isnull=True).values_list("parent_id", flat=True).distinct()]
        for parent_id in parent_ids:
            siblings = list(
                Tag.objects.filter(parent_id=parent_id, is_primary=True, position__isnull=False).order_by(
                    *TagService.structure_order_by(include_id=True)
                )
            )
            for index, sibling in enumerate(siblings):
                sibling.position = index
            if siblings:
                Tag.objects.bulk_update(siblings, ["position"])

    @staticmethod
    def _would_create_cycle(tag: Tag, new_parent: Tag) -> bool:
        """
        检查设置新父标签是否会形成循环

        Args:
            tag: 当前标签
            new_parent: 新的父标签

        Returns:
            bool: 是否会形成循环
        """
        current = new_parent
        while current:
            if current.id == tag.id:
                return True
            current = current.parent
        return False

    @staticmethod
    def delete_tag(tag_id: int, user: Any) -> bool:
        """
        删除标签

        Args:
            tag_id: 标签ID
            user: 操作用户

        Returns:
            bool: 是否删除成功

        Raises:
            PermissionDenied: 权限不足
            ValueError: 参数错误
        """
        TagService.ensure_can_manage_tags(user, "tag.delete")

        tag = Tag.objects.get(id=tag_id)

        # 检查是否有子标签
        if Tag.objects.filter(parent=tag).exists():
            raise ValueError("该标签下还有子标签，请先删除或移动子标签")

        # 检查是否有讨论使用该标签
        if tag_has_discussions(tag):
            raise ValueError("该标签下还有讨论，无法删除")

        with transaction.atomic():
            TagService.delete_tag_permissions(tag)
            tag.delete()

        return True

    @staticmethod
    def get_popular_tags(limit: int = 10) -> List[Tag]:
        """
        获取热门标签

        Args:
            limit: 返回数量

        Returns:
            List[Tag]: 热门标签列表
        """
        tags = Tag.objects.filter(
            is_hidden=False
        ).order_by('-discussion_count', '-last_posted_at')[:limit]

        return list(tags)

    @staticmethod
    def refresh_discussion_tag_stats(discussion) -> None:
        discussion_id = getattr(discussion, "id", discussion)
        tag_ids = get_discussion_tag_ids_for_stats(discussion_id)
        if tag_ids:
            TagService.dispatch_refresh_tag_stats(tag_ids)

    @staticmethod
    def increment_tag_stats_for_discussion(discussion, tag_ids: List[int] | tuple[int, ...]) -> int:
        """
        Increment tag metadata for a newly counted discussion.

        Creation can update affected tag counters directly. Moderation and tag
        changes that may invalidate an existing latest discussion still use
        refresh_tag_stats.
        """
        normalized_tag_ids = sorted({int(tag_id) for tag_id in (tag_ids or []) if tag_id})
        if not normalized_tag_ids:
            return 0
        if getattr(discussion, "hidden_at", None) is not None:
            return 0
        approval_status = getattr(discussion, "approval_status", "")
        approved_status = getattr(discussion.__class__, "APPROVAL_APPROVED", "approved")
        if approval_status != approved_status:
            return 0
        if getattr(discussion, "is_private", False):
            return 0

        Tag.objects.filter(id__in=normalized_tag_ids).update(
            discussion_count=F("discussion_count") + 1,
        )

        return TagService.update_tag_latest_discussion(discussion, normalized_tag_ids)

    @staticmethod
    def update_tag_latest_discussion(discussion, tag_ids: List[int] | tuple[int, ...]) -> int:
        normalized_tag_ids = sorted({int(tag_id) for tag_id in (tag_ids or []) if tag_id})
        if not normalized_tag_ids:
            return 0
        if getattr(discussion, "hidden_at", None) is not None:
            return 0
        approval_status = getattr(discussion, "approval_status", "")
        approved_status = getattr(discussion.__class__, "APPROVAL_APPROVED", "approved")
        if approval_status != approved_status:
            return 0
        if getattr(discussion, "is_private", False):
            return 0

        latest_candidate = Q(last_posted_at__isnull=True)
        if getattr(discussion, "last_posted_at", None) is not None:
            latest_candidate |= Q(last_posted_at__lte=discussion.last_posted_at)

        return Tag.objects.filter(
            Q(id__in=normalized_tag_ids) & latest_candidate
        ).update(
            last_posted_at=discussion.last_posted_at,
            last_posted_discussion_id=discussion.id,
        )

    @staticmethod
    def adjust_tag_stats_for_discussion_visibility(
        discussion,
        tag_ids: List[int] | tuple[int, ...],
        *,
        is_hidden: bool,
    ) -> None:
        normalized_tag_ids = sorted({int(tag_id) for tag_id in (tag_ids or []) if tag_id})
        if not normalized_tag_ids:
            return

        if not is_hidden:
            TagService.increment_tag_stats_for_discussion(discussion, normalized_tag_ids)
            return

        Tag.objects.filter(id__in=normalized_tag_ids).update(
            discussion_count=F("discussion_count") - 1,
        )
        latest_tag_ids = list(
            Tag.objects.filter(
                id__in=normalized_tag_ids,
                last_posted_discussion_id=discussion.id,
            ).values_list("id", flat=True)
        )
        if latest_tag_ids:
            TagService.refresh_tag_stats(latest_tag_ids)

    @staticmethod
    def adjust_tag_stats_for_discussion_tag_change(
        discussion,
        *,
        added_tag_ids: List[int] | tuple[int, ...],
        removed_tag_ids: List[int] | tuple[int, ...],
    ) -> None:
        removed_ids = sorted({int(tag_id) for tag_id in (removed_tag_ids or []) if tag_id})
        added_ids = sorted({int(tag_id) for tag_id in (added_tag_ids or []) if tag_id})

        if removed_ids:
            Tag.objects.filter(id__in=removed_ids).update(
                discussion_count=F("discussion_count") - 1,
            )
            latest_removed_ids = list(
                Tag.objects.filter(
                    id__in=removed_ids,
                    last_posted_discussion_id=discussion.id,
                ).values_list("id", flat=True)
            )
            if latest_removed_ids:
                TagService.refresh_tag_stats(latest_removed_ids)

        if added_ids:
            TagService.increment_tag_stats_for_discussion(discussion, added_ids)

    @staticmethod
    def tag_ids_where_discussion_is_latest(discussion, tag_ids: List[int] | tuple[int, ...]) -> tuple[int, ...]:
        normalized_tag_ids = sorted({int(tag_id) for tag_id in (tag_ids or []) if tag_id})
        if not normalized_tag_ids:
            return ()
        return tuple(
            Tag.objects.filter(
                id__in=normalized_tag_ids,
                last_posted_discussion_id=getattr(discussion, "id", discussion),
            ).values_list("id", flat=True)
        )

    @staticmethod
    def adjust_tag_stats_for_deleted_discussion(
        tag_ids: List[int] | tuple[int, ...],
        *,
        latest_tag_ids: List[int] | tuple[int, ...] = (),
    ) -> None:
        normalized_tag_ids = sorted({int(tag_id) for tag_id in (tag_ids or []) if tag_id})
        if not normalized_tag_ids:
            return

        Tag.objects.filter(id__in=normalized_tag_ids).update(
            discussion_count=F("discussion_count") - 1,
        )
        latest_ids = sorted({int(tag_id) for tag_id in (latest_tag_ids or []) if tag_id})
        if latest_ids:
            TagService.refresh_tag_stats(latest_ids)

    @staticmethod
    def dispatch_refresh_tag_stats(tag_ids: Optional[List[int]] = None) -> dict:
        normalized_tag_ids = None
        if tag_ids is not None:
            normalized_tag_ids = sorted({int(tag_id) for tag_id in tag_ids if tag_id})
            if not normalized_tag_ids:
                return {"mode": "skipped", "tag_ids": [], "message": "没有需要刷新的标签"}

        from bias_core.extensions.platform import QueueService
        from bias_ext_tags.backend.tasks import refresh_tag_stats_task

        def fallback():
            TagService.refresh_tag_stats(normalized_tag_ids)
            return {
                "mode": "sync",
                "tag_ids": normalized_tag_ids,
                "message": "标签统计已同步刷新",
            }

        if QueueService.should_enqueue():
            def enqueue():
                QueueService.dispatch_celery_task(
                    refresh_tag_stats_task,
                    normalized_tag_ids,
                    fallback=fallback,
                )

            transaction.on_commit(enqueue)
            return {
                "mode": "queued",
                "tag_ids": normalized_tag_ids,
                "message": "标签统计刷新任务已入队",
            }

        return QueueService.dispatch_celery_task(
            refresh_tag_stats_task,
            normalized_tag_ids,
            fallback=fallback,
        )

    @staticmethod
    def refresh_tag_stats(tag_ids: Optional[List[int]] = None) -> None:
        """
        重新计算标签讨论数和最后发帖讨论。

        用于修复历史数据，也用于讨论创建、隐藏、删除后的统计同步。
        """
        queryset = Tag.objects.all()
        if tag_ids is not None:
            queryset = queryset.filter(id__in=tag_ids)

        tags = list(queryset)
        if not tags:
            return

        target_tag_ids = [tag.id for tag in tags]
        counted_links = apply_runtime_counted_discussion_filter(
            DiscussionTag.objects.filter(tag_id__in=target_tag_ids),
            prefix="discussion",
        )
        discussion_counts = {
            item["tag_id"]: item["discussion_count"]
            for item in counted_links.values("tag_id").annotate(discussion_count=Count("discussion_id"))
        }
        latest_discussions = {
            item["tag_id"]: item
            for item in counted_links.annotate(
                row_number=Window(
                    expression=RowNumber(),
                    partition_by=[F("tag_id")],
                    order_by=[
                        F("discussion__last_posted_at").desc(nulls_last=True),
                        F("discussion_id").desc(),
                    ],
                )
            ).filter(row_number=1).values(
                "tag_id",
                "discussion_id",
                "discussion__last_posted_at",
            )
        }

        for tag in tags:
            latest_discussion = latest_discussions.get(tag.id)
            tag.discussion_count = int(discussion_counts.get(tag.id) or 0)
            tag.last_posted_at = latest_discussion["discussion__last_posted_at"] if latest_discussion else None
            tag.last_posted_discussion_id = latest_discussion["discussion_id"] if latest_discussion else None

        Tag.objects.bulk_update(
            tags,
            ["discussion_count", "last_posted_at", "last_posted_discussion"],
        )

