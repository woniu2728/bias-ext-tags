import uuid
from typing import Any, Optional, List
from django.db import transaction
from django.db.models import Q, QuerySet
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
        "reply": "reply_scope",
    }
    ACTION_RESTRICTED_PERMISSION = {
        "view": "viewForum",
        "start_discussion": "startDiscussion",
        "reply": "discussion.reply",
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

    @staticmethod
    def normalize_access_scope(scope: Optional[str], default: str) -> str:
        normalized = (scope or default).strip() if isinstance(scope, str) else default
        if normalized not in TagService.ACCESS_SCOPE_LABELS:
            raise ValueError("无效的标签访问级别")
        return normalized

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
    def filter_tags_for_user(queryset: QuerySet, user: Optional[Any], action: str = "view") -> QuerySet:
        if user and (user.is_staff or user.is_superuser):
            return queryset

        scope_field = TagService.ACTION_SCOPE_FIELD.get(action)
        if not scope_field:
            return queryset

        if user and user.is_authenticated:
            queryset = queryset.exclude(**{scope_field: Tag.ACCESS_STAFF})
        else:
            queryset = queryset.exclude(**{f"{scope_field}__in": [Tag.ACCESS_MEMBERS, Tag.ACCESS_STAFF]})

        restricted_permission = TagService.ACTION_RESTRICTED_PERMISSION.get(action)
        if not restricted_permission:
            return queryset

        allowed_restricted_tag_ids = TagService._restricted_tag_ids_with_permission(
            user,
            restricted_permission,
        )
        return queryset.filter(Q(is_restricted=False) | Q(id__in=allowed_restricted_tag_ids))

    @staticmethod
    def get_forbidden_tag_ids(user: Optional[Any], action: str = "view") -> List[int]:
        allowed_tag_ids = TagService.filter_tags_for_user(
            Tag.objects.all(),
            user,
            action=action,
        ).values_list("id", flat=True)
        return list(Tag.objects.exclude(id__in=allowed_tag_ids).values_list("id", flat=True))

    @staticmethod
    def filter_discussions_for_user(queryset: QuerySet, user: Optional[Any]) -> QuerySet:
        forbidden_tag_ids = TagService._forbidden_tag_ids_queryset(user, action="view")
        if forbidden_tag_ids is None:
            return queryset

        queryset = queryset.exclude(discussion_tags__tag_id__in=forbidden_tag_ids)
        if not TagService._has_global_permission(user, "viewForum"):
            queryset = queryset.filter(discussion_tags__isnull=False)
        return queryset

    @staticmethod
    def filter_posts_for_user(queryset: QuerySet, user: Optional[Any]) -> QuerySet:
        forbidden_tag_ids = TagService._forbidden_tag_ids_queryset(user, action="view")
        if forbidden_tag_ids is None:
            return queryset

        queryset = queryset.exclude(discussion__discussion_tags__tag_id__in=forbidden_tag_ids)
        if not TagService._has_global_permission(user, "viewForum"):
            queryset = queryset.filter(discussion__discussion_tags__isnull=False)
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
        allowed_tag_ids = TagService.filter_tags_for_user(
            Tag.objects.all(),
            user,
            action=action,
        ).values("id")
        return Tag.objects.exclude(id__in=allowed_tag_ids).values("id")

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
        primary_tags = [tag for tag in tags if tag.parent_id is None]
        secondary_tags = [tag for tag in tags if tag.parent_id is not None]

        if len(primary_tags) > 1:
            raise ValueError("当前最多只能选择 1 个主标签")

        if len(secondary_tags) > 1:
            raise ValueError("当前最多只能选择 1 个次标签")

        if secondary_tags and not primary_tags:
            raise ValueError("选择次标签时必须同时选择对应的主标签")

        if primary_tags and secondary_tags and secondary_tags[0].parent_id != primary_tags[0].id:
            raise ValueError("次标签必须与对应的主标签一起选择")

        if len(tags) > 2:
            raise ValueError("当前最多只能选择 2 个标签")

        return primary_tags + secondary_tags

    @staticmethod
    def normalize_tag_slug(name: str, slug: Optional[str] = None, *, exclude_tag_id: Optional[int] = None) -> str:
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
    def to_tag_slug(tag: Tag, *, driver: str = "default") -> str:
        runtime_slug = to_runtime_model_slug(Tag, tag, identifier=driver)
        if runtime_slug:
            return runtime_slug
        return str(getattr(tag, "slug", "") or "").strip()

    @staticmethod
    def ensure_can_start_discussion(user: Any, tag_ids: Optional[List[int]]) -> List[Tag]:
        tags = TagService.get_tags_for_selection(tag_ids)

        for tag in tags:
            if not TagService.can_start_discussion_in_tag(tag, user):
                raise PermissionDenied(f"没有权限在标签“{tag.name}”下发起讨论")

        return tags

    @staticmethod
    def ensure_can_change_discussion_tags(user: Any, discussion, tag_ids: Optional[List[int]]) -> List[Tag]:
        tags = TagService.get_tags_for_selection(tag_ids)
        if not TagService.can_tag_discussion(discussion, user):
            raise PermissionDenied("没有权限修改此讨论的标签")

        old_tag_ids = set(get_discussion_tag_ids_for_stats(discussion))
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
        position: int = 0,
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
        # 权限检查
        if user and not user.is_staff:
            raise PermissionDenied("只有管理员可以创建标签")

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
            tag = Tag.objects.create(
                name=name,
                slug=TagService.normalize_tag_slug(name, slug),
                description=description,
                color=color,
                icon=icon,
                background_url=background_url,
                position=position,
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
        queryset = Tag.objects.all()

        # 过滤父标签
        if parent_id is None:
            queryset = queryset.filter(parent__isnull=True)
        else:
            queryset = queryset.filter(parent_id=parent_id)

        # 过滤隐藏标签
        if not include_hidden:
            queryset = queryset.filter(is_hidden=False)

        # 排序
        queryset = queryset.order_by('position', 'name')

        tags = list(queryset)

        # 递归加载子标签（使用临时属性）
        for tag in tags:
            tag._children_list = TagService._get_children(tag.id, include_hidden)

        return tags

    @staticmethod
    def _get_children(parent_id: int, include_hidden: bool = False) -> List[Tag]:
        """
        递归获取子标签

        Args:
            parent_id: 父标签ID
            include_hidden: 是否包含隐藏标签

        Returns:
            List[Tag]: 子标签列表
        """
        queryset = Tag.objects.filter(parent_id=parent_id)

        if not include_hidden:
            queryset = queryset.filter(is_hidden=False)

        queryset = queryset.order_by('position', 'name')
        children = list(queryset)

        # 递归加载子标签的子标签（使用临时属性）
        for child in children:
            child._children_list = TagService._get_children(child.id, include_hidden)

        return children

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
            return Tag.objects.get(slug=slug)
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
        # 权限检查
        if not user.is_staff:
            raise PermissionDenied("只有管理员可以编辑标签")

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
                tag.description = description

            if color is not None:
                tag.color = color

            if icon is not None:
                tag.icon = icon

            if background_url is not None:
                tag.background_url = background_url

            if position is not None:
                tag.position = position

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
        if not user.is_staff:
            raise PermissionDenied("只有管理员可以调整标签排序")

        if direction not in {"up", "down"}:
            raise ValueError("无效的排序方向")

        tag = Tag.objects.get(id=tag_id)

        with transaction.atomic():
            siblings = list(
                Tag.objects.filter(parent_id=tag.parent_id).order_by("position", "name", "id")
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
        if not user.is_staff:
            raise PermissionDenied("只有管理员可以调整标签排序")
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
            return list(Tag.objects.select_related("parent").all().order_by("position", "name", "id"))

        tags_by_id = {
            tag.id: tag
            for tag in Tag.objects.select_related("parent").filter(id__in=seen_ids)
        }
        missing_ids = sorted(seen_ids - set(tags_by_id))
        if missing_ids:
            raise ValueError(f"标签不存在: {', '.join(str(item) for item in missing_ids)}")

        with transaction.atomic():
            for tag_id, parent_id, position in flattened:
                tag = tags_by_id[tag_id]
                parent = tags_by_id[parent_id] if parent_id is not None else None
                if parent is not None:
                    TagService.validate_parent_assignment(tag, parent)
                    tag.parent = parent
                else:
                    tag.parent = None
                tag.position = position

            Tag.objects.bulk_update(tags_by_id.values(), ["parent_id", "position"])
            TagService._compact_positions_for_all_levels()

        return list(Tag.objects.select_related("parent").all().order_by("position", "name", "id"))

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
            siblings = list(Tag.objects.filter(parent_id=parent_id).order_by("position", "name", "id"))
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
        # 权限检查
        if not user.is_staff:
            raise PermissionDenied("只有管理员可以删除标签")

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

        for tag in queryset:
            discussion_links = apply_runtime_counted_discussion_filter(
                DiscussionTag.objects.filter(tag=tag),
                prefix="discussion",
            ).select_related('discussion').order_by('-discussion__last_posted_at', '-discussion__id')

            latest_link = discussion_links.first()
            Tag.objects.filter(id=tag.id).update(
                discussion_count=discussion_links.count(),
                last_posted_at=latest_link.discussion.last_posted_at if latest_link else None,
                last_posted_discussion=latest_link.discussion if latest_link else None,
            )

