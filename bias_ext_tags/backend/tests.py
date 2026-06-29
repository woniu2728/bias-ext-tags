import json
import re
import ast
from pathlib import Path

from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from ninja_jwt.tokens import RefreshToken
from io import StringIO
from unittest.mock import Mock, patch

from bias_core.extensions import ResourceEndpointDefinition, SearchFilterDefinition
from bias_core.resource_errors import JsonApiValidationError
from bias_core.resource_objects import ResourceSearchCriteria
from bias_core.extensions.testing import (
    AuditLog,
    ExtensionInstallation,
    ExtensionRegistry,
    ExtensionRuntimeTestMixin,
    ResourceRegistry,
    bootstrap_extension_application,
    build_runtime_event,
    can_view_model_instance,
    capture_realtime_discussion_events,
    capture_runtime_events,
    clear_runtime_setting_caches,
    get_forum_event_bus,
    get_forum_registry,
    get_resource_registry,
    rebuild_runtime_urlconf,
    reset_extension_application_bootstrap_state,
    reset_extension_runtime_state,
)
from bias_ext_tags.backend.events import (
    DiscussionTaggedEvent,
    TagCreatingEvent,
    TagDeletingEvent,
    TagSavingEvent,
    TagStatsRefreshRequestedEvent,
)
from bias_ext_tags.backend.models import Tag
from bias_ext_tags.backend.services import TagService
from bias_ext_tags.backend.resources import tag_resource_endpoints
from bias_ext_tags.backend.tag_resource import TagResource
from bias_core.models import Setting
from bias_core.settings_service import clear_runtime_setting_caches


def _runtime_facade(name: str):
    from importlib import import_module

    return getattr(import_module("bias_core.extensions.runtime"), name)


def _resource_field_rule_value(rule):
    if isinstance(rule, dict) and "rule" in rule:
        return rule["rule"]
    return rule


def _resource_field_has_rule(field, expected) -> bool:
    return any(_resource_field_rule_value(rule) == expected for rule in field.validation_rules)


def approve_runtime_discussion(*args, **kwargs):
    return _runtime_facade("approve_runtime_discussion")(*args, **kwargs)


def create_runtime_discussion(*args, **kwargs):
    return _runtime_facade("create_runtime_discussion")(*args, **kwargs)


def delete_runtime_discussion(*args, **kwargs):
    return _runtime_facade("delete_runtime_discussion")(*args, **kwargs)


def get_runtime_model_url_service(*args, **kwargs):
    return _runtime_facade("get_runtime_model_url_service")(*args, **kwargs)


def get_runtime_tag_state_model(*args, **kwargs):
    return _runtime_facade("get_runtime_tag_state_model")(*args, **kwargs)


def reject_runtime_discussion(*args, **kwargs):
    return _runtime_facade("reject_runtime_discussion")(*args, **kwargs)


def set_runtime_discussion_hidden_state(*args, **kwargs):
    return _runtime_facade("set_runtime_discussion_hidden_state")(*args, **kwargs)


def list_runtime_discussions(*args, **kwargs):
    return _runtime_facade("list_runtime_discussions")(*args, **kwargs)


def to_runtime_model_slug(*args, **kwargs):
    return _runtime_facade("to_runtime_model_slug")(*args, **kwargs)


def update_runtime_discussion(*args, **kwargs):
    return _runtime_facade("update_runtime_discussion")(*args, **kwargs)


def get_runtime_discussion_tag_model(*args, **kwargs):
    return _runtime_facade("get_runtime_discussion_tag_model")(*args, **kwargs)


def approve_runtime_post(*args, **kwargs):
    return _runtime_facade("approve_runtime_post")(*args, **kwargs)


def create_runtime_post(*args, **kwargs):
    return _runtime_facade("create_runtime_post")(*args, **kwargs)


def delete_runtime_post(*args, **kwargs):
    return _runtime_facade("delete_runtime_post")(*args, **kwargs)


def get_runtime_post_model(*args, **kwargs):
    return _runtime_facade("get_runtime_post_model")(*args, **kwargs)


def reject_runtime_post(*args, **kwargs):
    return _runtime_facade("reject_runtime_post")(*args, **kwargs)


def set_runtime_post_hidden_state(*args, **kwargs):
    return _runtime_facade("set_runtime_post_hidden_state")(*args, **kwargs)


def get_runtime_group_model(*args, **kwargs):
    return _runtime_facade("get_runtime_group_model")(*args, **kwargs)


def get_runtime_permission_model(*args, **kwargs):
    return _runtime_facade("get_runtime_permission_model")(*args, **kwargs)


def get_runtime_user_model(*args, **kwargs):
    return _runtime_facade("get_runtime_user_model")(*args, **kwargs)


class RuntimeModelProxy:
    def __init__(self, resolver):
        self._resolver = resolver

    def __getattr__(self, name):
        return getattr(self._resolver(), name)


User = RuntimeModelProxy(get_runtime_user_model)
Group = RuntimeModelProxy(get_runtime_group_model)
Permission = RuntimeModelProxy(get_runtime_permission_model)
Post = RuntimeModelProxy(get_runtime_post_model)
DiscussionTag = RuntimeModelProxy(get_runtime_discussion_tag_model)
TagState = RuntimeModelProxy(get_runtime_tag_state_model)


def set_active_slug_driver(model, identifier: str):
    Setting.objects.update_or_create(
        key=f"slug_driver_{model.__module__}.{model.__qualname__}",
        defaults={"value": json.dumps(identifier)},
    )
    clear_runtime_setting_caches()


def set_tags_setting(key: str, value):
    Setting.objects.update_or_create(
        key=f"extensions.tags.{key}",
        defaults={"value": json.dumps(value)},
    )
    clear_runtime_setting_caches()


def discussion_tags_payload(tag_ids):
    return {
        "data": {
            "relationships": {
                "tags": {
                    "data": [
                        {"type": "tag", "id": str(tag_id)}
                        for tag_id in tag_ids
                    ],
                },
            },
        },
    }


def discussion_resource_payload(*, title=None, content=None, tag_ids=None):
    attributes = {}
    if title is not None:
        attributes["title"] = title
    if content is not None:
        attributes["content"] = content

    payload = {"data": {"type": "discussion", "attributes": attributes}}
    if tag_ids is not None:
        payload["data"]["relationships"] = discussion_tags_payload(tag_ids)["data"]["relationships"]
    return payload


class TagsExtensionRuntimeTests(ExtensionRuntimeTestMixin, TestCase):
    def test_tag_resources_do_not_import_runtime_facades_at_module_load(self):
        source = Path(__file__).with_name("resources.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        runtime_imports = [
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and node.module == "bias_core.extensions.runtime"
        ]

        self.assertEqual(runtime_imports, [])

    def test_tags_extension_registers_extension_settings_page(self):
        registry = ExtensionRegistry()
        extension = registry.get_extension("tags")

        self.assertEqual(extension.source, "filesystem")
        self.assertEqual(extension.manifest.frontend_admin_entry, "")
        self.assertEqual(extension.frontend_admin_entry, "extensions/tags/frontend/admin/index.js")
        self.assertEqual(extension.settings_pages, ("/admin/extensions/tags/settings",))

    def test_tags_extension_registers_runtime_service_provider(self):
        application = self.bootstrap_extensions("tags")
        service = application.get_service("tags.service")

        self.assertIn("tags.service", application.get_service_provider_keys(extension_id="tags"))
        self.assertEqual(service["model"].__name__, "Tag")
        self.assertEqual(service["state_model"].__name__, "TagState")
        self.assertEqual(service["relationship_model"].__name__, "DiscussionTag")
        for key in (
            "summaries_by_slugs",
            "create_tag",
            "move_tag",
            "order_tags",
            "delete_tag",
            "filter_tags_for_user",
            "dispatch_refresh_tag_stats",
            "refresh_discussion_tag_stats",
            "refresh_tag_stats",
            "ensure_can_start_discussion",
            "state_for_user",
            "prefetch_state_for_user",
            "mark_tag_read",
        ):
            self.assertTrue(callable(service[key]), key)

    def test_tags_extension_registers_bypass_tag_counts_permission(self):
        registry = ExtensionRegistry()
        extension = registry.get_extension("tags")

        permissions = {permission.code: permission for permission in extension.permissions}

        self.assertIn("bypassTagCounts", permissions)
        self.assertEqual(permissions["bypassTagCounts"].section, "tags")
        self.assertIn("tag.create", permissions)
        self.assertIn("tag.edit", permissions)
        self.assertIn("tag.delete", permissions)
        self.assertEqual(permissions["tag.delete"].required_permissions, ("tag.edit",))

    def test_tag_service_list_prefetches_direct_children_only(self):
        parent = Tag.objects.create(name="服务父标签", slug="service-parent", position=0, is_primary=True)
        visible_child = Tag.objects.create(name="服务子标签", slug="service-child", parent=parent, position=0, is_primary=True)
        hidden_child = Tag.objects.create(
            name="隐藏服务子标签",
            slug="service-hidden-child",
            parent=parent,
            position=1,
            is_primary=True,
            is_hidden=True,
        )
        grandchild = Tag.objects.create(
            name="服务孙级标签",
            slug="service-grandchild",
            parent=visible_child,
            position=0,
            is_primary=True,
        )

        with CaptureQueriesContext(connection) as queries:
            tags = TagService.get_tag_list()

        loaded_parent = next(tag for tag in tags if tag.id == parent.id)
        loaded_child_slugs = [tag.slug for tag in loaded_parent._children_list]
        self.assertEqual(loaded_child_slugs, ["service-child"])
        self.assertFalse(hasattr(loaded_parent._children_list[0], "_children_list"))
        self.assertNotIn(grandchild, loaded_parent._children_list)
        self.assertNotIn(hidden_child.slug, loaded_child_slugs)

        child_queries = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and re.search(r'where .*"tags"\."parent_id" in', query["sql"].lower())
        ]
        self.assertEqual(len(child_queries), 1)

        tags_with_hidden = TagService.get_tag_list(include_hidden=True)
        loaded_parent_with_hidden = next(tag for tag in tags_with_hidden if tag.id == parent.id)
        self.assertEqual(
            [tag.slug for tag in loaded_parent_with_hidden._children_list],
            ["service-child", "service-hidden-child"],
        )

    def test_tags_write_resource_endpoints_declare_forum_permissions(self):
        application = self.bootstrap_extensions("tags")
        endpoints = {
            endpoint.endpoint: endpoint
            for endpoint in application.resources.get_endpoints("tag")
        }

        self.assertEqual(endpoints["create"].forum_permission, "tag.create")
        self.assertEqual(endpoints["update"].forum_permission, "tag.edit")
        self.assertEqual(endpoints["delete"].kind, "delete")
        self.assertEqual(endpoints["delete"].ability, "delete")

    def test_tag_parent_relationship_declares_flarum_writable_condition(self):
        application = self.bootstrap_extensions("tags")
        relationships = {
            relationship.relationship: relationship
            for relationship in application.resources.get_relationships("tag")
        }
        parent = relationships["parent"]

        self.assertTrue(callable(parent.writable))
        self.assertTrue(parent.writable(None, {"payload": {"data": {"attributes": {"is_primary": True}}}}))
        self.assertFalse(parent.writable(None, {"payload": {"data": {"attributes": {"is_primary": False}}}}))

    def test_staff_users_receive_tag_management_forum_permissions(self):
        from bias_core.forum_permissions import has_forum_permission

        self.bootstrap_extensions("tags")
        staff = User.objects.create_user(
            username="tag-staff-permissions",
            email="tag-staff-permissions@example.com",
            password="password123",
            is_staff=True,
        )
        member = User.objects.create_user(
            username="tag-member-permissions",
            email="tag-member-permissions@example.com",
            password="password123",
        )

        self.assertTrue(has_forum_permission(staff, "tag.create"))
        self.assertTrue(has_forum_permission(staff, "tag.edit"))
        self.assertTrue(has_forum_permission(staff, "tag.delete"))
        self.assertFalse(has_forum_permission(member, "tag.create"))

    def test_tag_management_policy_uses_forum_permissions(self):
        from bias_core.authorization import can

        self.bootstrap_extensions("tags")
        member = User.objects.create_user(
            username="tag-policy-member",
            email="tag-policy-member@example.com",
            password="password123",
        )
        group = Group.objects.create(name="TagPolicyManagers", color="#4d698e")
        member.user_groups.add(group)
        tag = Tag.objects.create(name="策略标签", slug="policy-tag")

        self.assertFalse(can(member, "edit", tag))
        Permission.objects.create(group=group, permission="tag.edit")
        if hasattr(member, "_forum_permission_cache"):
            delattr(member, "_forum_permission_cache")

        self.assertTrue(can(member, "edit", tag))
        self.assertTrue(can(member, "move", tag))

    def test_tag_policy_denies_child_ability_when_restricted_parent_is_denied(self):
        from bias_core.authorization import can

        self.bootstrap_extensions("tags")
        member = User.objects.create_user(
            username="tag-policy-child-member",
            email="tag-policy-child-member@example.com",
            password="password123",
        )
        group = Group.objects.create(name="TagPolicyChildAccess", color="#4d698e")
        member.user_groups.add(group)
        parent = Tag.objects.create(
            name="受限父标签",
            slug="policy-restricted-parent",
            is_restricted=True,
            view_scope=Tag.ACCESS_MEMBERS,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        child = Tag.objects.create(
            name="受限子标签",
            slug="policy-restricted-child",
            parent=parent,
            is_restricted=True,
            view_scope=Tag.ACCESS_MEMBERS,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )

        Permission.objects.create(group=group, permission=f"tag{child.id}.viewForum")
        if hasattr(member, "_forum_permission_cache"):
            delattr(member, "_forum_permission_cache")

        self.assertFalse(can(member, "view", child))

        Permission.objects.create(group=group, permission=f"tag{parent.id}.viewForum")
        if hasattr(member, "_forum_permission_cache"):
            delattr(member, "_forum_permission_cache")

        self.assertTrue(can(member, "view", child))

    def test_tag_policy_add_to_discussion_alias_uses_start_permission(self):
        from bias_core.authorization import can

        self.bootstrap_extensions("tags")
        member = User.objects.create_user(
            username="tag-policy-add-member",
            email="tag-policy-add-member@example.com",
            password="password123",
        )
        group = Group.objects.create(name="TagPolicyAddAccess", color="#4d698e")
        member.user_groups.add(group)
        tag = Tag.objects.create(
            name="发帖标签",
            slug="policy-add-tag",
            is_restricted=True,
            view_scope=Tag.ACCESS_MEMBERS,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )

        Permission.objects.create(group=group, permission=f"tag{tag.id}.viewForum")
        if hasattr(member, "_forum_permission_cache"):
            delattr(member, "_forum_permission_cache")

        self.assertFalse(can(member, "addToDiscussion", tag))
        self.assertFalse(can(member, "add_to_discussion", tag))

        Permission.objects.create(group=group, permission=f"tag{tag.id}.startDiscussion")
        if hasattr(member, "_forum_permission_cache"):
            delattr(member, "_forum_permission_cache")

        self.assertTrue(can(member, "addToDiscussion", tag))
        self.assertTrue(can(member, "add_to_discussion", tag))

    def test_tag_global_policy_allows_start_discussion_from_accessible_primary_tag(self):
        from bias_core.authorization import can

        self.bootstrap_extensions("tags")
        member = User.objects.create_user(
            username="tag-global-primary-member",
            email="tag-global-primary-member@example.com",
            password="password123",
        )
        group = Group.objects.create(name="TagGlobalPrimary", color="#4d698e")
        member.user_groups.add(group)
        primary = Tag.objects.create(
            name="全局主标签",
            slug="global-primary",
            position=0,
            is_restricted=True,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
        )
        Permission.objects.create(group=group, permission=f"tag{primary.id}.startDiscussion")
        if hasattr(member, "_forum_permission_cache"):
            delattr(member, "_forum_permission_cache")
        set_tags_setting("min_primary_tags", 1)

        self.assertTrue(can(member, "startDiscussion"))

    def test_tag_global_policy_denies_start_discussion_from_child_tag_only(self):
        from bias_core.authorization import can

        self.bootstrap_extensions("tags")
        member = User.objects.create_user(
            username="tag-global-child-member",
            email="tag-global-child-member@example.com",
            password="password123",
        )
        group = Group.objects.create(name="TagGlobalChildOnly", color="#4d698e")
        member.user_groups.add(group)
        parent = Tag.objects.create(
            name="全局父标签",
            slug="global-parent",
            position=0,
            is_restricted=True,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
        )
        child = Tag.objects.create(
            name="全局子标签",
            slug="global-child",
            parent=parent,
            is_restricted=True,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
        )
        Permission.objects.create(group=group, permission=f"tag{child.id}.startDiscussion")
        if hasattr(member, "_forum_permission_cache"):
            delattr(member, "_forum_permission_cache")
        set_tags_setting("min_primary_tags", 1)

        self.assertFalse(can(member, "startDiscussion"))

    def test_tag_global_policy_counts_accessible_child_tags_like_positioned_primary_tags(self):
        from bias_core.authorization import can

        self.bootstrap_extensions("tags")
        member = User.objects.create_user(
            username="tag-global-child-positioned-member",
            email="tag-global-child-positioned-member@example.com",
            password="password123",
        )
        group = Group.objects.create(name="TagGlobalPositionedChild", color="#4d698e")
        member.user_groups.add(group)
        parent = Tag.objects.create(
            name="全局可访问父标签",
            slug="global-accessible-parent",
            position=0,
            is_restricted=True,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
        )
        child = Tag.objects.create(
            name="全局可访问子标签",
            slug="global-accessible-child",
            parent=parent,
            position=0,
            is_restricted=True,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
        )
        Permission.objects.create(group=group, permission=f"tag{parent.id}.startDiscussion")
        Permission.objects.create(group=group, permission=f"tag{child.id}.startDiscussion")
        if hasattr(member, "_forum_permission_cache"):
            delattr(member, "_forum_permission_cache")
        set_tags_setting("min_primary_tags", 2)
        set_tags_setting("min_secondary_tags", 0)

        self.assertTrue(can(member, "startDiscussion"))

    def test_tag_global_policy_allows_secondary_when_minimums_match(self):
        from bias_core.authorization import can

        self.bootstrap_extensions("tags")
        member = User.objects.create_user(
            username="tag-global-secondary-member",
            email="tag-global-secondary-member@example.com",
            password="password123",
        )
        group = Group.objects.create(name="TagGlobalSecondary", color="#4d698e")
        member.user_groups.add(group)
        secondary = Tag.objects.create(
            name="全局次标签",
            slug="global-secondary",
            position=None,
            is_primary=False,
            is_restricted=True,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
        )
        Permission.objects.create(group=group, permission=f"tag{secondary.id}.startDiscussion")
        if hasattr(member, "_forum_permission_cache"):
            delattr(member, "_forum_permission_cache")
        set_tags_setting("min_primary_tags", 0)
        set_tags_setting("min_secondary_tags", 1)

        self.assertTrue(can(member, "startDiscussion"))

    def test_tag_global_policy_bypass_requires_start_discussion_permission(self):
        from bias_core.authorization import can

        self.bootstrap_extensions("tags")
        member = User.objects.create_user(
            username="tag-global-bypass-member",
            email="tag-global-bypass-member@example.com",
            password="password123",
        )
        group = Group.objects.create(name="TagGlobalBypass", color="#4d698e")
        member.user_groups.add(group)
        Tag.objects.create(
            name="全局绕过主标签",
            slug="global-bypass-primary",
            position=0,
            is_restricted=True,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
        )
        set_tags_setting("min_primary_tags", 1)

        Permission.objects.create(group=group, permission="bypassTagCounts")
        if hasattr(member, "_forum_permission_cache"):
            delattr(member, "_forum_permission_cache")
        self.assertFalse(can(member, "startDiscussion"))

        Permission.objects.create(group=group, permission="startDiscussion")
        if hasattr(member, "_forum_permission_cache"):
            delattr(member, "_forum_permission_cache")
        self.assertTrue(can(member, "startDiscussion"))

    def test_tag_global_policy_caches_tag_counts_per_user_and_action(self):
        from bias_core.authorization import can

        self.bootstrap_extensions("tags")
        member = User.objects.create_user(
            username="tag-global-cache-member",
            email="tag-global-cache-member@example.com",
            password="password123",
        )
        group = Group.objects.create(name="TagGlobalCache", color="#4d698e")
        member.user_groups.add(group)
        primary = Tag.objects.create(
            name="全局缓存主标签",
            slug="global-cache-primary",
            position=0,
            is_restricted=True,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
        )
        Permission.objects.create(group=group, permission=f"tag{primary.id}.startDiscussion")
        if hasattr(member, "_forum_permission_cache"):
            delattr(member, "_forum_permission_cache")
        set_tags_setting("min_primary_tags", 1)

        with CaptureQueriesContext(connection) as queries:
            self.assertTrue(can(member, "startDiscussion"))
            self.assertTrue(can(member, "startDiscussion"))

        tag_count_queries = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and "count" in query["sql"].lower()
        ]
        self.assertLessEqual(
            len(tag_count_queries),
            4,
            "Tag global policy should cache primary/secondary tag count checks per user and action.",
        )

    def test_tag_service_management_checks_use_forum_permissions(self):
        self.bootstrap_extensions("tags")
        member = User.objects.create_user(
            username="tag-service-member",
            email="tag-service-member@example.com",
            password="password123",
        )
        group = Group.objects.create(name="TagServiceManagers", color="#4d698e")
        member.user_groups.add(group)

        with self.assertRaises(PermissionDenied):
            TagService.create_tag(name="未授权创建", slug="service-denied", user=member)

        Permission.objects.create(group=group, permission="tag.create")
        if hasattr(member, "_forum_permission_cache"):
            delattr(member, "_forum_permission_cache")

        tag = TagService.create_tag(name="授权创建", slug="service-allowed", user=member)
        self.assertEqual(tag.slug, "service-allowed")

    def test_restricted_tag_permission_scope_caches_runtime_permission_snapshot(self):
        self.bootstrap_extensions("tags")
        member = User.objects.create_user(
            username="tag-scope-cache-member",
            email="tag-scope-cache-member@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        first_tag = Tag.objects.create(name="Scope Cache One", slug="scope-cache-one", is_restricted=True)
        second_tag = Tag.objects.create(name="Scope Cache Two", slug="scope-cache-two", is_restricted=True)

        permissions = [f"tag{first_tag.id}.viewForum"]

        def runtime_permissions(_user):
            return tuple(permissions)

        with patch("bias_ext_tags.backend.services._get_runtime_forum_permissions", side_effect=runtime_permissions) as get_permissions:
            self.assertEqual(
                TagService._restricted_tag_ids_with_permission(member, "viewForum"),
                (first_tag.id,),
            )
            self.assertEqual(len(member._tag_restricted_permission_ids_cache), 1)
            self.assertEqual(
                TagService._restricted_tag_ids_with_permission(member, "viewForum"),
                (first_tag.id,),
            )
            self.assertEqual(len(member._tag_restricted_permission_ids_cache), 1)
            permissions.append(f"tag{second_tag.id}.viewForum")
            self.assertEqual(
                TagService._restricted_tag_ids_with_permission(member, "viewForum"),
                (first_tag.id, second_tag.id),
            )
            self.assertEqual(len(member._tag_restricted_permission_ids_cache), 2)

        self.assertEqual(
            get_permissions.call_count,
            3,
            "Permission snapshots should be fetched once per call, but tag-id parsing should reuse matching snapshots.",
        )

    def test_restricted_tag_policy_reuses_forum_permission_cache(self):
        self.bootstrap_extensions("tags")
        member = User.objects.create_user(
            username="tag-policy-cache-member",
            email="tag-policy-cache-member@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        tag = Tag.objects.create(
            name="Policy Cache",
            slug="policy-cache",
            is_restricted=True,
            view_scope=Tag.ACCESS_MEMBERS,
        )
        member._forum_permission_cache = {f"tag{tag.id}.viewForum"}

        with patch(
            "bias_ext_tags.backend.services._get_runtime_forum_permissions",
            side_effect=AssertionError("tag policy should reuse the actor permission snapshot"),
        ):
            self.assertTrue(TagService.can_view_tag(tag, member))
            self.assertTrue(TagService.can_view_tag(tag, member))

        member._forum_permission_cache = set()
        self.assertFalse(TagService.can_view_tag(tag, member))

    def test_restricted_tag_policy_invalidates_snapshot_when_forum_permission_cache_is_cleared(self):
        self.bootstrap_extensions("tags")
        member = User.objects.create_user(
            username="tag-policy-cache-clear-member",
            email="tag-policy-cache-clear-member@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        tag = Tag.objects.create(
            name="Policy Cache Clear",
            slug="policy-cache-clear",
            is_restricted=True,
            view_scope=Tag.ACCESS_MEMBERS,
        )

        member._forum_permission_cache = set()
        self.assertFalse(TagService.can_view_tag(tag, member))

        delattr(member, "_forum_permission_cache")

        with patch(
            "bias_ext_tags.backend.services._get_runtime_forum_permissions",
            return_value={f"tag{tag.id}.viewForum"},
        ) as get_permissions:
            self.assertTrue(TagService.can_view_tag(tag, member))

        self.assertEqual(get_permissions.call_count, 1)

    def test_tags_posts_integration_is_optional(self):
        self.disable_extension_for_test("posts")
        application = self.bootstrap_extensions("tags")
        forum_registry = get_forum_registry()
        resource_registry = get_resource_registry()

        self.assertIsNone(forum_registry.get_post_type("discussionTagged"))
        self.assertFalse(any(
            item.resource == "post" and item.relationship == "eventPostMentionsTags"
            for item in resource_registry.get_relationships("post")
        ))
        self.assertFalse(any(
            getattr(getattr(item, "handler", None), "__name__", "") == "handle_post_created_tag_stats"
            for item in application.events.get_listeners(extension_id="tags")
        ))

    def test_tags_registers_post_integration_when_posts_enabled(self):
        application = self.bootstrap_extensions("posts", "tags")
        forum_registry = get_forum_registry()
        resource_registry = get_resource_registry()

        self.assertIsNotNone(forum_registry.get_post_type("discussionTagged"))
        self.assertTrue(any(
            item.resource == "post" and item.relationship == "eventPostMentionsTags"
            for item in resource_registry.get_relationships("post")
        ))
        self.assertTrue(any(
            getattr(getattr(item, "handler", None), "__name__", "") == "handle_post_created_tag_stats"
            for item in application.events.get_listeners(extension_id="tags")
        ))

    def test_tags_extension_registers_default_and_id_slug_drivers(self):
        self.bootstrap_extensions("tags")
        model_urls = get_runtime_model_url_service()

        drivers = model_urls.get_slug_drivers(Tag)

        self.assertEqual(
            {driver.identifier for driver in drivers},
            {"default", "id_with_slug"},
        )

    def test_id_with_slug_driver_formats_and_resolves_by_leading_id(self):
        self.bootstrap_extensions("tags")
        tag = Tag.objects.create(name="公告", slug="announcements")

        slug = to_runtime_model_slug(Tag, tag, identifier="id_with_slug")

        self.assertEqual(slug, f"{tag.id}-announcements")
        self.assertEqual(TagService.get_tag_by_url_slug(f"{tag.id}-renamed", driver="id_with_slug"), tag)

    def test_tag_slug_lookup_falls_back_to_id_with_slug_when_default_slug_missing(self):
        self.bootstrap_extensions("tags")
        tag = Tag.objects.create(name="公告", slug="announcements")

        self.assertEqual(TagService.get_tag_by_url_slug(f"{tag.id}-renamed", driver="id_with_slug"), tag)

    def test_tags_capabilities_are_filtered_when_extension_disabled(self):
        self.disable_extension_for_test("tags")

        resource_registry = get_resource_registry()
        forum_registry = get_forum_registry()

        self.assertFalse(any(item.module_id == "tags" for item in resource_registry.get_fields("discussion")))
        self.assertFalse(any(item.module_id == "tags" for item in resource_registry.get_relationships("discussion")))
        self.assertIsNone(resource_registry.get_dispatch_endpoint("tag", "index", "GET", {}))
        self.assertFalse(any(item.module_id == "tags" for item in forum_registry.get_search_filters()))


class TagStatsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="tagger",
            email="tagger@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        self.tag = Tag.objects.create(
            name="生活",
            slug="life",
            color="#4d698e",
        )

    def test_create_discussion_refreshes_tag_count(self):
        events, dispatch_patch = capture_runtime_events()
        with patch("bias_ext_tags.backend.services.TagService.refresh_tag_stats") as refresh_tag_stats:
            with dispatch_patch:
                with self.captureOnCommitCallbacks(execute=True):
                    create_runtime_discussion(
                        title="生活讨论 1",
                        content="第一条生活内容",
                        user=self.user,
                        extension_payload=discussion_tags_payload([self.tag.id]),
                    )
                with self.captureOnCommitCallbacks(execute=True):
                    create_runtime_discussion(
                        title="生活讨论 2",
                        content="第二条生活内容",
                        user=self.user,
                        extension_payload=discussion_tags_payload([self.tag.id]),
                    )

        self.tag.refresh_from_db()

        self.assertEqual(self.tag.discussion_count, 2)
        self.assertIsNotNone(self.tag.last_posted_discussion)
        refresh_tag_stats.assert_not_called()
        self.assertFalse(any(isinstance(event, TagStatsRefreshRequestedEvent) for event in events))

    def test_refresh_tag_stats_repairs_existing_discussion_count(self):
        discussion = create_runtime_discussion(
            title="历史讨论",
            content="历史内容",
            user=self.user,
        )
        DiscussionTag.objects.create(discussion=discussion, tag=self.tag)
        Tag.objects.filter(id=self.tag.id).update(discussion_count=0)

        TagService.refresh_tag_stats([self.tag.id])
        self.tag.refresh_from_db()

        self.assertEqual(self.tag.discussion_count, 1)

    def test_refresh_tag_stats_extension_command_repairs_all_tags(self):
        discussion = create_runtime_discussion(
            title="命令刷新统计",
            content="命令刷新内容",
            user=self.user,
        )
        DiscussionTag.objects.create(discussion=discussion, tag=self.tag)
        Tag.objects.filter(id=self.tag.id).update(discussion_count=0)

        stdout = StringIO()
        call_command("extension_console", "tags.refresh_stats", stdout=stdout)
        self.tag.refresh_from_db()

        self.assertEqual(self.tag.discussion_count, 1)
        self.assertIn("已刷新全部标签统计", stdout.getvalue())

    def test_refresh_tag_stats_batches_counts_and_latest_discussions(self):
        tags = [
            Tag.objects.create(name=f"批量标签 {index}", slug=f"batch-tag-{index}")
            for index in range(6)
        ]
        discussions_by_tag = {}
        for index, tag in enumerate(tags):
            first = create_runtime_discussion(
                title=f"Batch discussion {index} older",
                content="Older discussion",
                user=self.user,
            )
            second = create_runtime_discussion(
                title=f"Batch discussion {index} newer",
                content="Newer discussion",
                user=self.user,
            )
            DiscussionTag.objects.create(discussion=first, tag=tag)
            DiscussionTag.objects.create(discussion=second, tag=tag)
            discussions_by_tag[tag.id] = second

        Tag.objects.filter(id__in=[tag.id for tag in tags]).update(
            discussion_count=0,
            last_posted_discussion=None,
            last_posted_at=None,
        )

        with CaptureQueriesContext(connection) as queries:
            TagService.refresh_tag_stats([tag.id for tag in tags])

        for tag in tags:
            tag.refresh_from_db()
            self.assertEqual(tag.discussion_count, 2)
            self.assertEqual(tag.last_posted_discussion_id, discussions_by_tag[tag.id].id)

        per_tag_link_queries = [
            query["sql"]
            for query in queries
            if 'from "discussion_tag"' in query["sql"].lower()
            and 'where "discussion_tag"."tag_id" =' in query["sql"].lower()
        ]
        self.assertEqual(
            per_tag_link_queries,
            [],
            "Refreshing tag stats should batch discussion_tag aggregation instead of querying once per tag.",
        )

    @override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
    def test_dispatch_refresh_tag_stats_queues_when_enabled(self):
        from bias_ext_tags.backend.tasks import refresh_tag_stats_task

        clear_runtime_setting_caches()
        with patch("bias_core.queue_service.QueueService.get_runtime_config", return_value={"enabled": True, "driver": "redis"}):
            with patch.object(refresh_tag_stats_task, "delay") as delay:
                with patch("bias_ext_tags.backend.services.TagService.refresh_tag_stats") as refresh_tag_stats:
                    with self.captureOnCommitCallbacks(execute=True):
                        result = TagService.dispatch_refresh_tag_stats([self.tag.id])

        self.assertEqual(result["mode"], "queued")
        delay.assert_called_once_with([self.tag.id])
        refresh_tag_stats.assert_not_called()

    def test_pending_discussion_is_not_counted_until_approved(self):
        trusted_group = Group.objects.create(name="TagTrusted", color="#4d698e")
        Permission.objects.create(group=trusted_group, permission="startDiscussionWithoutApproval")
        admin = User.objects.create_superuser(
            username="tag-admin",
            email="tag-admin@example.com",
            password="password123",
        )

        with self.captureOnCommitCallbacks(execute=True):
            discussion = create_runtime_discussion(
                title="待审核标签讨论",
                content="等待审核",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.discussion_count, 0)
        self.assertIsNone(self.tag.last_posted_discussion)

        with patch("bias_ext_tags.backend.services.TagService.refresh_tag_stats") as refresh_tag_stats:
            with patch("bias_ext_tags.backend.listeners.refresh_runtime_discussion_tag_stats") as refresh_discussion_tag_stats:
                with self.captureOnCommitCallbacks(execute=True):
                    approve_runtime_discussion(discussion, admin)
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.discussion_count, 1)
        self.assertEqual(self.tag.last_posted_discussion_id, discussion.id)
        refresh_tag_stats.assert_not_called()
        refresh_discussion_tag_stats.assert_not_called()

    def test_hiding_non_latest_discussion_updates_tag_count_without_refresh(self):
        admin = User.objects.create_superuser(
            username="tag-hide-admin",
            email="tag-hide-admin@example.com",
            password="password123",
        )
        with self.captureOnCommitCallbacks(execute=True):
            older_discussion = create_runtime_discussion(
                title="较早标签讨论",
                content="较早内容",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )
        with self.captureOnCommitCallbacks(execute=True):
            newer_discussion = create_runtime_discussion(
                title="较新标签讨论",
                content="较新内容",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )

        with patch("bias_ext_tags.backend.services.TagService.refresh_tag_stats") as refresh_tag_stats:
            with self.captureOnCommitCallbacks(execute=True):
                set_runtime_discussion_hidden_state(older_discussion, admin, True)

        self.tag.refresh_from_db()
        self.assertEqual(self.tag.discussion_count, 1)
        self.assertEqual(self.tag.last_posted_discussion_id, newer_discussion.id)
        refresh_tag_stats.assert_not_called()

    def test_hiding_latest_discussion_refreshes_tag_latest_discussion(self):
        admin = User.objects.create_superuser(
            username="tag-hide-latest-admin",
            email="tag-hide-latest-admin@example.com",
            password="password123",
        )
        with self.captureOnCommitCallbacks(execute=True):
            older_discussion = create_runtime_discussion(
                title="较早 latest 标签讨论",
                content="较早内容",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )
        with self.captureOnCommitCallbacks(execute=True):
            newer_discussion = create_runtime_discussion(
                title="较新 latest 标签讨论",
                content="较新内容",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )

        with self.captureOnCommitCallbacks(execute=True):
            set_runtime_discussion_hidden_state(newer_discussion, admin, True)

        self.tag.refresh_from_db()
        self.assertEqual(self.tag.discussion_count, 1)
        self.assertEqual(self.tag.last_posted_discussion_id, older_discussion.id)

    def test_rejecting_non_latest_discussion_updates_tag_count_without_refresh(self):
        admin = User.objects.create_superuser(
            username="tag-reject-admin",
            email="tag-reject-admin@example.com",
            password="password123",
        )
        with self.captureOnCommitCallbacks(execute=True):
            older_discussion = create_runtime_discussion(
                title="较早拒绝标签讨论",
                content="较早内容",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )
        with self.captureOnCommitCallbacks(execute=True):
            newer_discussion = create_runtime_discussion(
                title="较新拒绝标签讨论",
                content="较新内容",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )

        with patch("bias_ext_tags.backend.services.TagService.refresh_tag_stats") as refresh_tag_stats:
            with patch("bias_ext_tags.backend.listeners.refresh_runtime_discussion_tag_stats") as refresh_discussion_tag_stats:
                with self.captureOnCommitCallbacks(execute=True):
                    reject_runtime_discussion(older_discussion, admin)

        self.tag.refresh_from_db()
        self.assertEqual(self.tag.discussion_count, 1)
        self.assertEqual(self.tag.last_posted_discussion_id, newer_discussion.id)
        refresh_tag_stats.assert_not_called()
        refresh_discussion_tag_stats.assert_not_called()

    def test_rejecting_latest_discussion_refreshes_tag_latest_discussion(self):
        admin = User.objects.create_superuser(
            username="tag-reject-latest-admin",
            email="tag-reject-latest-admin@example.com",
            password="password123",
        )
        with self.captureOnCommitCallbacks(execute=True):
            older_discussion = create_runtime_discussion(
                title="较早 latest 拒绝标签讨论",
                content="较早内容",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )
        with self.captureOnCommitCallbacks(execute=True):
            newer_discussion = create_runtime_discussion(
                title="较新 latest 拒绝标签讨论",
                content="较新内容",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )

        with patch("bias_ext_tags.backend.listeners.refresh_runtime_discussion_tag_stats") as refresh_discussion_tag_stats:
            with self.captureOnCommitCallbacks(execute=True):
                reject_runtime_discussion(newer_discussion, admin)

        self.tag.refresh_from_db()
        self.assertEqual(self.tag.discussion_count, 1)
        self.assertEqual(self.tag.last_posted_discussion_id, older_discussion.id)
        refresh_discussion_tag_stats.assert_not_called()

    def test_create_tag_generates_slug_when_missing(self):
        admin = User.objects.create_superuser(
            username="tag-admin-2",
            email="tag-admin-2@example.com",
            password="password123",
        )
        tag = TagService.create_tag(name="纯中文标签", user=admin)

        self.assertTrue(tag.slug)
        self.assertEqual(tag.slug, tag.slug.strip())

    def test_reply_refreshes_tag_last_posted_at(self):
        with self.captureOnCommitCallbacks(execute=True):
            discussion = create_runtime_discussion(
                title="标签回复刷新",
                content="首帖",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )
        self.tag.refresh_from_db()
        initial_last_posted_at = self.tag.last_posted_at

        with patch("bias_ext_tags.backend.services.TagService.refresh_tag_stats") as refresh_tag_stats:
            with patch("bias_ext_tags.backend.listeners.refresh_runtime_discussion_tag_stats") as refresh_discussion_tag_stats:
                with self.captureOnCommitCallbacks(execute=True):
                    create_runtime_post(
                        discussion_id=discussion.id,
                        content="新的回复",
                        user=self.user,
                    )

        self.tag.refresh_from_db()
        self.assertIsNotNone(initial_last_posted_at)
        self.assertGreater(self.tag.last_posted_at, initial_last_posted_at)
        self.assertEqual(self.tag.last_posted_discussion_id, discussion.id)
        refresh_tag_stats.assert_not_called()
        refresh_discussion_tag_stats.assert_not_called()

    def test_hiding_non_latest_reply_does_not_refresh_tag_stats(self):
        admin = User.objects.create_superuser(
            username="tag-post-hide-admin",
            email="tag-post-hide-admin@example.com",
            password="password123",
        )
        with self.captureOnCommitCallbacks(execute=True):
            older_discussion = create_runtime_discussion(
                title="较早回复讨论",
                content="首帖",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )
        older_reply = create_runtime_post(
            discussion_id=older_discussion.id,
            content="较早回复",
            user=self.user,
        )
        with self.captureOnCommitCallbacks(execute=True):
            newer_discussion = create_runtime_discussion(
                title="较新回复讨论",
                content="首帖",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )

        with patch("bias_ext_tags.backend.services.TagService.refresh_tag_stats") as refresh_tag_stats:
            with patch("bias_ext_tags.backend.listeners.refresh_runtime_discussion_tag_stats") as refresh_discussion_tag_stats:
                with self.captureOnCommitCallbacks(execute=True):
                    set_runtime_post_hidden_state(older_reply, admin, True)

        self.tag.refresh_from_db()
        self.assertEqual(self.tag.last_posted_discussion_id, newer_discussion.id)
        refresh_tag_stats.assert_not_called()
        refresh_discussion_tag_stats.assert_not_called()

    def test_hiding_latest_reply_rebuilds_tag_latest_from_updated_discussion(self):
        admin = User.objects.create_superuser(
            username="tag-post-hide-latest-admin",
            email="tag-post-hide-latest-admin@example.com",
            password="password123",
        )
        with self.captureOnCommitCallbacks(execute=True):
            discussion = create_runtime_discussion(
                title="隐藏最后回复",
                content="首帖",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )
        reply = create_runtime_post(
            discussion_id=discussion.id,
            content="最后回复",
            user=self.user,
        )

        self.tag.refresh_from_db()
        discussion.refresh_from_db()
        self.assertEqual(self.tag.last_posted_discussion_id, discussion.id)
        self.assertEqual(self.tag.last_posted_at, discussion.last_posted_at)

        with patch("bias_ext_tags.backend.listeners.refresh_runtime_discussion_tag_stats") as refresh_discussion_tag_stats:
            with self.captureOnCommitCallbacks(execute=True):
                set_runtime_post_hidden_state(reply, admin, True)

        discussion.refresh_from_db()
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.last_posted_discussion_id, discussion.id)
        self.assertEqual(self.tag.last_posted_at, discussion.last_posted_at)
        self.assertLess(self.tag.last_posted_at, reply.created_at)
        refresh_discussion_tag_stats.assert_not_called()

    def test_restoring_reply_updates_tag_latest_without_refresh(self):
        admin = User.objects.create_superuser(
            username="tag-post-restore-admin",
            email="tag-post-restore-admin@example.com",
            password="password123",
        )
        with self.captureOnCommitCallbacks(execute=True):
            discussion = create_runtime_discussion(
                title="恢复回复",
                content="首帖",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )
        reply = create_runtime_post(
            discussion_id=discussion.id,
            content="可恢复回复",
            user=self.user,
        )
        with self.captureOnCommitCallbacks(execute=True):
            set_runtime_post_hidden_state(reply, admin, True)

        with patch("bias_ext_tags.backend.services.TagService.refresh_tag_stats") as refresh_tag_stats:
            with patch("bias_ext_tags.backend.listeners.refresh_runtime_discussion_tag_stats") as refresh_discussion_tag_stats:
                with self.captureOnCommitCallbacks(execute=True):
                    set_runtime_post_hidden_state(reply, admin, False)

        self.tag.refresh_from_db()
        self.assertEqual(self.tag.last_posted_discussion_id, discussion.id)
        discussion.refresh_from_db()
        self.assertEqual(self.tag.last_posted_at, discussion.last_posted_at)
        refresh_tag_stats.assert_not_called()
        refresh_discussion_tag_stats.assert_not_called()

    def test_approving_reply_updates_tag_latest_without_refresh(self):
        trusted_group = Group.objects.create(name="TagPendingReplyTrusted", color="#4d698e")
        Permission.objects.create(group=trusted_group, permission="replyWithoutApproval")
        admin = User.objects.create_superuser(
            username="tag-post-approve-admin",
            email="tag-post-approve-admin@example.com",
            password="password123",
        )
        with self.captureOnCommitCallbacks(execute=True):
            discussion = create_runtime_discussion(
                title="审核回复标签",
                content="首帖",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )
        pending_reply = create_runtime_post(
            discussion_id=discussion.id,
            content="等待审核回复",
            user=self.user,
        )

        self.tag.refresh_from_db()
        initial_last_posted_at = self.tag.last_posted_at

        with patch("bias_ext_tags.backend.services.TagService.refresh_tag_stats") as refresh_tag_stats:
            with patch("bias_ext_tags.backend.listeners.refresh_runtime_discussion_tag_stats") as refresh_discussion_tag_stats:
                with self.captureOnCommitCallbacks(execute=True):
                    approve_runtime_post(pending_reply, admin)

        discussion.refresh_from_db()
        self.tag.refresh_from_db()
        self.assertGreater(self.tag.last_posted_at, initial_last_posted_at)
        self.assertEqual(self.tag.last_posted_at, discussion.last_posted_at)
        self.assertEqual(self.tag.last_posted_discussion_id, discussion.id)
        refresh_tag_stats.assert_not_called()
        refresh_discussion_tag_stats.assert_not_called()

    def test_rejecting_latest_reply_refreshes_only_latest_tags(self):
        admin = User.objects.create_superuser(
            username="tag-post-reject-admin",
            email="tag-post-reject-admin@example.com",
            password="password123",
        )
        with self.captureOnCommitCallbacks(execute=True):
            discussion = create_runtime_discussion(
                title="拒绝回复标签",
                content="首帖",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )
        reply = create_runtime_post(
            discussion_id=discussion.id,
            content="会被拒绝的回复",
            user=self.user,
        )

        with patch("bias_ext_tags.backend.listeners.refresh_runtime_discussion_tag_stats") as refresh_discussion_tag_stats:
            with self.captureOnCommitCallbacks(execute=True):
                reject_runtime_post(reply, admin)

        discussion.refresh_from_db()
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.last_posted_discussion_id, discussion.id)
        self.assertEqual(self.tag.last_posted_at, discussion.last_posted_at)
        self.assertLess(self.tag.last_posted_at, reply.created_at)
        refresh_discussion_tag_stats.assert_not_called()

    def test_deleting_non_latest_reply_does_not_refresh_tag_stats(self):
        admin = User.objects.create_superuser(
            username="tag-post-delete-admin",
            email="tag-post-delete-admin@example.com",
            password="password123",
        )
        with self.captureOnCommitCallbacks(execute=True):
            older_discussion = create_runtime_discussion(
                title="删除非最新回复",
                content="首帖",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )
        old_reply = create_runtime_post(
            discussion_id=older_discussion.id,
            content="较早会删除回复",
            user=self.user,
        )
        with self.captureOnCommitCallbacks(execute=True):
            newer_discussion = create_runtime_discussion(
                title="删除非最新回复之后的新讨论",
                content="首帖",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )

        with patch("bias_ext_tags.backend.services.TagService.refresh_tag_stats") as refresh_tag_stats:
            with patch("bias_ext_tags.backend.listeners.refresh_runtime_discussion_tag_stats") as refresh_discussion_tag_stats:
                with self.captureOnCommitCallbacks(execute=True):
                    delete_runtime_post(old_reply.id, admin)

        self.tag.refresh_from_db()
        self.assertEqual(self.tag.last_posted_discussion_id, newer_discussion.id)
        refresh_tag_stats.assert_not_called()
        refresh_discussion_tag_stats.assert_not_called()

    def test_deleting_latest_reply_refreshes_only_latest_tags(self):
        admin = User.objects.create_superuser(
            username="tag-post-delete-latest-admin",
            email="tag-post-delete-latest-admin@example.com",
            password="password123",
        )
        with self.captureOnCommitCallbacks(execute=True):
            discussion = create_runtime_discussion(
                title="删除最新回复",
                content="首帖",
                user=self.user,
                extension_payload=discussion_tags_payload([self.tag.id]),
            )
        reply = create_runtime_post(
            discussion_id=discussion.id,
            content="最新会删除回复",
            user=self.user,
        )

        with patch("bias_ext_tags.backend.listeners.refresh_runtime_discussion_tag_stats") as refresh_discussion_tag_stats:
            with self.captureOnCommitCallbacks(execute=True):
                delete_runtime_post(reply.id, admin)

        discussion.refresh_from_db()
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.last_posted_discussion_id, discussion.id)
        self.assertEqual(self.tag.last_posted_at, discussion.last_posted_at)
        self.assertLess(self.tag.last_posted_at, reply.created_at)
        refresh_discussion_tag_stats.assert_not_called()


class TagAccessApiTests(ExtensionRuntimeTestMixin, TestCase):
    def _pre_setup(self):
        super()._pre_setup()
        self.bootstrap_extensions("tags")

    def setUp(self):
        self.member = User.objects.create_user(
            username="member",
            email="member@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        self.admin = User.objects.create_superuser(
            username="tag-admin",
            email="tag-admin@example.com",
            password="password123",
        )
        self.public_tag = Tag.objects.create(name="公开", slug="public-tag")
        self.members_tag = Tag.objects.create(
            name="成员区",
            slug="members-tag",
            view_scope=Tag.ACCESS_MEMBERS,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        self.staff_tag = Tag.objects.create(
            name="管理区",
            slug="staff-tag",
            view_scope=Tag.ACCESS_STAFF,
            start_discussion_scope=Tag.ACCESS_STAFF,
            reply_scope=Tag.ACCESS_STAFF,
        )

    def auth_header(self, user):
        token = RefreshToken.for_user(user).access_token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def jsonapi_header(self, user=None):
        headers = {"HTTP_ACCEPT": "application/vnd.api+json"}
        if user is not None:
            headers.update(self.auth_header(user))
        return headers

    def test_guest_tag_list_hides_member_and_staff_tags(self):
        response = self.client.get("/api/tags")

        self.assertEqual(response.status_code, 200, response.content)
        slugs = [tag["slug"] for tag in response.json()["data"]]
        self.assertEqual(slugs, ["public-tag"])

    def test_member_tag_list_for_start_discussion_excludes_staff_only_tags(self):
        response = self.client.get(
            "/api/tags",
            {"purpose": "start_discussion"},
            **self.auth_header(self.member),
        )

        self.assertEqual(response.status_code, 200, response.content)
        slugs = {tag["slug"] for tag in response.json()["data"]}
        self.assertEqual(slugs, {"public-tag", "members-tag"})

    def test_tag_write_resource_endpoints_require_declared_permissions(self):
        create_response = self.client.post(
            "/api/tags",
            data=json.dumps({"name": "成员创建标签", "slug": "member-create-tag"}),
            content_type="application/json",
            **self.auth_header(self.member),
        )
        update_response = self.client.patch(
            f"/api/tags/{self.public_tag.id}",
            data=json.dumps({"name": "成员改名"}),
            content_type="application/json",
            **self.auth_header(self.member),
        )
        delete_response = self.client.delete(
            f"/api/tags/{self.public_tag.id}",
            **self.auth_header(self.member),
        )

        self.assertEqual(create_response.status_code, 403, create_response.content)
        self.assertEqual(update_response.status_code, 403, update_response.content)
        self.assertEqual(delete_response.status_code, 403, delete_response.content)

    def test_staff_can_use_tag_write_resource_endpoints(self):
        create_response = self.client.post(
            "/api/tags",
            data=json.dumps({"name": "资源端点标签", "slug": "resource-endpoint-tag"}),
            content_type="application/json",
            **self.auth_header(self.admin),
        )

        self.assertEqual(create_response.status_code, 200, create_response.content)
        tag_id = create_response.json()["id"]

        update_response = self.client.patch(
            f"/api/tags/{tag_id}",
            data=json.dumps({"name": "资源端点标签更新"}),
            content_type="application/json",
            **self.auth_header(self.admin),
        )
        delete_response = self.client.delete(
            f"/api/tags/{tag_id}",
            **self.auth_header(self.admin),
        )

        self.assertEqual(update_response.status_code, 200, update_response.content)
        self.assertEqual(update_response.json()["name"], "资源端点标签更新")
        self.assertEqual(delete_response.status_code, 200, delete_response.content)
        self.assertFalse(Tag.objects.filter(id=tag_id).exists())

    def test_tag_write_resource_endpoints_dispatch_flarum_lifecycle_events(self):
        events, dispatch_patch = capture_runtime_events()
        with dispatch_patch:
            create_response = self.client.post(
                "/api/tags",
                data=json.dumps({"name": "事件标签", "slug": "event-tag"}),
                content_type="application/json",
                **self.auth_header(self.admin),
            )

            self.assertEqual(create_response.status_code, 200, create_response.content)
            tag_id = create_response.json()["id"]
            update_response = self.client.patch(
                f"/api/tags/{tag_id}",
                data=json.dumps({"name": "事件标签更新"}),
                content_type="application/json",
                **self.auth_header(self.admin),
            )
            delete_response = self.client.delete(
                f"/api/tags/{tag_id}",
                **self.auth_header(self.admin),
            )

        self.assertEqual(update_response.status_code, 200, update_response.content)
        self.assertEqual(delete_response.status_code, 200, delete_response.content)

        creating_events = [event for event in events if isinstance(event, TagCreatingEvent)]
        saving_events = [event for event in events if isinstance(event, TagSavingEvent)]
        deleting_events = [event for event in events if isinstance(event, TagDeletingEvent)]
        self.assertEqual(len(creating_events), 1)
        self.assertEqual(len(saving_events), 2)
        self.assertEqual(len(deleting_events), 1)
        self.assertEqual(creating_events[0].tag.name, "事件标签")
        self.assertEqual(creating_events[0].actor.id, self.admin.id)
        self.assertEqual(creating_events[0].data["data"]["attributes"]["name"], "事件标签")
        self.assertEqual(saving_events[0].tag.slug, "event-tag")
        self.assertEqual(saving_events[1].tag.name, "事件标签更新")
        self.assertEqual(deleting_events[0].tag.id, tag_id)

    def test_tag_lifecycle_listeners_can_mutate_model_before_create_save(self):
        from bias_core.extensions.platform import get_runtime_forum_event_bus

        def mutate_created_tag(event):
            event.tag.description = "created by lifecycle"
            event.tag.color = "#123abc"

        get_runtime_forum_event_bus().register(
            TagSavingEvent,
            mutate_created_tag,
            listener_key="tests.tags.lifecycle.create",
        )

        response = self.client.post(
            "/api/tags",
            data=json.dumps({"name": "生命周期创建", "slug": "lifecycle-create"}),
            content_type="application/json",
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        tag = Tag.objects.get(id=response.json()["id"])
        self.assertEqual(tag.description, "created by lifecycle")
        self.assertEqual(tag.color, "#123abc")

    def test_tag_lifecycle_listeners_can_mutate_model_before_update_save(self):
        from bias_core.extensions.platform import get_runtime_forum_event_bus

        def mutate_updated_tag(event):
            if event.tag.id == self.public_tag.id:
                event.tag.description = "updated by lifecycle"
                event.tag.color = "#456def"

        get_runtime_forum_event_bus().register(
            TagSavingEvent,
            mutate_updated_tag,
            listener_key="tests.tags.lifecycle.update",
        )

        response = self.client.patch(
            f"/api/tags/{self.public_tag.id}",
            data=json.dumps({"name": "生命周期更新"}),
            content_type="application/json",
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.public_tag.refresh_from_db()
        self.assertEqual(self.public_tag.name, "生命周期更新")
        self.assertEqual(self.public_tag.description, "updated by lifecycle")
        self.assertEqual(self.public_tag.color, "#456def")

    def test_tag_resource_persists_default_sort(self):
        create_response = self.client.post(
            "/api/tags",
            data=json.dumps({
                "name": "默认排序标签",
                "slug": "default-sort-tag",
                "default_sort": "latest",
            }),
            content_type="application/json",
            **self.auth_header(self.admin),
        )

        self.assertEqual(create_response.status_code, 200, create_response.content)
        self.assertEqual(create_response.json()["default_sort"], "latest")

        tag_id = create_response.json()["id"]
        update_response = self.client.patch(
            f"/api/tags/{tag_id}",
            data=json.dumps({"default_sort": "top"}),
            content_type="application/json",
            **self.auth_header(self.admin),
        )

        self.assertEqual(update_response.status_code, 200, update_response.content)
        self.assertEqual(update_response.json()["default_sort"], "top")
        self.assertEqual(Tag.objects.get(id=tag_id).default_sort, "top")

    def test_tag_resource_rejects_slug_with_slash_or_space(self):
        slash_response = self.client.post(
            "/api/tags",
            data=json.dumps({"name": "非法 slug 标签", "slug": "invalid/slug"}),
            content_type="application/json",
            **self.auth_header(self.admin),
        )
        space_response = self.client.post(
            "/api/tags",
            data=json.dumps({"name": "非法空白 slug 标签", "slug": "invalid slug"}),
            content_type="application/json",
            **self.auth_header(self.admin),
        )

        self.assertEqual(slash_response.status_code, 400, slash_response.content)
        self.assertEqual(space_response.status_code, 400, space_response.content)
        self.assertFalse(Tag.objects.filter(name__in=["非法 slug 标签", "非法空白 slug 标签"]).exists())

    def test_tag_resource_rejects_description_longer_than_flarum_limit(self):
        response = self.client.post(
            "/api/tags",
            data=json.dumps({
                "name": "描述过长标签",
                "slug": "long-description-tag",
                "description": "x" * 701,
            }),
            content_type="application/json",
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Tag.objects.filter(slug="long-description-tag").exists())

    def test_member_with_tag_management_permissions_can_use_resource_endpoints(self):
        group = Group.objects.create(name="ResourceTagManagers", color="#4d698e")
        Permission.objects.create(group=group, permission="tag.create")
        Permission.objects.create(group=group, permission="tag.edit")
        Permission.objects.create(group=group, permission="tag.delete")
        self.member.user_groups.add(group)
        if hasattr(self.member, "_forum_permission_cache"):
            delattr(self.member, "_forum_permission_cache")

        create_response = self.client.post(
            "/api/tags",
            data=json.dumps({"name": "成员授权标签", "slug": "member-managed-tag"}),
            content_type="application/json",
            **self.auth_header(self.member),
        )

        self.assertEqual(create_response.status_code, 200, create_response.content)
        tag_id = create_response.json()["id"]

        update_response = self.client.patch(
            f"/api/tags/{tag_id}",
            data=json.dumps({"name": "成员授权标签更新"}),
            content_type="application/json",
            **self.auth_header(self.member),
        )
        delete_response = self.client.delete(
            f"/api/tags/{tag_id}",
            **self.auth_header(self.member),
        )

        self.assertEqual(update_response.status_code, 200, update_response.content)
        self.assertEqual(update_response.json()["name"], "成员授权标签更新")
        self.assertEqual(delete_response.status_code, 200, delete_response.content)
        self.assertFalse(Tag.objects.filter(id=tag_id).exists())

    def test_tag_editor_can_include_hidden_tags_without_staff_flag(self):
        hidden_parent = Tag.objects.create(
            name="隐藏父标签",
            slug="hidden-editor-parent",
            is_hidden=True,
            position=5,
            is_primary=True,
        )
        hidden_child = Tag.objects.create(
            name="隐藏子标签",
            slug="hidden-editor-child",
            parent=hidden_parent,
            is_hidden=True,
            position=0,
            is_primary=True,
        )

        denied_response = self.client.get(
            "/api/tags",
            {"include_hidden": True},
            **self.auth_header(self.member),
        )
        self.assertEqual(denied_response.status_code, 200, denied_response.content)
        denied_slugs = {tag["slug"] for tag in denied_response.json()["data"]}
        self.assertNotIn(hidden_parent.slug, denied_slugs)

        group = Group.objects.create(name="HiddenTagEditors", color="#4d698e")
        Permission.objects.create(group=group, permission="tag.edit")
        self.member.user_groups.add(group)
        if hasattr(self.member, "_forum_permission_cache"):
            delattr(self.member, "_forum_permission_cache")

        allowed_response = self.client.get(
            "/api/tags",
            {"include_hidden": True},
            **self.auth_header(self.member),
        )

        self.assertEqual(allowed_response.status_code, 200, allowed_response.content)
        payload_by_slug = {tag["slug"]: tag for tag in allowed_response.json()["data"]}
        self.assertIn(hidden_parent.slug, payload_by_slug)
        self.assertEqual(
            [child["slug"] for child in payload_by_slug[hidden_parent.slug]["children"]],
            [hidden_child.slug],
        )

    def test_tag_editor_include_children_relationship_respects_include_hidden(self):
        hidden_parent = Tag.objects.create(
            name="隐藏关系父标签",
            slug="hidden-relationship-parent",
            is_hidden=True,
            position=6,
            is_primary=True,
        )
        hidden_child = Tag.objects.create(
            name="隐藏关系子标签",
            slug="hidden-relationship-child",
            parent=hidden_parent,
            is_hidden=True,
            position=0,
            is_primary=True,
        )
        group = Group.objects.create(name="HiddenTagRelationshipEditors", color="#4d698e")
        Permission.objects.create(group=group, permission="tag.edit")
        self.member.user_groups.add(group)
        if hasattr(self.member, "_forum_permission_cache"):
            delattr(self.member, "_forum_permission_cache")

        response = self.client.get(
            "/api/tags",
            {"include": "children", "include_hidden": True},
            **self.auth_header(self.member),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload_by_slug = {tag["slug"]: tag for tag in response.json()["data"]}
        self.assertEqual(
            [child["slug"] for child in payload_by_slug[hidden_parent.slug]["children"]],
            [hidden_child.slug],
        )

    def test_public_tag_payload_hides_admin_only_access_fields(self):
        tag = Tag.objects.create(
            name="受限可见",
            slug="restricted-visible",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        group = Group.objects.create(name="RestrictedVisible", color="#4d698e")
        Permission.objects.create(group=group, permission=f"tag{tag.id}.viewForum")
        self.member.user_groups.add(group)

        response = self.client.get(
            f"/api/tags/{tag.id}",
            **self.auth_header(self.member),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertNotIn("is_restricted", payload)
        self.assertNotIn("view_scope", payload)
        self.assertNotIn("start_discussion_scope", payload)
        self.assertNotIn("reply_scope", payload)
        self.assertNotIn("stored_slug", payload)

    def test_tag_editor_payload_includes_stored_slug_without_admin_only_access_fields(self):
        tag = Tag.objects.create(
            name="编辑者可见",
            slug="editor-visible",
            is_restricted=False,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_STAFF,
        )
        group = Group.objects.create(name="TagEditorStoredSlug", color="#4d698e")
        Permission.objects.create(group=group, permission="tag.edit")
        self.member.user_groups.add(group)
        if hasattr(self.member, "_forum_permission_cache"):
            delattr(self.member, "_forum_permission_cache")

        response = self.client.get(
            f"/api/tags/{tag.id}",
            **self.auth_header(self.member),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["stored_slug"], "editor-visible")
        self.assertNotIn("is_restricted", payload)
        self.assertNotIn("view_scope", payload)
        self.assertNotIn("start_discussion_scope", payload)
        self.assertNotIn("reply_scope", payload)

    def test_admin_tag_payload_includes_access_fields(self):
        tag = Tag.objects.create(
            name="后台可见",
            slug="admin-visible",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_STAFF,
        )

        response = self.client.get(
            f"/api/tags/{tag.id}",
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["stored_slug"], "admin-visible")
        self.assertTrue(payload["is_restricted"])
        self.assertEqual(payload["view_scope"], Tag.ACCESS_PUBLIC)
        self.assertEqual(payload["start_discussion_scope"], Tag.ACCESS_MEMBERS)
        self.assertEqual(payload["reply_scope"], Tag.ACCESS_STAFF)

    def test_tag_payload_uses_active_slug_driver_and_keeps_stored_slug_admin_only(self):
        set_active_slug_driver(Tag, "id_with_slug")

        member_response = self.client.get(f"/api/tags/{self.public_tag.id}")
        admin_response = self.client.get(
            f"/api/tags/{self.public_tag.id}",
            **self.auth_header(self.admin),
        )

        self.assertEqual(member_response.status_code, 200, member_response.content)
        self.assertEqual(admin_response.status_code, 200, admin_response.content)
        expected_slug = f"{self.public_tag.id}-{self.public_tag.slug}"
        self.assertEqual(member_response.json()["slug"], expected_slug)
        self.assertNotIn("stored_slug", member_response.json())
        self.assertEqual(admin_response.json()["slug"], expected_slug)
        self.assertEqual(admin_response.json()["stored_slug"], self.public_tag.slug)

    def test_public_tag_children_hide_admin_only_access_fields(self):
        child = Tag.objects.create(
            name="公开子标签",
            slug="public-child-access",
            parent=self.public_tag,
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        group = Group.objects.create(name="RestrictedChildVisible", color="#4d698e")
        Permission.objects.create(group=group, permission=f"tag{child.id}.viewForum")
        self.member.user_groups.add(group)

        response = self.client.get(
            "/api/tags",
            {"include_children": True},
            **self.auth_header(self.member),
        )

        self.assertEqual(response.status_code, 200, response.content)
        parent_payload = next(item for item in response.json()["data"] if item["slug"] == self.public_tag.slug)
        child_payload = next(item for item in parent_payload["children"] if item["id"] == child.id)
        self.assertNotIn("is_restricted", child_payload)
        self.assertNotIn("view_scope", child_payload)
        self.assertNotIn("start_discussion_scope", child_payload)
        self.assertNotIn("reply_scope", child_payload)

    def test_tag_list_hides_child_when_restricted_parent_is_not_visible(self):
        parent = Tag.objects.create(
            name="列表受限父标签",
            slug="list-restricted-parent",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        child = Tag.objects.create(
            name="列表受限子标签",
            slug="list-restricted-child",
            parent=parent,
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        group = Group.objects.create(name="ChildOnlyVisible", color="#4d698e")
        Permission.objects.create(group=group, permission=f"tag{child.id}.viewForum")
        self.member.user_groups.add(group)

        child_only_response = self.client.get(
            "/api/tags",
            {"parent_id": parent.id},
            **self.auth_header(self.member),
        )

        self.assertEqual(child_only_response.status_code, 200, child_only_response.content)
        self.assertNotIn(child.slug, {tag["slug"] for tag in child_only_response.json()["data"]})

        Permission.objects.create(group=group, permission=f"tag{parent.id}.viewForum")
        if hasattr(self.member, "_forum_permission_cache"):
            delattr(self.member, "_forum_permission_cache")
        parent_allowed_response = self.client.get(
            "/api/tags",
            {"parent_id": parent.id},
            **self.auth_header(self.member),
        )

        self.assertEqual(parent_allowed_response.status_code, 200, parent_allowed_response.content)
        self.assertIn(child.slug, {tag["slug"] for tag in parent_allowed_response.json()["data"]})

    def test_tag_payload_exposes_primary_and_child_flags(self):
        child = Tag.objects.create(
            name="子结构",
            slug="structure-child",
            parent=self.public_tag,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )

        parent_response = self.client.get(f"/api/tags/{self.public_tag.id}")
        child_response = self.client.get(f"/api/tags/{child.id}")

        self.assertEqual(parent_response.status_code, 200, parent_response.content)
        self.assertEqual(child_response.status_code, 200, child_response.content)
        parent_payload = parent_response.json()
        child_payload = child_response.json()
        self.assertTrue(parent_payload["is_primary"])
        self.assertFalse(parent_payload["is_child"])
        self.assertTrue(child_payload["is_primary"])
        self.assertTrue(child_payload["is_child"])

    def test_tag_structure_flags_use_central_primary_and_child_helpers(self):
        child = Tag.objects.create(
            name="Helper 子标签",
            slug="helper-child",
            parent=self.public_tag,
        )
        secondary = Tag.objects.create(
            name="Helper 次级标签",
            slug="helper-secondary",
            position=None,
            is_primary=False,
        )

        self.assertTrue(TagService.is_primary_tag(self.public_tag))
        self.assertFalse(TagService.is_child_tag(self.public_tag))
        self.assertFalse(TagService.is_primary_tag(child))
        self.assertTrue(TagService.is_primary_tree_tag(child))
        self.assertTrue(TagService.is_child_tag(child))
        self.assertFalse(TagService.is_primary_tag(secondary))
        self.assertFalse(TagService.is_child_tag(secondary))
        self.assertTrue(child.is_primary)
        self.assertFalse(secondary.is_primary)
        self.assertEqual(
            set(Tag.objects.filter(TagService.primary_tag_filter()).values_list("id", flat=True)),
            {self.public_tag.id, self.members_tag.id, self.staff_tag.id},
        )
        self.assertEqual(
            set(Tag.objects.filter(TagService.secondary_tag_filter()).values_list("id", flat=True)),
            {secondary.id},
        )

    def test_tag_list_orders_primary_before_secondary_roots(self):
        secondary = Tag.objects.create(
            name="A 次级",
            slug="a-secondary",
            position=None,
            is_primary=False,
        )
        primary = Tag.objects.create(
            name="Z 主标签",
            slug="z-primary",
            position=4,
        )

        response = self.client.get("/api/tags")

        self.assertEqual(response.status_code, 200, response.content)
        slugs = [tag["slug"] for tag in response.json()["data"]]
        self.assertLess(slugs.index(primary.slug), slugs.index(secondary.slug))

    def test_creating_secondary_root_tag_exposes_nullable_position(self):
        response = self.client.post(
            "/api/tags",
            data=json.dumps({
                "name": "API 次级标签",
                "slug": "api-secondary",
                "is_primary": False,
            }),
            content_type="application/json",
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertFalse(payload["is_primary"])
        self.assertFalse(payload["is_child"])
        self.assertIsNone(payload["position"])
        tag = Tag.objects.get(id=payload["id"])
        self.assertFalse(tag.is_primary)

    def test_creating_child_under_secondary_root_is_rejected(self):
        secondary = Tag.objects.create(
            name="API 次级父级",
            slug="api-secondary-parent",
            position=None,
            is_primary=False,
        )

        response = self.client.post(
            "/api/tags",
            data=json.dumps({
                "name": "错误子标签",
                "slug": "invalid-secondary-child",
                "parent_id": secondary.id,
            }),
            content_type="application/json",
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("主标签", response.json()["error"])
        self.assertFalse(Tag.objects.filter(slug="invalid-secondary-child").exists())

    def test_tag_create_and_update_accept_parent_relationship_payload_like_flarum(self):
        response = self.client.post(
            "/api/tags",
            data=json.dumps({
                "data": {
                    "type": "tag",
                    "attributes": {
                        "name": "关系子标签",
                        "slug": "relationship-payload-child",
                        "is_primary": True,
                    },
                    "relationships": {
                        "parent": {
                            "data": {"type": "tag", "id": str(self.public_tag.id)},
                        },
                    },
                },
            }),
            content_type="application/json",
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["parent_id"], self.public_tag.id)
        tag = Tag.objects.get(id=payload["id"])
        self.assertEqual(tag.parent_id, self.public_tag.id)

        response = self.client.patch(
            f"/api/tags/{tag.id}",
            data=json.dumps({
                "data": {
                    "type": "tag",
                    "attributes": {"is_primary": True},
                    "relationships": {"parent": {"data": None}},
                },
            }),
            content_type="application/json",
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()["parent_id"])
        tag.refresh_from_db()
        self.assertIsNone(tag.parent_id)

    def test_tag_create_and_update_accept_camel_case_attributes_like_flarum(self):
        response = self.client.post(
            "/api/tags",
            data=json.dumps({
                "data": {
                    "type": "tag",
                    "attributes": {
                        "name": "Camel 标签",
                        "slug": "camel-tag",
                        "defaultSort": "latest",
                        "isHidden": True,
                        "isPrimary": False,
                        "isRestricted": True,
                    },
                },
            }),
            content_type="application/json",
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["default_sort"], "latest")
        self.assertEqual(payload["defaultSort"], "latest")
        self.assertTrue(payload["is_hidden"])
        self.assertTrue(payload["isHidden"])
        self.assertFalse(payload["is_primary"])
        self.assertFalse(payload["isPrimary"])
        self.assertTrue(payload["is_restricted"])
        self.assertTrue(payload["isRestricted"])

        tag = Tag.objects.get(id=payload["id"])
        response = self.client.patch(
            f"/api/tags/{tag.id}",
            data=json.dumps({
                "data": {
                    "type": "tag",
                    "attributes": {
                        "defaultSort": "top",
                        "isHidden": False,
                        "isPrimary": True,
                        "parentId": self.public_tag.id,
                    },
                },
            }),
            content_type="application/json",
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["defaultSort"], "top")
        self.assertFalse(payload["isHidden"])
        self.assertTrue(payload["isPrimary"])
        self.assertEqual(payload["parent_id"], self.public_tag.id)
        tag.refresh_from_db()
        self.assertEqual(tag.default_sort, "top")
        self.assertFalse(tag.is_hidden)
        self.assertTrue(tag.is_primary)
        self.assertEqual(tag.parent_id, self.public_tag.id)

    def test_tag_create_can_return_flarum_jsonapi_document_when_requested(self):
        response = self.client.post(
            "/api/tags",
            data=json.dumps({
                "data": {
                    "type": "tags",
                    "attributes": {
                        "name": "JSONAPI 标签",
                        "slug": "jsonapi-tag",
                        "description": "Flarum shape",
                        "color": "#123456",
                    },
                },
            }),
            content_type="application/json",
            **self.jsonapi_header(self.admin),
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response["Content-Type"], "application/vnd.api+json")
        data = response.json()["data"]
        self.assertEqual(data["type"], "tags")
        self.assertEqual(data["attributes"]["name"], "JSONAPI 标签")
        self.assertEqual(data["attributes"]["slug"], "jsonapi-tag")
        self.assertEqual(data["attributes"]["description"], "Flarum shape")
        self.assertEqual(data["attributes"]["color"], "#123456")
        self.assertEqual(Tag.objects.get(slug="jsonapi-tag").name, "JSONAPI 标签")

    def test_tag_detail_exposes_registered_resource_fields(self):
        with self.captureOnCommitCallbacks(execute=True):
            discussion = create_runtime_discussion(
                title="标签详情附加字段",
                content="用于验证资源注册输出",
                user=self.admin,
                extension_payload=discussion_tags_payload([self.members_tag.id]),
            )

        response = self.client.get(
            f"/api/tags/{self.members_tag.id}",
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertTrue(payload["can_start_discussion"])
        self.assertTrue(payload["canStartDiscussion"])
        self.assertTrue(payload["can_add_to_discussion"])
        self.assertTrue(payload["canAddToDiscussion"])
        self.assertTrue(payload["can_reply"])
        self.assertEqual(payload["last_posted_discussion"]["id"], discussion.id)
        self.assertEqual(payload["lastPostedDiscussion"]["id"], discussion.id)

    def test_tag_detail_can_return_flarum_jsonapi_document_when_requested(self):
        child = Tag.objects.create(
            name="JSONAPI 子标签",
            slug="jsonapi-child",
            parent=self.public_tag,
            position=1,
        )

        response = self.client.get(
            f"/api/tags/{child.id}",
            {"include": "parent"},
            **self.jsonapi_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Content-Type"], "application/vnd.api+json")
        payload = response.json()
        self.assertNotIn("name", payload)
        self.assertEqual(payload["data"]["type"], "tags")
        self.assertEqual(payload["data"]["id"], str(child.id))
        self.assertEqual(payload["data"]["attributes"]["name"], "JSONAPI 子标签")
        self.assertTrue(payload["data"]["attributes"]["isChild"])
        self.assertNotIn("lastPostedDiscussion", payload["data"]["attributes"])
        self.assertEqual(
            payload["data"]["relationships"]["parent"]["data"],
            {"type": "tags", "id": str(self.public_tag.id)},
        )
        self.assertEqual(payload["included"][0]["type"], "tags")

    def test_tag_index_can_return_flarum_jsonapi_document_when_requested(self):
        response = self.client.get(
            "/api/tags",
            **self.jsonapi_header(self.member),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertIn("data", payload)
        self.assertEqual([item["type"] for item in payload["data"]], ["tags", "tags"])
        slugs = [item["attributes"]["slug"] for item in payload["data"]]
        self.assertEqual(slugs, ["public-tag", "members-tag"])

    def test_tag_detail_exposes_flarum_camel_case_base_fields(self):
        self.public_tag.default_sort = "latest"
        self.public_tag.discussion_count = 3
        self.public_tag.is_hidden = True
        self.public_tag.save(update_fields=["default_sort", "discussion_count", "is_hidden"])

        response = self.client.get(
            f"/api/tags/{self.public_tag.id}",
            {"include_hidden": True},
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["defaultSort"], "latest")
        self.assertTrue(payload["isHidden"])
        self.assertTrue(payload["isPrimary"])
        self.assertFalse(payload["isChild"])
        self.assertEqual(payload["discussionCount"], 3)
        self.assertEqual(payload["storedSlug"], self.public_tag.slug)
        self.assertEqual(payload["isRestricted"], self.public_tag.is_restricted)

    def test_tag_detail_resource_field_selection_respects_camel_case_resource_fields(self):
        create_runtime_discussion(
            title="标签 camelCase 字段裁剪",
            content="用于裁剪",
            user=self.admin,
            extension_payload=discussion_tags_payload([self.members_tag.id]),
        )

        response = self.client.get(
            f"/api/tags/{self.members_tag.id}",
            {"fields[tag]": "can_reply"},
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertIn("can_reply", payload)
        self.assertNotIn("can_start_discussion", payload)
        self.assertNotIn("canStartDiscussion", payload)
        self.assertNotIn("can_add_to_discussion", payload)
        self.assertNotIn("canAddToDiscussion", payload)
        self.assertNotIn("last_posted_discussion", payload)
        self.assertNotIn("lastPostedDiscussion", payload)

    def test_tag_detail_supports_flarum_last_posted_discussion_include_name(self):
        discussion = create_runtime_discussion(
            title="标签 camelCase include 讨论",
            content="用于 include",
            user=self.admin,
            extension_payload=discussion_tags_payload([self.members_tag.id]),
        )
        TagService.refresh_tag_stats([self.members_tag.id])

        response = self.client.get(
            f"/api/tags/{self.members_tag.id}",
            {"fields[tag]": "can_reply", "include": "lastPostedDiscussion"},
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertIn("can_reply", payload)
        self.assertIn("lastPostedDiscussion", payload)
        self.assertEqual(payload["lastPostedDiscussion"]["id"], discussion.id)
        self.assertEqual(payload["lastPostedDiscussion"]["last_post_number"], discussion.last_post_number)
        self.assertNotIn("user", payload["lastPostedDiscussion"])

    def test_tag_delete_can_return_flarum_jsonapi_no_content_when_requested(self):
        tag = Tag.objects.create(name="JSONAPI 删除", slug="jsonapi-delete")

        response = self.client.delete(
            f"/api/tags/{tag.id}",
            **self.jsonapi_header(self.admin),
        )

        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(response.content, b"")
        self.assertFalse(Tag.objects.filter(id=tag.id).exists())

    def test_tag_delete_keeps_plain_bias_response_by_default(self):
        tag = Tag.objects.create(name="Plain 删除", slug="plain-delete")

        response = self.client.delete(
            f"/api/tags/{tag.id}",
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["message"], "标签已删除")
        self.assertFalse(Tag.objects.filter(id=tag.id).exists())

    def test_tag_update_without_parent_field_preserves_existing_parent(self):
        child = Tag.objects.create(
            name="保留父级",
            slug="keep-parent-child",
            parent=self.public_tag,
            position=0,
        )

        response = self.client.patch(
            f"/api/tags/{child.id}",
            data=json.dumps({"name": "保留父级更新"}),
            content_type="application/json",
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        child.refresh_from_db()
        self.assertEqual(child.name, "保留父级更新")
        self.assertEqual(child.parent_id, self.public_tag.id)
        self.assertTrue(child.is_primary)
        self.assertTrue(response.json()["is_child"])

    def test_tag_detail_exposes_actor_tag_state_for_authenticated_user(self):
        marked_state = TagService.mark_tag_read(self.members_tag, self.member)

        response = self.client.get(
            f"/api/tags/{self.members_tag.id}",
            **self.auth_header(self.member),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["state"]["is_hidden"], False)
        self.assertIsNotNone(payload["state"]["marked_as_read_at"])
        self.assertEqual(TagState.objects.get(tag=self.members_tag, user=self.member).id, marked_state.id)

    def test_tag_detail_omits_actor_tag_state_for_guest(self):
        response = self.client.get(f"/api/tags/{self.public_tag.id}")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()["state"])

    def test_tag_list_prefetches_actor_tag_state(self):
        state = TagState.objects.create(
            tag=self.public_tag,
            user=self.member,
            is_hidden=True,
        )

        response = self.client.get("/api/tags", **self.auth_header(self.member))

        self.assertEqual(response.status_code, 200, response.content)
        payload_by_slug = {tag["slug"]: tag for tag in response.json()["data"]}
        self.assertEqual(payload_by_slug[self.public_tag.slug]["state"]["is_hidden"], True)
        self.assertIsNone(payload_by_slug[self.public_tag.slug]["state"]["marked_as_read_at"])
        self.assertEqual(state.tag_id, self.public_tag.id)

    def test_restricted_tag_add_to_discussion_uses_tag_specific_permission(self):
        tag = Tag.objects.create(
            name="受限发帖标签",
            slug="restricted-add-to-discussion",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        limited_group = Group.objects.create(name="TagLimited", color="#4d698e")
        Permission.objects.create(group=limited_group, permission="startDiscussion")
        Permission.objects.create(group=limited_group, permission="discussion.reply")
        self.member.user_groups.add(limited_group)

        denied_response = self.client.get(
            f"/api/tags/{tag.id}",
            **self.auth_header(self.member),
        )
        self.assertEqual(denied_response.status_code, 403, denied_response.content)

        Permission.objects.create(group=limited_group, permission=f"tag{tag.id}.viewForum")
        Permission.objects.create(group=limited_group, permission=f"tag{tag.id}.startDiscussion")
        if hasattr(self.member, "_forum_permission_cache"):
            delattr(self.member, "_forum_permission_cache")

        allowed_response = self.client.get(
            f"/api/tags/{tag.id}",
            **self.auth_header(self.member),
        )
        self.assertEqual(allowed_response.status_code, 200, allowed_response.content)
        self.assertTrue(allowed_response.json()["can_start_discussion"])
        self.assertTrue(allowed_response.json()["can_add_to_discussion"])

    def test_restricted_tag_detail_and_slug_require_view_permission(self):
        tag = Tag.objects.create(
            name="受限查看标签",
            slug="restricted-view-tag",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        group = Group.objects.create(name="RestrictedView", color="#4d698e")
        self.member.user_groups.add(group)

        detail_response = self.client.get(
            f"/api/tags/{tag.id}",
            **self.auth_header(self.member),
        )
        slug_response = self.client.get(
            f"/api/tags/slug/{tag.slug}",
            **self.auth_header(self.member),
        )

        self.assertEqual(detail_response.status_code, 403, detail_response.content)
        self.assertEqual(slug_response.status_code, 403, slug_response.content)

        Permission.objects.create(group=group, permission=f"tag{tag.id}.viewForum")
        if hasattr(self.member, "_forum_permission_cache"):
            delattr(self.member, "_forum_permission_cache")

        allowed_response = self.client.get(
            f"/api/tags/{tag.id}",
            **self.auth_header(self.member),
        )

        self.assertEqual(allowed_response.status_code, 200, allowed_response.content)
        self.assertEqual(allowed_response.json()["slug"], tag.slug)

    def test_start_discussion_tag_list_hides_restricted_tags_without_tag_permission(self):
        restricted_tag = Tag.objects.create(
            name="受限选择标签",
            slug="restricted-picker-tag",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        member_group = Group.objects.create(name="RestrictedPicker", color="#4d698e")
        Permission.objects.create(group=member_group, permission="startDiscussion")
        self.member.user_groups.add(member_group)

        denied_response = self.client.get(
            "/api/tags",
            {"purpose": "start_discussion"},
            **self.auth_header(self.member),
        )
        self.assertEqual(denied_response.status_code, 200, denied_response.content)
        self.assertNotIn(restricted_tag.slug, {tag["slug"] for tag in denied_response.json()["data"]})

        Permission.objects.create(group=member_group, permission=f"tag{restricted_tag.id}.startDiscussion")
        if hasattr(self.member, "_forum_permission_cache"):
            delattr(self.member, "_forum_permission_cache")

        allowed_response = self.client.get(
            "/api/tags",
            {"purpose": "start_discussion"},
            **self.auth_header(self.member),
        )
        self.assertEqual(allowed_response.status_code, 200, allowed_response.content)
        self.assertIn(restricted_tag.slug, {tag["slug"] for tag in allowed_response.json()["data"]})

    def test_add_to_discussion_tag_list_keeps_current_tags_without_add_permission(self):
        current_tag = Tag.objects.create(
            name="当前受限标签",
            slug="current-restricted-edit-tag",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        unavailable_tag = Tag.objects.create(
            name="其他受限标签",
            slug="other-restricted-edit-tag",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        member_group = Group.objects.create(name="RestrictedEditPicker", color="#4d698e")
        Permission.objects.create(group=member_group, permission=f"tag{current_tag.id}.viewForum")
        Permission.objects.create(group=member_group, permission=f"tag{unavailable_tag.id}.viewForum")
        Permission.objects.create(group=member_group, permission="startDiscussion")
        self.member.user_groups.add(member_group)
        discussion = create_runtime_discussion(
            title="保留当前标签",
            content="编辑标签时保留已有标签",
            user=self.admin,
            extension_payload=discussion_tags_payload([current_tag.id]),
        )

        response = self.client.get(
            "/api/tags",
            {
                "purpose": "add_to_discussion",
                "discussion_id": discussion.id,
                "include_children": True,
            },
            **self.auth_header(self.member),
        )

        self.assertEqual(response.status_code, 200, response.content)
        slugs = {tag["slug"] for tag in response.json()["data"]}
        self.assertIn(current_tag.slug, slugs)
        self.assertNotIn(unavailable_tag.slug, slugs)

    def test_tag_detail_supports_resource_field_selection(self):
        create_runtime_discussion(
            title="标签字段裁剪",
            content="用于裁剪",
            user=self.admin,
            extension_payload=discussion_tags_payload([self.members_tag.id]),
        )

        response = self.client.get(
            f"/api/tags/{self.members_tag.id}",
            {"fields[tag]": "can_reply"},
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertIn("can_reply", payload)
        self.assertNotIn("can_start_discussion", payload)
        self.assertNotIn("last_posted_discussion", payload)

    def test_tag_detail_supports_resource_include_for_last_posted_discussion(self):
        discussion = create_runtime_discussion(
            title="标签 include 讨论",
            content="用于 include",
            user=self.admin,
            extension_payload=discussion_tags_payload([self.members_tag.id]),
        )
        TagService.refresh_tag_stats([self.members_tag.id])

        response = self.client.get(
            f"/api/tags/{self.members_tag.id}",
            {"fields[tag]": "can_reply", "include": "last_posted_discussion"},
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertIn("can_reply", payload)
        self.assertIn("last_posted_discussion", payload)
        self.assertEqual(payload["last_posted_discussion"]["id"], discussion.id)
        self.assertEqual(payload["last_posted_discussion"]["last_post_number"], discussion.last_post_number)
        self.assertNotIn("user", payload["last_posted_discussion"])

    def test_tag_detail_supports_parent_and_children_resource_includes(self):
        visible_child = Tag.objects.create(
            name="关系子标签",
            slug="relationship-child",
            parent=self.public_tag,
            position=1,
        )
        Tag.objects.create(
            name="隐藏关系子标签",
            slug="relationship-hidden-child",
            parent=self.public_tag,
            is_hidden=True,
            position=2,
        )

        parent_response = self.client.get(
            f"/api/tags/{self.public_tag.id}",
            {"include": "children"},
        )
        child_response = self.client.get(
            f"/api/tags/{visible_child.id}",
            {"include": "parent"},
        )

        self.assertEqual(parent_response.status_code, 200, parent_response.content)
        self.assertEqual(child_response.status_code, 200, child_response.content)
        parent_payload = parent_response.json()
        child_payload = child_response.json()
        self.assertEqual([item["slug"] for item in parent_payload["children"]], ["relationship-child"])
        self.assertEqual(child_payload["parent"]["id"], self.public_tag.id)
        self.assertEqual(child_payload["parent"]["slug"], self.public_tag.slug)

    def test_tag_index_default_includes_parent_relationship(self):
        child = Tag.objects.create(
            name="默认父关系子标签",
            slug="default-parent-child",
            parent=self.public_tag,
        )

        response = self.client.get("/api/tags", {"parent_id": self.public_tag.id})

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()["data"][0]
        self.assertEqual(payload["id"], child.id)
        self.assertEqual(payload["parent"]["id"], self.public_tag.id)
        self.assertEqual(payload["parent"]["slug"], self.public_tag.slug)

    def test_tag_index_default_parent_include_uses_select_related(self):
        for index in range(4):
            Tag.objects.create(
                name=f"默认父关系批量子标签 {index}",
                slug=f"default-parent-batch-child-{index}",
                parent=self.public_tag,
                position=index,
            )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/tags", {"parent_id": self.public_tag.id})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(all(item["parent"]["id"] == self.public_tag.id for item in response.json()["data"]))
        per_child_parent_queries = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and f'"tags"."id" = {self.public_tag.id}' in query["sql"]
        ]
        self.assertEqual(
            per_child_parent_queries,
            [],
            "Tag index default parent include should use select_related instead of querying once per child tag.",
        )

    def test_tag_policy_parent_ability_uses_prefetched_parent(self):
        children = [
            Tag.objects.create(
                name=f"策略父级批量子标签 {index}",
                slug=f"policy-parent-batch-child-{index}",
                parent=self.public_tag,
                position=index,
            )
            for index in range(5)
        ]
        loaded_children = list(Tag.objects.select_related("parent").filter(id__in=[tag.id for tag in children]))

        with CaptureQueriesContext(connection) as queries:
            decisions = [
                TagService.can_tag_ability(tag, self.member, "view")
                for tag in loaded_children
            ]

        self.assertTrue(all(decisions))
        per_child_parent_queries = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and f'"tags"."id" = {self.public_tag.id}' in query["sql"]
        ]
        self.assertEqual(
            per_child_parent_queries,
            [],
            "Tag policy parent inheritance should use the prefetched parent instead of querying once per child tag.",
        )

    def test_tag_list_children_include_uses_prefetched_children(self):
        for index in range(5):
            Tag.objects.create(
                name=f"批量关系子标签 {index}",
                slug=f"relationship-batch-child-{index}",
                parent=self.public_tag,
                position=index,
            )
        second_parent = Tag.objects.create(name="第二批量父标签", slug="relationship-second-parent")
        Tag.objects.create(
            name="第二批量子标签",
            slug="relationship-second-child",
            parent=second_parent,
        )

        with patch(
            "bias_ext_tags.backend.services.TagService.get_forbidden_tag_ids",
            wraps=TagService.get_forbidden_tag_ids,
        ) as get_forbidden_tag_ids, CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/tags", {"include": "children"})

        self.assertEqual(response.status_code, 200, response.content)
        public_tag = next(tag for tag in response.json()["data"] if tag["slug"] == self.public_tag.slug)
        second_parent_payload = next(tag for tag in response.json()["data"] if tag["slug"] == second_parent.slug)
        self.assertEqual(len(public_tag["children"]), 5)
        self.assertEqual([tag["slug"] for tag in second_parent_payload["children"]], ["relationship-second-child"])
        get_forbidden_tag_ids.assert_called_once()
        per_parent_child_queries = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and re.search(r'where .*"tags"\."parent_id"\s*=\s*\d+', query["sql"].lower())
        ]
        self.assertEqual(
            per_parent_child_queries,
            [],
            "Tag children include should use the prefetched child collection instead of querying per parent tag.",
        )

    def test_tag_list_skips_child_prefetch_when_children_are_not_requested(self):
        Tag.objects.create(
            name="无需预加载子标签",
            slug="no-prefetch-child",
            parent=self.public_tag,
        )

        with patch(
            "bias_ext_tags.backend.handlers.TagService.get_forbidden_tag_ids",
            wraps=TagService.get_forbidden_tag_ids,
        ) as get_forbidden_tag_ids, CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/tags", {"include_children": False})

        self.assertEqual(response.status_code, 200, response.content)
        public_tag = next(tag for tag in response.json()["data"] if tag["slug"] == self.public_tag.slug)
        self.assertEqual(public_tag["children"], [])
        get_forbidden_tag_ids.assert_not_called()
        child_prefetch_queries = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and re.search(r'where .*"tags"\."parent_id" in', query["sql"].lower())
        ]
        self.assertEqual(
            child_prefetch_queries,
            [],
            "Tag list should not prefetch children when no child payload or include is requested.",
        )

    def test_tag_slug_detail_accepts_id_with_slug_url(self):
        response = self.client.get(f"/api/tags/slug/{self.public_tag.id}-renamed")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["slug"], "public-tag")

    def test_tag_detail_accepts_slug_on_primary_show_route(self):
        response = self.client.get("/api/tags/public-tag")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["id"], self.public_tag.id)

    def test_tag_detail_accepts_id_with_slug_on_primary_show_route(self):
        response = self.client.get(f"/api/tags/{self.public_tag.id}-renamed")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["id"], self.public_tag.id)

    def test_tag_slug_detail_is_case_insensitive_for_default_slug(self):
        response = self.client.get("/api/tags/slug/PUBLIC-TAG")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["slug"], "public-tag")

    def test_tag_detail_static_route_uses_resource_endpoint_mutator(self):
        def mutate_endpoint(endpoint):
            def response_callback(context, response):
                callback = endpoint.response_callback
                payload = callback(context, response) if callback is not None else response
                payload["mutated_by_resource_endpoint"] = True
                return payload

            return ResourceEndpointDefinition(
                resource=endpoint.resource,
                endpoint=endpoint.endpoint,
                module_id="test",
                handler=endpoint.handler,
                kind=endpoint.kind,
                ability=endpoint.ability,
                methods=endpoint.methods,
                response_callback=response_callback,
            )

        registry = ResourceRegistry()
        registry.register_resource(TagResource())
        registry.register_endpoint(
            ResourceEndpointDefinition(
                resource="tag",
                endpoint="show",
                module_id="test",
                operation="mutate",
                mutator=mutate_endpoint,
            )
        )

        with patch("bias_ext_tags.backend.handlers.get_runtime_resource_registry", return_value=registry):
            with patch("bias_core.resource_dispatcher.get_runtime_resource_registry", return_value=registry):
                response = self.client.get(
                    f"/api/tags/{self.members_tag.id}",
                    **self.auth_header(self.admin),
                )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["mutated_by_resource_endpoint"])

    def test_tag_slug_detail_static_route_uses_resource_endpoint_mutator(self):
        def mutate_endpoint(endpoint):
            def response_callback(context, response):
                callback = endpoint.response_callback
                payload = callback(context, response) if callback is not None else response
                payload["mutated_by_resource_endpoint"] = True
                return payload

            return ResourceEndpointDefinition(
                resource=endpoint.resource,
                endpoint=endpoint.endpoint,
                module_id="test",
                handler=endpoint.handler,
                kind=endpoint.kind,
                ability=endpoint.ability,
                methods=endpoint.methods,
                response_callback=response_callback,
            )

        registry = ResourceRegistry()
        registry.register_resource(TagResource())
        registry.register_endpoint(
            ResourceEndpointDefinition(
                resource="tag",
                endpoint="show-by-slug",
                module_id="test",
                operation="mutate",
                mutator=mutate_endpoint,
            )
        )

        with patch("bias_ext_tags.backend.handlers.get_runtime_resource_registry", return_value=registry):
            with patch("bias_core.resource_dispatcher.get_runtime_resource_registry", return_value=registry):
                response = self.client.get(
                    f"/api/tags/slug/{self.members_tag.slug}",
                    **self.auth_header(self.admin),
                )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["mutated_by_resource_endpoint"])

    def test_runtime_registry_registers_tag_as_database_resource_object(self):
        registry = get_resource_registry()
        resource_object = registry.get_resource_object("tag")

        self.assertIsInstance(resource_object, TagResource)
        self.assertEqual(resource_object.model, Tag)
        self.assertEqual(resource_object.type(), "tag")
        self.assertEqual(resource_object.resource_for(self.public_tag, {"user": self.admin}), "tag")

        by_id = resource_object.find(str(self.public_tag.id), {"user": self.member})
        by_slug = resource_object.find(self.public_tag.slug, {"user": self.member})
        set_active_slug_driver(Tag, "id_with_slug")
        by_id_slug = resource_object.find(f"{self.public_tag.id}-renamed", {"user": self.member})
        restricted = resource_object.find(str(self.staff_tag.id), {"user": self.member})

        self.assertEqual(by_id.id, self.public_tag.id)
        self.assertEqual(by_slug.id, self.public_tag.id)
        self.assertEqual(by_id_slug.id, self.public_tag.id)
        self.assertEqual(restricted.id, self.staff_tag.id)
        with self.assertRaisesMessage(PermissionDenied, "没有权限查看此标签"):
            resource_object.can(self.member, "view", restricted, {})

    def test_tag_resource_object_owns_public_resource_endpoint_definitions(self):
        registry = ResourceRegistry()
        registry.register_resource(TagResource())

        routes = {
            (endpoint.endpoint, endpoint.path, endpoint.handler is not None)
            for endpoint in registry.get_dispatch_endpoints("tag")
        }

        self.assertIn(("index", "/tags", False), routes)
        self.assertIn(("show", "/tags/{object_id}", False), routes)
        self.assertIn(("show-by-slug", "/tags/slug/{object_id}", False), routes)
        self.assertIn(("create", "/tags", False), routes)
        self.assertIn(("update", "/tags/{object_id}", False), routes)
        self.assertIn(("delete", "/tags/{object_id}", False), routes)
        endpoints = {
            endpoint.endpoint: endpoint
            for endpoint in registry.get_dispatch_endpoints("tag")
        }
        self.assertEqual(endpoints["create"].kind, "create")
        self.assertEqual(endpoints["index"].kind, "index")
        self.assertEqual(endpoints["show-by-slug"].kind, "show")
        self.assertEqual(endpoints["show-by-slug"].ability, "view")
        self.assertEqual(endpoints["update"].kind, "update")

    def test_tag_resource_object_owns_flarum_writable_field_contract(self):
        registry = ResourceRegistry()
        registry.register_resource(TagResource())
        fields = {
            field.field: field
            for field in registry.get_effective_fields("tag", {"user": self.admin})
        }
        relationships = {
            relationship.relationship: relationship
            for relationship in registry.get_effective_relationships("tag", {"user": self.admin})
        }

        self.assertTrue(fields["name"].writable)
        self.assertTrue(fields["name"].required_on_create)
        self.assertTrue(_resource_field_has_rule(fields["name"], ("max_length", 100)))
        self.assertTrue(fields["description"].nullable)
        self.assertTrue(_resource_field_has_rule(fields["description"], ("max_length", 700)))
        self.assertTrue(fields["slug"].writable)
        self.assertTrue(fields["slug"].required_on_create)
        self.assertTrue(_resource_field_has_rule(fields["slug"], ("max_length", 100)))
        self.assertTrue(any(_resource_field_rule_value(rule)[0] == "regex" for rule in fields["slug"].validation_rules))
        self.assertTrue(fields["color"].writable)
        self.assertTrue(fields["color"].nullable)
        self.assertTrue(fields["isHidden"].writable)
        self.assertTrue(fields["isPrimary"].writable)
        self.assertTrue(fields["isRestricted"].writable)
        self.assertTrue(fields["defaultSort"].writable)
        self.assertTrue(fields["parent_id"].writable)
        self.assertTrue(fields["parentId"].writable)
        self.assertEqual(TagResource().jsonapi_types(), ("tag", "tags"))
        self.assertEqual(relationships["parent"].resource_type, "tag")
        self.assertFalse(relationships["parent"].many)
        self.assertTrue(relationships["parent"].nullable)
        self.assertTrue(relationships["parent"].writable(None, {"payload": {"data": {"attributes": {"isPrimary": True}}}}))
        self.assertFalse(relationships["parent"].writable(None, {"payload": {"data": {"attributes": {"isPrimary": False}}}}))
        self.assertEqual(relationships["children"].resource_type, "tag")
        self.assertTrue(relationships["children"].many)
        self.assertEqual(relationships["lastPostedDiscussion"].resource_type, "discussion")

    def test_tag_resource_payload_applies_flarum_writable_fields_and_parent_relationship(self):
        registry = ResourceRegistry()
        registry.register_resource(TagResource())
        tag = Tag()
        context = {
            "user": self.admin,
            "payload": {
                "data": {
                    "type": "tag",
                    "attributes": {
                        "name": "Pipeline 标签",
                        "slug": "pipeline-tag",
                        "description": None,
                        "color": "#abc",
                        "icon": None,
                        "isHidden": True,
                        "isPrimary": True,
                        "defaultSort": "latest",
                    },
                    "relationships": {
                        "parent": {"data": {"type": "tag", "id": str(self.public_tag.id)}},
                    },
                },
            },
        }

        registry.apply_resource_payload(
            "tag",
            tag,
            {
                "name": "Pipeline 标签",
                "slug": "pipeline-tag",
                "description": None,
                "color": "#abc",
                "icon": None,
                "isHidden": True,
                "isPrimary": True,
                "defaultSort": "latest",
            },
            context,
            creating=True,
        )

        self.assertEqual(tag.name, "Pipeline 标签")
        self.assertEqual(tag.slug, "pipeline-tag")
        self.assertIsNone(tag.description)
        self.assertEqual(tag.color, "#abc")
        self.assertIsNone(tag.icon)
        self.assertTrue(tag.is_hidden)
        self.assertTrue(tag.is_primary)
        self.assertEqual(tag.default_sort, "latest")
        self.assertEqual(tag.parent_id, self.public_tag.id)

    def test_tag_resource_payload_validates_flarum_schema_rules(self):
        registry = ResourceRegistry()
        registry.register_resource(TagResource())

        with self.assertRaisesMessage(JsonApiValidationError, "缺少必填字段: name"):
            registry.apply_resource_payload(
                "tag",
                Tag(),
                {"slug": "missing-name"},
                {
                    "user": self.admin,
                    "payload": {"data": {"type": "tag", "attributes": {"slug": "missing-name"}}},
                },
                creating=True,
            )

        with self.assertRaisesMessage(JsonApiValidationError, "slug format is invalid"):
            registry.apply_resource_payload(
                "tag",
                Tag(),
                {"name": "Invalid Slug", "slug": "has space"},
                {
                    "user": self.admin,
                    "payload": {
                        "data": {
                            "type": "tag",
                            "attributes": {"name": "Invalid Slug", "slug": "has space"},
                        },
                    },
                },
                creating=True,
            )

        with self.assertRaisesMessage(JsonApiValidationError, "isHidden must be a boolean"):
            registry.apply_resource_payload(
                "tag",
                Tag(),
                {"name": "Bool", "slug": "bool", "isHidden": "yes"},
                {
                    "user": self.admin,
                    "payload": {
                        "data": {
                            "type": "tag",
                            "attributes": {"name": "Bool", "slug": "bool", "isHidden": "yes"},
                        },
                    },
                },
                creating=True,
            )

        with self.assertRaisesMessage(JsonApiValidationError, "color must be a valid hex color"):
            registry.apply_resource_payload(
                "tag",
                Tag(),
                {"name": "Color", "slug": "color", "color": "red"},
                {
                    "user": self.admin,
                    "payload": {
                        "data": {
                            "type": "tag",
                            "attributes": {"name": "Color", "slug": "color", "color": "red"},
                        },
                    },
                },
                creating=True,
            )

        tag = Tag()
        registry.apply_resource_payload(
            "tag",
            tag,
            {"name": "Restricted", "slug": "restricted", "isRestricted": True},
            {
                "user": self.admin,
                "payload": {
                    "data": {
                        "type": "tag",
                        "attributes": {
                            "name": "Restricted",
                            "slug": "restricted",
                            "isRestricted": True,
                        },
                    },
                },
            },
            creating=True,
        )
        self.assertTrue(tag.is_restricted)

    def test_tag_resource_object_serializes_related_tag_models_through_core_jsonapi_serializer(self):
        child = Tag.objects.create(
            name="资源对象子标签",
            slug="resource-object-child",
            parent=self.public_tag,
            position=1,
        )
        registry = get_resource_registry()

        document = registry.serialize_jsonapi_document(
            "tag",
            child,
            {"user": self.admin},
            include=("parent",),
        )

        self.assertEqual(document["data"]["type"], "tag")
        self.assertEqual(document["data"]["relationships"]["parent"]["data"], {"type": "tag", "id": str(self.public_tag.id)})
        self.assertEqual(document["included"][0]["type"], "tag")
        self.assertEqual(document["included"][0]["id"], str(self.public_tag.id))

    def test_guest_cannot_view_staff_tag_detail(self):
        response = self.client.get(f"/api/tags/{self.staff_tag.id}")

        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("没有权限", response.json()["error"])

    def test_tag_detail_jsonapi_error_response_uses_jsonapi_errors_document(self):
        response = self.client.get(
            f"/api/tags/{self.staff_tag.id}",
            **self.jsonapi_header(),
        )

        self.assertEqual(response.status_code, 403, response.content)
        payload = response.json()
        self.assertIn("errors", payload)
        self.assertEqual(payload["errors"][0]["status"], "403")
        self.assertIn("没有权限", payload["errors"][0]["detail"])

    def test_tag_read_endpoints_do_not_refresh_stats(self):
        with patch("bias_ext_tags.backend.handlers.TagService.refresh_tag_stats") as refresh_stats:
            list_response = self.client.get("/api/tags")
            popular_response = self.client.get("/api/tags/popular")

        self.assertEqual(list_response.status_code, 200, list_response.content)
        self.assertEqual(popular_response.status_code, 200, popular_response.content)
        refresh_stats.assert_not_called()

    def test_tag_list_reuses_forbidden_tag_context_for_children(self):
        Tag.objects.create(
            name="公开子标签",
            slug="public-child",
            parent=self.public_tag,
        )
        Tag.objects.create(
            name="内部子标签",
            slug="staff-child",
            parent=self.public_tag,
            view_scope=Tag.ACCESS_STAFF,
            start_discussion_scope=Tag.ACCESS_STAFF,
            reply_scope=Tag.ACCESS_STAFF,
        )

        with patch(
            "bias_ext_tags.backend.handlers.TagService.get_forbidden_tag_ids",
            wraps=TagService.get_forbidden_tag_ids,
        ) as get_forbidden_tag_ids:
            response = self.client.get("/api/tags", {"include_children": True})

        self.assertEqual(response.status_code, 200, response.content)
        public_tag = next(tag for tag in response.json()["data"] if tag["slug"] == "public-tag")
        self.assertEqual([tag["slug"] for tag in public_tag["children"]], ["public-child"])
        self.assertEqual(get_forbidden_tag_ids.call_count, 1)

    def test_tag_children_payload_is_limited_to_direct_children(self):
        child = Tag.objects.create(
            name="直接子标签",
            slug="direct-child",
            parent=self.public_tag,
        )
        Tag.objects.create(
            name="历史孙级标签",
            slug="legacy-grandchild",
            parent=child,
        )

        list_response = self.client.get("/api/tags", {"include_children": True})
        detail_response = self.client.get(f"/api/tags/{self.public_tag.id}")
        include_response = self.client.get("/api/tags", {"include": "children"})

        self.assertEqual(list_response.status_code, 200, list_response.content)
        self.assertEqual(detail_response.status_code, 200, detail_response.content)
        self.assertEqual(include_response.status_code, 200, include_response.content)
        list_public_tag = next(tag for tag in list_response.json()["data"] if tag["slug"] == "public-tag")
        include_public_tag = next(tag for tag in include_response.json()["data"] if tag["slug"] == "public-tag")
        list_child = next(tag for tag in list_public_tag["children"] if tag["slug"] == "direct-child")
        detail_child = next(tag for tag in detail_response.json()["children"] if tag["slug"] == "direct-child")
        include_child_slugs = [tag["slug"] for tag in include_public_tag["children"]]
        self.assertEqual(list_child["children"], [])
        self.assertEqual(detail_child["children"], [])
        self.assertEqual(include_child_slugs, ["direct-child"])


class TagForumSettingsTests(ExtensionRuntimeTestMixin, TestCase):
    def setUp(self):
        self.bootstrap_extensions("tags")
        clear_runtime_setting_caches()
        self.admin = User.objects.create_superuser(
            username="tag-forum-admin",
            email="tag-forum-admin@example.com",
            password="password123",
        )
        self.member = User.objects.create_user(
            username="tag-forum-member",
            email="tag-forum-member@example.com",
            password="password123",
            is_email_confirmed=True,
        )

    def tearDown(self):
        clear_runtime_setting_caches()
        super().tearDown()

    def auth_header(self, user=None):
        token = RefreshToken.for_user(user or self.admin).access_token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_extension_detail_api_surfaces_registered_resources_for_tags_extension(self):
        response = self.client.get(
            "/api/admin/extensions/tags",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()["extension"]
        self.assertGreaterEqual(payload["capability_summary"]["resource_field_count"], 1)
        self.assertTrue(any(item["module_id"] == "tags" for item in payload["resource_fields"]))
        self.assertTrue(
            any(
                item["module_id"] == "tags"
                and item["resource"] == "forum"
                and item["field"] == "tags"
                for item in payload["resource_fields"]
            )
        )
        self.assertTrue(
            any(
                item["module_id"] == "tags"
                and item["resource"] == "post"
                and item["relationship"] == "eventPostMentionsTags"
                for item in payload["resource_relationships"]
            )
        )
        self.assertTrue(
            any(
                item["module_id"] == "tags"
                and item["model"] == "Post"
                and item["name"] == "eventPostMentionsTags"
                for item in payload["model_relations"]
            )
        )
        self.assertTrue(
            any(
                item["module_id"] == "tags"
                and item["model"] == "Tag"
                and item["model_label"] == "tags.Tag"
                for item in payload["owned_models"]
            )
        )
        self.assertTrue(
            any(
                item["module_id"] == "tags"
                and item["model"] == "DiscussionTag"
                and item["model_label"] == "tags.DiscussionTag"
                for item in payload["owned_models"]
            )
        )
        self.assertGreaterEqual(payload["capability_summary"]["event_listener_count"], 1)
        self.assertTrue(
            any(
                item["event"] == "DiscussionTaggedEvent"
                and item["module_id"] == "tags"
                and item.get("source") == "runtime"
                for item in payload["event_listeners"]
            )
        )

    def test_tags_extension_adds_forum_show_default_includes(self):
        endpoint = get_resource_registry().get_dispatch_endpoint("forum", "show", "GET")

        self.assertIsNotNone(endpoint)
        self.assertIn("tags", endpoint.default_include)
        self.assertIn("tags.parent", endpoint.default_include)

    def test_public_forum_settings_expose_tags_forum_resource_fields(self):
        parent = Tag.objects.create(
            name="Announcements",
            slug="announcements",
            position=1,
        )
        Tag.objects.create(
            name="News",
            slug="news",
            parent=parent,
            position=1,
        )
        Tag.objects.create(
            name="Staff",
            slug="staff",
            view_scope=Tag.ACCESS_STAFF,
            position=2,
        )

        guest_response = self.client.get("/api/forum")
        self.assertEqual(guest_response.status_code, 200, guest_response.content)
        guest_payload = guest_response.json()
        tags_extension = next(item for item in guest_payload["enabled_extensions"] if item["id"] == "tags")
        self.assertEqual(tags_extension["frontend_forum_entry"], "extensions/tags/frontend/forum/index.js")
        frontend_routes = {route["name"]: route for route in tags_extension["frontend_routes"]}
        self.assertEqual(frontend_routes["tags"]["path"], "/tags")
        self.assertEqual(frontend_routes["tags"]["component"], "./TagsView.vue")
        self.assertIn(
            "/api/tags?include_children=true",
            {item["href"] for item in frontend_routes["tags"]["preloads"]},
        )
        self.assertEqual(frontend_routes["tag-detail"]["path"], "/t/:slug")
        self.assertIn(
            "/api/discussions/?page=1&limit=20&tag=:slug",
            {item["href"] for item in frontend_routes["tag-detail"]["preloads"]},
        )
        self.assertFalse(guest_payload["can_bypass_tag_counts"])
        self.assertEqual([item["slug"] for item in guest_payload["tags"]], ["announcements"])
        self.assertEqual(guest_payload["tags"][0]["children"], [])

        staff_response = self.client.get("/api/forum", **self.auth_header())
        self.assertEqual(staff_response.status_code, 200, staff_response.content)
        staff_payload = staff_response.json()
        self.assertTrue(staff_payload["can_bypass_tag_counts"])
        self.assertEqual(
            [item["slug"] for item in staff_payload["tags"]],
            ["announcements", "staff"],
        )

    def test_forum_settings_include_primary_and_limited_secondary_root_tags(self):
        primary = Tag.objects.create(name="Forum Primary", slug="forum-primary", position=1)
        Tag.objects.create(name="Forum Child Hidden From Summary", slug="forum-child-summary", parent=primary, position=1)
        secondary_tags = [
            Tag.objects.create(
                name=f"Forum Secondary {index}",
                slug=f"forum-secondary-{index}",
                position=None,
                is_primary=False,
                discussion_count=10 - index,
            )
            for index in range(5)
        ]

        response = self.client.get("/api/forum")

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        slugs = [item["slug"] for item in payload["tags"]]
        self.assertEqual(slugs, ["forum-primary", *[tag.slug for tag in secondary_tags[:4]]])
        self.assertTrue(all(item["parent_id"] is None for item in payload["tags"]))
        self.assertTrue(all(item["children"] == [] for item in payload["tags"]))

    def test_public_forum_settings_use_bypass_tag_counts_permission(self):
        bypass_group = Group.objects.create(name="BypassTagCounts", color="#4d698e")
        Permission.objects.create(group=bypass_group, permission="bypassTagCounts")
        self.member.user_groups.add(bypass_group)

        response = self.client.get("/api/forum", **self.auth_header(self.member))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["can_bypass_tag_counts"])

    def test_forum_tag_summary_prefetches_last_posted_discussions(self):
        permission_group = Group.objects.create(name="ForumTagTreePosting", color="#4d698e")
        Permission.objects.create(group=permission_group, permission="startDiscussion")
        Permission.objects.create(group=permission_group, permission="startDiscussionWithoutApproval")
        self.member.user_groups.add(permission_group)
        tags = [
            Tag.objects.create(
                name=f"Forum Summary Tag {index}",
                slug=f"forum-summary-tag-{index}",
                position=index,
            )
            for index in range(6)
        ]
        for tag in tags:
            Permission.objects.create(group=permission_group, permission=f"tag{tag.id}.startDiscussion")
        for tag in tags:
            with self.captureOnCommitCallbacks(execute=True):
                create_runtime_discussion(
                    title=f"Forum summary discussion {tag.id}",
                    content="Forum summary last posted discussion.",
                    user=self.member,
                    extension_payload=discussion_tags_payload([tag.id]),
                )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/forum", **self.auth_header(self.member))

        self.assertEqual(response.status_code, 200, response.content)
        payload_tags = response.json()["tags"]
        self.assertTrue(
            all(item["last_posted_discussion"] for item in payload_tags)
        )
        tag_discussion_fetches = [
            query["sql"]
            for query in queries
            if 'from "discussions"' in query["sql"].lower()
            and 'where "discussions"."id" =' in query["sql"].lower()
        ]
        self.assertEqual(
            tag_discussion_fetches,
            [],
            "Forum tag summary should not issue per-tag last_posted_discussion lookups.",
        )
        tag_parent_fetches = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and 'where "tags"."id" =' in query["sql"].lower()
        ]
        self.assertEqual(
            tag_parent_fetches,
            [],
            "Forum tag summary should not issue per-tag parent lookups while resolving permissions.",
        )


class TagSearchApiTests(ExtensionRuntimeTestMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="tag-search-user",
            email="tag-search-user@example.com",
            password="password123",
            is_email_confirmed=True,
        )

    def auth_header(self, user=None):
        token = RefreshToken.for_user(user or self.user).access_token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_search_api_hides_discussions_in_staff_only_tags(self):
        admin = User.objects.create_superuser(
            username="search-admin",
            email="search-admin@example.com",
            password="password123",
        )
        hidden_tag = Tag.objects.create(
            name="管理搜索区",
            slug="search-staff",
            view_scope=Tag.ACCESS_STAFF,
            start_discussion_scope=Tag.ACCESS_STAFF,
            reply_scope=Tag.ACCESS_STAFF,
        )
        create_runtime_discussion(
            title="搜索内网讨论",
            content="这里有搜索关键字",
            user=admin,
            extension_payload=discussion_tags_payload([hidden_tag.id]),
        )

        guest_response = self.client.get("/api/search", {"q": "搜索", "type": "discussions"})
        self.assertEqual(guest_response.status_code, 200, guest_response.content)
        self.assertEqual(guest_response.json()["discussion_total"], 0)
        self.assertEqual(guest_response.json()["discussions"], [])

        admin_response = self.client.get(
            "/api/search",
            {"q": "搜索", "type": "discussions"},
            **self.auth_header(admin),
        )
        self.assertEqual(admin_response.status_code, 200, admin_response.content)
        self.assertGreaterEqual(admin_response.json()["discussion_total"], 1)

    def test_search_api_supports_registered_tag_filter_syntax(self):
        target_tag = Tag.objects.create(name="扩展搜索标签", slug="extension-search-tag")
        other_tag = Tag.objects.create(name="其他标签", slug="other-search-tag")
        matched = create_runtime_discussion(
            title="模块搜索过滤命中",
            content="使用注册式过滤器检索标签。",
            user=self.user,
            extension_payload=discussion_tags_payload([target_tag.id]),
        )
        create_runtime_discussion(
            title="模块搜索过滤未命中",
            content="同样包含搜索关键字，但标签不同。",
            user=self.user,
            extension_payload=discussion_tags_payload([other_tag.id]),
        )

        response = self.client.get(
            "/api/search",
            {"q": "搜索 tag:extension-search-tag", "type": "discussions"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["discussion_total"], 1)
        self.assertEqual([item["id"] for item in payload["discussions"]], [matched.id])

    def test_search_api_tag_filter_matches_slug_case_insensitively(self):
        target_tag = Tag.objects.create(name="大小写搜索标签", slug="case-search-tag")
        other_tag = Tag.objects.create(name="大小写其他标签", slug="case-other-tag")
        matched = create_runtime_discussion(
            title="大小写标签搜索命中",
            content="case-insensitive-tag-keyword",
            user=self.user,
            extension_payload=discussion_tags_payload([target_tag.id]),
        )
        create_runtime_discussion(
            title="大小写标签搜索未命中",
            content="case-insensitive-tag-keyword",
            user=self.user,
            extension_payload=discussion_tags_payload([other_tag.id]),
        )

        response = self.client.get(
            "/api/search",
            {"q": "case-insensitive-tag-keyword tag:CASE-SEARCH-TAG", "type": "discussions"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["discussion_total"], 1)
        self.assertEqual([item["id"] for item in payload["discussions"]], [matched.id])

    def test_search_api_tag_filter_supports_comma_or_slugs(self):
        first_tag = Tag.objects.create(name="搜索标签一", slug="search-filter-one")
        second_tag = Tag.objects.create(name="搜索标签二", slug="search-filter-two")
        other_tag = Tag.objects.create(name="搜索标签三", slug="search-filter-three")
        first_discussion = create_runtime_discussion(
            title="批量标签搜索命中一",
            content="shared-filter-keyword",
            user=self.user,
            extension_payload=discussion_tags_payload([first_tag.id]),
        )
        second_discussion = create_runtime_discussion(
            title="批量标签搜索命中二",
            content="shared-filter-keyword",
            user=self.user,
            extension_payload=discussion_tags_payload([second_tag.id]),
        )
        create_runtime_discussion(
            title="批量标签搜索未命中",
            content="shared-filter-keyword",
            user=self.user,
            extension_payload=discussion_tags_payload([other_tag.id]),
        )

        response = self.client.get(
            "/api/search",
            {"q": "shared-filter-keyword tag:search-filter-one,search-filter-two", "type": "discussions"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["discussion_total"], 2)
        self.assertEqual(
            {item["id"] for item in payload["discussions"]},
            {first_discussion.id, second_discussion.id},
        )

    def test_search_api_tag_filter_supports_negated_slug(self):
        excluded_tag = Tag.objects.create(name="搜索排除标签", slug="search-excluded-tag")
        included_tag = Tag.objects.create(name="搜索保留标签", slug="search-included-tag")
        create_runtime_discussion(
            title="排除标签讨论",
            content="shared-negated-filter-keyword",
            user=self.user,
            extension_payload=discussion_tags_payload([excluded_tag.id]),
        )
        matched = create_runtime_discussion(
            title="保留标签讨论",
            content="shared-negated-filter-keyword",
            user=self.user,
            extension_payload=discussion_tags_payload([included_tag.id]),
        )

        response = self.client.get(
            "/api/search",
            {"q": "shared-negated-filter-keyword -tag:search-excluded-tag", "type": "discussions"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["discussion_total"], 1)
        self.assertEqual([item["id"] for item in payload["discussions"]], [matched.id])

    def test_search_api_tag_filter_supports_negated_untagged(self):
        tagged = Tag.objects.create(name="搜索保留已标记", slug="search-keep-tagged")
        matched = create_runtime_discussion(
            title="已标记保留讨论",
            content="shared-negated-untagged-keyword",
            user=self.user,
            extension_payload=discussion_tags_payload([tagged.id]),
        )
        create_runtime_discussion(
            title="未标记排除讨论",
            content="shared-negated-untagged-keyword",
            user=self.user,
        )

        response = self.client.get(
            "/api/search",
            {"q": "shared-negated-untagged-keyword -tag:untagged", "type": "discussions"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["discussion_total"], 1)
        self.assertEqual([item["id"] for item in payload["discussions"]], [matched.id])

    def test_search_api_posts_tag_filter_supports_negated_slug(self):
        excluded_tag = Tag.objects.create(name="帖子排除标签", slug="post-excluded-tag")
        included_tag = Tag.objects.create(name="帖子保留标签", slug="post-included-tag")
        excluded_discussion = create_runtime_discussion(
            title="帖子排除讨论",
            content="excluded starter",
            user=self.user,
            extension_payload=discussion_tags_payload([excluded_tag.id]),
        )
        included_discussion = create_runtime_discussion(
            title="帖子保留讨论",
            content="included starter",
            user=self.user,
            extension_payload=discussion_tags_payload([included_tag.id]),
        )
        create_runtime_post(
            discussion_id=excluded_discussion.id,
            content="shared-negated-post-keyword",
            user=self.user,
        )
        matched_post = create_runtime_post(
            discussion_id=included_discussion.id,
            content="shared-negated-post-keyword",
            user=self.user,
        )

        response = self.client.get(
            "/api/search",
            {"q": "shared-negated-post-keyword -tag:post-excluded-tag", "type": "posts"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["post_total"], 1)
        self.assertEqual([item["id"] for item in payload["posts"]], [matched_post.id])

    def test_search_api_tag_filter_supports_untagged_without_slug_lookup(self):
        tagged = Tag.objects.create(name="搜索已标记", slug="search-tagged")
        create_runtime_discussion(
            title="搜索已标记讨论",
            content="shared-untagged-keyword",
            user=self.user,
            extension_payload=discussion_tags_payload([tagged.id]),
        )
        untagged_discussion = create_runtime_discussion(
            title="搜索未标记讨论",
            content="shared-untagged-keyword",
            user=self.user,
        )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                "/api/search",
                {"q": "shared-untagged-keyword tag:untagged", "type": "discussions"},
                **self.auth_header(),
            )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["discussion_total"], 1)
        self.assertEqual([item["id"] for item in payload["discussions"]], [untagged_discussion.id])
        slug_resolution_queries = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and '"slug" in' in query["sql"].lower()
        ]
        self.assertEqual(slug_resolution_queries, [])

    def test_search_api_multiple_tag_filters_resolve_slugs_with_one_lookup(self):
        primary_tag = Tag.objects.create(name="搜索交集主标签", slug="search-and-primary")
        secondary_tag = Tag.objects.create(
            name="搜索交集次标签",
            slug="search-and-secondary",
            position=None,
            is_primary=False,
        )
        matched = create_runtime_discussion(
            title="搜索标签交集命中",
            content="shared-and-keyword",
            user=self.user,
            extension_payload=discussion_tags_payload([primary_tag.id, secondary_tag.id]),
        )
        create_runtime_discussion(
            title="搜索标签交集缺一",
            content="shared-and-keyword",
            user=self.user,
            extension_payload=discussion_tags_payload([primary_tag.id]),
        )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                "/api/search",
                {"q": "shared-and-keyword tag:search-and-primary tag:search-and-secondary", "type": "discussions"},
                **self.auth_header(),
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([item["id"] for item in response.json()["discussions"]], [matched.id])
        slug_resolution_queries = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and '"slug" in' in query["sql"].lower()
        ]
        self.assertEqual(
            len(slug_resolution_queries),
            1,
            f"Expected one batched slug lookup across repeated tag filters, got {len(slug_resolution_queries)}: {slug_resolution_queries}",
        )

    def test_search_api_tag_filter_uses_active_slug_driver(self):
        set_active_slug_driver(Tag, "id_with_slug")
        first_tag = Tag.objects.create(name="搜索 ID 标签一", slug="search-id-filter-one")
        second_tag = Tag.objects.create(name="搜索 ID 标签二", slug="search-id-filter-two")
        first_discussion = create_runtime_discussion(
            title="ID 标签搜索命中一",
            content="active-slug-keyword",
            user=self.user,
            extension_payload=discussion_tags_payload([first_tag.id]),
        )
        second_discussion = create_runtime_discussion(
            title="ID 标签搜索命中二",
            content="active-slug-keyword",
            user=self.user,
            extension_payload=discussion_tags_payload([second_tag.id]),
        )

        response = self.client.get(
            "/api/search",
            {"q": f"active-slug-keyword tag:{first_tag.id}-renamed,{second_tag.id}", "type": "discussions"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            {item["id"] for item in response.json()["discussions"]},
            {first_discussion.id, second_discussion.id},
        )

    def test_search_api_posts_support_registered_tag_filter_syntax(self):
        target_tag = Tag.objects.create(name="帖子搜索标签", slug="post-search-tag")
        other_tag = Tag.objects.create(name="帖子其他标签", slug="post-other-tag")
        matched_discussion = create_runtime_discussion(
            title="帖子标签搜索命中讨论",
            content="首帖内容",
            user=self.user,
            extension_payload=discussion_tags_payload([target_tag.id]),
        )
        other_discussion = create_runtime_discussion(
            title="帖子标签搜索未命中讨论",
            content="首帖内容",
            user=self.user,
            extension_payload=discussion_tags_payload([other_tag.id]),
        )
        matched_post = create_runtime_post(
            discussion_id=matched_discussion.id,
            content="帖子标签搜索关键字",
            user=self.user,
        )
        create_runtime_post(
            discussion_id=other_discussion.id,
            content="帖子标签搜索关键字",
            user=self.user,
        )

        response = self.client.get(
            "/api/search",
            {"q": "帖子标签搜索关键字 tag:post-search-tag", "type": "posts"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["post_total"], 1)
        self.assertEqual([item["id"] for item in payload["posts"]], [matched_post.id])

    def test_search_api_posts_tag_filter_supports_comma_or_slugs(self):
        first_tag = Tag.objects.create(name="帖子搜索标签一", slug="post-filter-one")
        second_tag = Tag.objects.create(name="帖子搜索标签二", slug="post-filter-two")
        other_tag = Tag.objects.create(name="帖子搜索标签三", slug="post-filter-three")
        first_discussion = create_runtime_discussion(
            title="帖子标签并集命中一",
            content="首帖内容",
            user=self.user,
            extension_payload=discussion_tags_payload([first_tag.id]),
        )
        second_discussion = create_runtime_discussion(
            title="帖子标签并集命中二",
            content="首帖内容",
            user=self.user,
            extension_payload=discussion_tags_payload([second_tag.id]),
        )
        other_discussion = create_runtime_discussion(
            title="帖子标签并集未命中",
            content="首帖内容",
            user=self.user,
            extension_payload=discussion_tags_payload([other_tag.id]),
        )
        first_post = create_runtime_post(
            discussion_id=first_discussion.id,
            content="post-or-filter-keyword",
            user=self.user,
        )
        second_post = create_runtime_post(
            discussion_id=second_discussion.id,
            content="post-or-filter-keyword",
            user=self.user,
        )
        create_runtime_post(
            discussion_id=other_discussion.id,
            content="post-or-filter-keyword",
            user=self.user,
        )

        response = self.client.get(
            "/api/search",
            {"q": "post-or-filter-keyword tag:post-filter-one,post-filter-two", "type": "posts"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["post_total"], 2)
        self.assertEqual({item["id"] for item in payload["posts"]}, {first_post.id, second_post.id})

    def test_search_api_posts_tag_filter_supports_untagged_without_slug_lookup(self):
        tagged = Tag.objects.create(name="帖子已标记", slug="post-tagged")
        tagged_discussion = create_runtime_discussion(
            title="帖子已标记讨论",
            content="首帖内容",
            user=self.user,
            extension_payload=discussion_tags_payload([tagged.id]),
        )
        untagged_discussion = create_runtime_discussion(
            title="帖子未标记讨论",
            content="首帖内容",
            user=self.user,
        )
        create_runtime_post(
            discussion_id=tagged_discussion.id,
            content="post-untagged-keyword",
            user=self.user,
        )
        matched_post = create_runtime_post(
            discussion_id=untagged_discussion.id,
            content="post-untagged-keyword",
            user=self.user,
        )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                "/api/search",
                {"q": "post-untagged-keyword tag:untagged", "type": "posts"},
                **self.auth_header(),
            )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["post_total"], 1)
        self.assertEqual([item["id"] for item in payload["posts"]], [matched_post.id])
        slug_resolution_queries = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and '"slug" in' in query["sql"].lower()
        ]
        self.assertEqual(slug_resolution_queries, [])

    def test_search_api_posts_multiple_tag_filters_resolve_slugs_with_one_lookup(self):
        primary_tag = Tag.objects.create(name="帖子交集主标签", slug="post-and-primary")
        secondary_tag = Tag.objects.create(
            name="帖子交集次标签",
            slug="post-and-secondary",
            position=None,
            is_primary=False,
        )
        matched_discussion = create_runtime_discussion(
            title="帖子标签交集命中",
            content="首帖内容",
            user=self.user,
            extension_payload=discussion_tags_payload([primary_tag.id, secondary_tag.id]),
        )
        partial_discussion = create_runtime_discussion(
            title="帖子标签交集缺一",
            content="首帖内容",
            user=self.user,
            extension_payload=discussion_tags_payload([primary_tag.id]),
        )
        matched_post = create_runtime_post(
            discussion_id=matched_discussion.id,
            content="post-and-filter-keyword",
            user=self.user,
        )
        create_runtime_post(
            discussion_id=partial_discussion.id,
            content="post-and-filter-keyword",
            user=self.user,
        )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                "/api/search",
                {"q": "post-and-filter-keyword tag:post-and-primary tag:post-and-secondary", "type": "posts"},
                **self.auth_header(),
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([item["id"] for item in response.json()["posts"]], [matched_post.id])
        slug_resolution_queries = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and '"slug" in' in query["sql"].lower()
        ]
        self.assertEqual(
            len(slug_resolution_queries),
            1,
            f"Expected one batched slug lookup across repeated post tag filters, got {len(slug_resolution_queries)}: {slug_resolution_queries}",
        )

    def test_search_filters_api_exposes_registered_tag_filter_syntax(self):
        response = self.client.get("/api/search/filters", {"target": "discussions"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("tag:<slug>", {item["syntax"] for item in response.json()["filters"]})

    def test_search_filters_api_exposes_registered_post_tag_filter_syntax(self):
        response = self.client.get("/api/search/filters", {"target": "posts"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("tag:<slug>", {item["syntax"] for item in response.json()["filters"]})

    def test_search_filters_api_accepts_registered_tag_target(self):
        response = self.client.get("/api/search/filters", {"target": "tag"})

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["target"], "tag")
        self.assertEqual(payload["filters"], [])

    def test_tags_extension_registers_tag_search_target(self):
        application = self.bootstrap_extensions("tags")

        self.assertIn("search.target.tag", application.get_service_provider_keys(extension_id="tags"))
        provider = application.get_service("search.target.tag")
        self.assertEqual(provider["model"].__name__, "Tag")
        self.assertEqual(provider["resource"], "tag")
        self.assertEqual(provider["results_key"], "tags")

    def test_search_api_tags_type_matches_name_or_slug_prefix(self):
        matched_by_name = Tag.objects.create(name="Support", slug="help")
        matched_by_slug = Tag.objects.create(name="Docs", slug="support-docs")
        Tag.objects.create(name="Community Support", slug="community-support")

        response = self.client.get(
            "/api/search",
            {"q": "sup", "type": "tag"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["type"], "tag")
        self.assertEqual(payload["tag_total"], 2)
        self.assertEqual({item["id"] for item in payload["tags"]}, {matched_by_name.id, matched_by_slug.id})

    def test_search_api_tags_type_uses_structural_ordering(self):
        secondary = Tag.objects.create(
            name="Support Secondary",
            slug="support-secondary",
            position=None,
            is_primary=False,
        )
        primary = Tag.objects.create(
            name="Support Primary",
            slug="support-primary",
            position=0,
            is_primary=True,
        )
        later_primary = Tag.objects.create(
            name="Support Later",
            slug="support-later",
            position=1,
            is_primary=True,
        )

        response = self.client.get(
            "/api/search",
            {"q": "support", "type": "tag"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            [item["id"] for item in response.json()["tags"]],
            [primary.id, later_primary.id, secondary.id],
        )

    def test_tag_fulltext_search_uses_id_subquery(self):
        application = self.bootstrap_extensions("tags")
        matched_by_name = Tag.objects.create(name="Subquery Support", slug="subquery-help")
        matched_by_slug = Tag.objects.create(name="Docs", slug="subquery-support")
        Tag.objects.create(name="Community Support", slug="community-support")

        with CaptureQueriesContext(connection) as queries:
            result = application.search.query(
                Tag,
                Tag.objects.all(),
                ResourceSearchCriteria(filters={"q": "subquery"}, resource="tag"),
                {"user": self.user},
            )
            result_ids = set(result.results.values_list("id", flat=True))

        self.assertEqual(result_ids, {matched_by_name.id, matched_by_slug.id})
        sql = " ".join(query["sql"].lower() for query in queries)
        self.assertRegex(sql, r'"tags"\."id"\s+in\s+\(select')

    def test_search_api_all_includes_tag_section(self):
        Tag.objects.create(name="Support", slug="support")

        response = self.client.get(
            "/api/search",
            {"q": "sup", "type": "all"},
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["tag_total"], 1)
        self.assertEqual(len(payload["tags"]), 1)
        self.assertEqual(payload["tags"][0]["slug"], "support")

    def test_search_api_tags_type_respects_tag_visibility(self):
        Tag.objects.create(name="Support", slug="support")
        Tag.objects.create(
            name="Support Staff",
            slug="support-staff",
            view_scope=Tag.ACCESS_STAFF,
            start_discussion_scope=Tag.ACCESS_STAFF,
            reply_scope=Tag.ACCESS_STAFF,
        )

        response = self.client.get("/api/search", {"q": "sup", "type": "tag"})

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["tag_total"], 1)
        self.assertEqual([item["slug"] for item in payload["tags"]], ["support"])


class TagDiscussionForumApiTests(ExtensionRuntimeTestMixin, TestCase):
    def _pre_setup(self):
        super()._pre_setup()
        self.bootstrap_extensions("tags")

    def setUp(self):
        self.author = User.objects.create_user(
            username="tag-discussion-author",
            email="tag-discussion-author@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        self.reader = User.objects.create_user(
            username="tag-discussion-reader",
            email="tag-discussion-reader@example.com",
            password="password123",
            is_email_confirmed=True,
        )

    def auth_header(self, user=None):
        token = RefreshToken.for_user(user or self.author).access_token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_discussion_list_filters_by_tag_slug(self):
        life_tag = Tag.objects.create(name="生活", slug="life", color="#4d698e")
        tech_tag = Tag.objects.create(name="技术", slug="tech", color="#3498db")

        life_discussion = create_runtime_discussion(
            title="Life discussion",
            content="Only belongs to life.",
            user=self.author,
            extension_payload=discussion_tags_payload([life_tag.id]),
        )
        create_runtime_discussion(
            title="Tech discussion",
            content="Only belongs to tech.",
            user=self.author,
            extension_payload=discussion_tags_payload([tech_tag.id]),
        )

        response = self.client.get("/api/discussions/", {"tag": life_tag.slug})

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(len(payload["data"]), 1)
        self.assertEqual(payload["data"][0]["id"], life_discussion.id)
        self.assertEqual(payload["data"][0]["tags"][0]["slug"], life_tag.slug)

    def test_discussion_detail_exposes_can_tag_field(self):
        tag = Tag.objects.create(name="可改标签", slug="can-tag-discussion")
        discussion = create_runtime_discussion(
            title="Can tag discussion",
            content="用于验证讨论资源 can_tag 字段。",
            user=self.author,
            extension_payload=discussion_tags_payload([tag.id]),
        )

        author_response = self.client.get(
            f"/api/discussions/{discussion.id}",
            **self.auth_header(self.author),
        )
        reader_response = self.client.get(
            f"/api/discussions/{discussion.id}",
            **self.auth_header(self.reader),
        )

        self.assertEqual(author_response.status_code, 200, author_response.content)
        self.assertEqual(reader_response.status_code, 200, reader_response.content)
        self.assertTrue(author_response.json()["can_tag"])
        self.assertFalse(reader_response.json()["can_tag"])

    def test_discussion_list_accepts_json_api_filter_tag_param(self):
        matched_tag = Tag.objects.create(name="JSON API 标签", slug="json-api-filter")
        other_tag = Tag.objects.create(name="其他 JSON API 标签", slug="json-api-other")
        matched = create_runtime_discussion(
            title="JSON API 标签命中",
            content="兼容 JSON API filter[tag] 查询参数。",
            user=self.author,
            extension_payload=discussion_tags_payload([matched_tag.id]),
        )
        create_runtime_discussion(
            title="JSON API 标签未命中",
            content="不应被 filter[tag] 命中。",
            user=self.author,
            extension_payload=discussion_tags_payload([other_tag.id]),
        )

        response = self.client.get("/api/discussions/", {"filter[tag]": matched_tag.slug})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual([item["id"] for item in response.json()["data"]], [matched.id])

    def test_discussion_list_tag_filter_supports_comma_or_slugs(self):
        first_tag = Tag.objects.create(name="列表标签一", slug="list-filter-one")
        second_tag = Tag.objects.create(name="列表标签二", slug="list-filter-two")
        other_tag = Tag.objects.create(name="列表标签三", slug="list-filter-three")
        first_discussion = create_runtime_discussion(
            title="列表标签命中一",
            content="按多个标签筛选。",
            user=self.author,
            extension_payload=discussion_tags_payload([first_tag.id]),
        )
        second_discussion = create_runtime_discussion(
            title="列表标签命中二",
            content="按多个标签筛选。",
            user=self.author,
            extension_payload=discussion_tags_payload([second_tag.id]),
        )
        create_runtime_discussion(
            title="列表标签未命中",
            content="按多个标签筛选。",
            user=self.author,
            extension_payload=discussion_tags_payload([other_tag.id]),
        )

        response = self.client.get("/api/discussions/", {"tag": "list-filter-one,list-filter-two"})

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["total"], 2)
        self.assertEqual(
            {item["id"] for item in payload["data"]},
            {first_discussion.id, second_discussion.id},
        )

    def test_discussion_list_tag_filter_supports_multiple_and_groups(self):
        first_tag = Tag.objects.create(name="列表交集标签一", slug="list-and-one")
        second_tag = Tag.objects.create(
            name="列表交集标签二",
            slug="list-and-two",
            position=None,
            is_primary=False,
        )
        other_tag = Tag.objects.create(name="列表交集标签三", slug="list-and-three")
        matched = create_runtime_discussion(
            title="列表标签交集命中",
            content="同时拥有两个标签。",
            user=self.author,
            extension_payload=discussion_tags_payload([first_tag.id, second_tag.id]),
        )
        create_runtime_discussion(
            title="列表标签交集缺一",
            content="只拥有第一个标签。",
            user=self.author,
            extension_payload=discussion_tags_payload([first_tag.id]),
        )
        create_runtime_discussion(
            title="列表标签交集无关",
            content="拥有其他标签。",
            user=self.author,
            extension_payload=discussion_tags_payload([other_tag.id]),
        )

        response = self.client.get(
            "/api/discussions/?tag=list-and-one&tag=list-and-two",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual([item["id"] for item in response.json()["data"]], [matched.id])

    def test_discussion_list_tag_filter_resolves_multiple_slugs_with_one_lookup(self):
        tags = [
            Tag.objects.create(name=f"查询标签 {index}", slug=f"query-filter-{index}")
            for index in range(3)
        ]
        for tag in tags:
            create_runtime_discussion(
                title=f"查询标签讨论 {tag.id}",
                content="用于查询数量防线。",
                user=self.author,
                extension_payload=discussion_tags_payload([tag.id]),
            )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                "/api/discussions/",
                {"tag": "query-filter-0,query-filter-1,query-filter-2"},
            )

        self.assertEqual(response.status_code, 200, response.content)
        slug_resolution_queries = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and '"slug" in' in query["sql"].lower()
        ]
        self.assertEqual(
            len(slug_resolution_queries),
            1,
            f"Expected one batched slug lookup, got {len(slug_resolution_queries)}: {slug_resolution_queries}",
        )

    def test_discussion_list_serializes_tags_from_prefetched_links(self):
        tags = [
            Tag.objects.create(name=f"列表序列标签 {index}", slug=f"list-serialize-tag-{index}")
            for index in range(3)
        ]
        for index in range(8):
            create_runtime_discussion(
                title=f"列表序列讨论 {index}",
                content="用于验证讨论列表标签序列化查询数。",
                user=self.author,
                extension_payload=discussion_tags_payload([tags[index % len(tags)].id]),
            )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/discussions/")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertGreaterEqual(len(response.json()["data"]), 8)
        per_discussion_tag_queries = [
            query["sql"]
            for query in queries
            if re.search(r'\bfrom\s+["`]?discussion_tag["`]?', query["sql"].lower())
            and re.search(r'["`]?discussion_id["`]?\s*=\s*\d+\b', query["sql"].lower())
        ]
        self.assertEqual(
            per_discussion_tag_queries,
            [],
            "Discussion list tag serialization should reuse prefetched tag links instead of querying per discussion.",
        )

    def test_discussion_list_default_includes_tag_parent_relationship(self):
        parent = Tag.objects.create(name="讨论默认父标签", slug="discussion-default-parent", position=1)
        child = Tag.objects.create(
            name="讨论默认子标签",
            slug="discussion-default-child",
            parent=parent,
            position=None,
            is_primary=False,
        )
        discussion = create_runtime_discussion(
            title="Discussion default tag parent include",
            content="用于验证讨论列表默认展开 tags.parent。",
            user=self.author,
            extension_payload=discussion_tags_payload([parent.id, child.id]),
        )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/discussions/")

        self.assertEqual(response.status_code, 200, response.content)
        payload = next(item for item in response.json()["data"] if item["id"] == discussion.id)
        child_payload = next(item for item in payload["tags"] if item["id"] == child.id)
        self.assertEqual(child_payload["parent"]["id"], parent.id)
        self.assertEqual(child_payload["parent"]["slug"], parent.slug)
        per_tag_parent_queries = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and f'"tags"."id" = {parent.id}' in query["sql"]
        ]
        self.assertEqual(
            per_tag_parent_queries,
            [],
            "Discussion list tags.parent default include should prefetch tag parents.",
        )

    def test_post_list_nested_discussion_tags_include_uses_prefetched_links(self):
        tags = [
            Tag.objects.create(
                name="帖子讨论主标签",
                slug="post-discussion-primary-tag",
                position=1,
                is_primary=True,
            ),
            Tag.objects.create(
                name="帖子讨论次标签",
                slug="post-discussion-secondary-tag",
                position=None,
                is_primary=False,
            ),
        ]
        discussion = create_runtime_discussion(
            title="Post list discussion tags include",
            content="用于验证帖子列表嵌套讨论标签 include。",
            user=self.author,
            extension_payload=discussion_tags_payload([tag.id for tag in tags]),
        )
        for index in range(5):
            create_runtime_post(
                discussion_id=discussion.id,
                content=f"帖子列表嵌套标签回复 {index}",
                user=self.author,
            )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                f"/api/discussions/{discussion.id}/posts",
                {"include": "discussion.tags", "limit": 10},
                **self.auth_header(self.reader),
            )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertGreaterEqual(len(payload["data"]), 5)
        self.assertTrue(
            all(
                [item["slug"] for item in post["discussion"]["tags"]] == [tag.slug for tag in tags]
                for post in payload["data"]
            )
        )
        per_post_tag_queries = [
            query["sql"]
            for query in queries
            if re.search(r'\bfrom\s+["`]?discussion_tag["`]?', query["sql"].lower())
            and re.search(r'\bwhere\s+["`]?discussion_tag["`]?\.\s*["`]?discussion_id["`]?\s*=\s*\d+\b', query["sql"].lower())
        ]
        self.assertEqual(
            per_post_tag_queries,
            [],
            "Post list nested discussion.tags include should reuse the prefetched discussion tag links.",
        )

    def test_discussion_list_tag_filter_uses_active_slug_driver(self):
        set_active_slug_driver(Tag, "id_with_slug")
        first_tag = Tag.objects.create(name="列表 ID 标签一", slug="list-id-filter-one")
        second_tag = Tag.objects.create(name="列表 ID 标签二", slug="list-id-filter-two")
        first_discussion = create_runtime_discussion(
            title="ID 标签列表命中一",
            content="按当前 slug driver 筛选。",
            user=self.author,
            extension_payload=discussion_tags_payload([first_tag.id]),
        )
        second_discussion = create_runtime_discussion(
            title="ID 标签列表命中二",
            content="按当前 slug driver 筛选。",
            user=self.author,
            extension_payload=discussion_tags_payload([second_tag.id]),
        )

        response = self.client.get(
            "/api/discussions/",
            {"tag": f"{first_tag.id}-renamed,{second_tag.id}"},
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            {item["id"] for item in response.json()["data"]},
            {first_discussion.id, second_discussion.id},
        )

    def test_discussion_list_tag_filter_hides_invisible_tag_slug(self):
        staff_tag = Tag.objects.create(
            name="过滤管理标签",
            slug="staff-filter-tag",
            view_scope=Tag.ACCESS_STAFF,
            start_discussion_scope=Tag.ACCESS_STAFF,
            reply_scope=Tag.ACCESS_STAFF,
        )
        admin = User.objects.create_superuser(
            username="staff-filter-admin",
            email="staff-filter-admin@example.com",
            password="password123",
        )
        create_runtime_discussion(
            title="管理标签过滤讨论",
            content="访客不能通过 tag filter 绕过可见性。",
            user=admin,
            extension_payload=discussion_tags_payload([staff_tag.id]),
        )

        guest_response = self.client.get("/api/discussions/", {"tag": staff_tag.slug})
        self.assertEqual(guest_response.status_code, 200, guest_response.content)
        self.assertEqual(guest_response.json()["total"], 0)
        self.assertEqual(guest_response.json()["data"], [])

        admin_response = self.client.get(
            "/api/discussions/",
            {"tag": staff_tag.slug},
            **self.auth_header(admin),
        )
        self.assertEqual(admin_response.status_code, 200, admin_response.content)
        self.assertEqual(admin_response.json()["total"], 1)

    def test_discussion_list_tag_filter_allows_restricted_tag_with_view_permission(self):
        restricted_tag = Tag.objects.create(
            name="受限过滤标签",
            slug="restricted-filter-tag",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        admin = User.objects.create_superuser(
            username="restricted-filter-admin",
            email="restricted-filter-admin@example.com",
            password="password123",
        )
        discussion = create_runtime_discussion(
            title="受限标签过滤讨论",
            content="只有有 tag view 权限的用户可筛选。",
            user=admin,
            extension_payload=discussion_tags_payload([restricted_tag.id]),
        )
        member_group = Group.objects.create(name="RestrictedFilter", color="#4d698e")
        Permission.objects.create(group=member_group, permission="viewForum")
        self.reader.user_groups.add(member_group)

        denied_response = self.client.get(
            "/api/discussions/",
            {"tag": restricted_tag.slug},
            **self.auth_header(self.reader),
        )
        self.assertEqual(denied_response.status_code, 200, denied_response.content)
        self.assertEqual(denied_response.json()["total"], 0)

        Permission.objects.create(group=member_group, permission=f"tag{restricted_tag.id}.viewForum")
        if hasattr(self.reader, "_forum_permission_cache"):
            delattr(self.reader, "_forum_permission_cache")

        allowed_response = self.client.get(
            "/api/discussions/",
            {"tag": restricted_tag.slug},
            **self.auth_header(self.reader),
        )
        self.assertEqual(allowed_response.status_code, 200, allowed_response.content)
        self.assertEqual([item["id"] for item in allowed_response.json()["data"]], [discussion.id])

    def test_discussion_detail_hides_restricted_tag_without_view_permission(self):
        restricted_tag = Tag.objects.create(
            name="受限详情标签",
            slug="restricted-detail-view",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        member_group = Group.objects.create(name="RestrictedDetailReader", color="#4d698e")
        Permission.objects.create(group=member_group, permission="viewForum")
        self.reader.user_groups.add(member_group)
        admin = User.objects.create_superuser(
            username="restricted-detail-admin",
            email="restricted-detail-admin@example.com",
            password="password123",
        )
        discussion = create_runtime_discussion(
            title="受限详情讨论",
            content="详情页也必须尊重 tag viewForum 权限。",
            user=admin,
            extension_payload=discussion_tags_payload([restricted_tag.id]),
        )

        denied_response = self.client.get(
            f"/api/discussions/{discussion.id}",
            **self.auth_header(self.reader),
        )

        self.assertEqual(denied_response.status_code, 404, denied_response.content)

        Permission.objects.create(group=member_group, permission=f"tag{restricted_tag.id}.viewForum")
        if hasattr(self.reader, "_forum_permission_cache"):
            delattr(self.reader, "_forum_permission_cache")

        allowed_response = self.client.get(
            f"/api/discussions/{discussion.id}",
            **self.auth_header(self.reader),
        )

        self.assertEqual(allowed_response.status_code, 200, allowed_response.content)
        self.assertEqual(allowed_response.json()["id"], discussion.id)

    def test_discussion_list_active_slug_driver_respects_tag_visibility(self):
        set_active_slug_driver(Tag, "id_with_slug")
        staff_tag = Tag.objects.create(
            name="ID 管理过滤标签",
            slug="id-staff-filter-tag",
            view_scope=Tag.ACCESS_STAFF,
            start_discussion_scope=Tag.ACCESS_STAFF,
            reply_scope=Tag.ACCESS_STAFF,
        )
        admin = User.objects.create_superuser(
            username="id-staff-filter-admin",
            email="id-staff-filter-admin@example.com",
            password="password123",
        )
        create_runtime_discussion(
            title="ID 管理标签过滤讨论",
            content="active driver 也不能绕过可见性。",
            user=admin,
            extension_payload=discussion_tags_payload([staff_tag.id]),
        )

        response = self.client.get("/api/discussions/", {"tag": f"{staff_tag.id}-renamed"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["total"], 0)
        self.assertEqual(response.json()["data"], [])

    def test_discussion_list_active_slug_driver_resolves_multiple_slugs_with_one_lookup(self):
        from bias_ext_tags.backend.search import apply_discussion_tag_list_query

        set_active_slug_driver(Tag, "id_with_slug")
        tags = [
            Tag.objects.create(name=f"ID 查询标签 {index}", slug=f"id-query-filter-{index}")
            for index in range(3)
        ]
        discussions = []
        for tag in tags:
            discussions.append(create_runtime_discussion(
                title=f"ID 查询标签讨论 {tag.id}",
                content="用于 active driver 查询数量防线。",
                user=self.author,
                extension_payload=discussion_tags_payload([tag.id]),
            ))

        with CaptureQueriesContext(connection) as queries:
            queryset = apply_discussion_tag_list_query(
                discussions[0].__class__.objects.all(),
                {
                    "user": self.author,
                    "params": {"tag": ",".join(f"{tag.id}-renamed" for tag in tags)},
                },
            )
            result_ids = set(queryset.values_list("id", flat=True))

        self.assertEqual(result_ids, {discussion.id for discussion in discussions})
        id_resolution_queries = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and '"id" in' in query["sql"].lower()
        ]
        self.assertEqual(
            len(id_resolution_queries),
            1,
            f"Expected one batched id slug lookup, got {len(id_resolution_queries)}: {id_resolution_queries}",
        )

    def test_discussion_list_tag_filter_unknown_slug_returns_empty_result(self):
        Tag.objects.create(name="存在标签", slug="known-filter-tag")
        create_runtime_discussion(
            title="不会被未知标签命中",
            content="未知标签应返回空列表。",
            user=self.author,
        )

        response = self.client.get("/api/discussions/", {"tag": "missing-filter-tag"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["total"], 0)
        self.assertEqual(response.json()["data"], [])

    def test_discussion_list_tag_filter_matches_slug_case_insensitively(self):
        tag = Tag.objects.create(name="列表大小写标签", slug="case-list-tag")
        matched = create_runtime_discussion(
            title="列表大小写命中",
            content="按大小写不敏感 slug 筛选。",
            user=self.author,
            extension_payload=discussion_tags_payload([tag.id]),
        )
        create_runtime_discussion(
            title="列表大小写未命中",
            content="不应被标签筛选命中。",
            user=self.author,
        )

        response = self.client.get("/api/discussions/", {"tag": "CASE-LIST-TAG"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["data"][0]["id"], matched.id)

    def test_discussion_list_tag_filter_supports_untagged(self):
        tagged = Tag.objects.create(name="已标记", slug="tagged-filter")
        create_runtime_discussion(
            title="已标记讨论",
            content="不应出现在 untagged 过滤中。",
            user=self.author,
            extension_payload=discussion_tags_payload([tagged.id]),
        )
        untagged_discussion = create_runtime_discussion(
            title="未标记讨论",
            content="应出现在 untagged 过滤中。",
            user=self.author,
        )

        response = self.client.get("/api/discussions/", {"tag": "untagged"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["data"][0]["id"], untagged_discussion.id)

    def test_all_discussions_list_hides_discussions_in_hidden_tags_by_default(self):
        public_tag = Tag.objects.create(name="公开", slug="public-list")
        hidden_tag = Tag.objects.create(name="隐藏", slug="hidden-list", is_hidden=True)
        visible_discussion = create_runtime_discussion(
            title="公开标签讨论",
            content="普通列表可见",
            user=self.author,
            extension_payload=discussion_tags_payload([public_tag.id]),
        )
        hidden_discussion = create_runtime_discussion(
            title="隐藏标签讨论",
            content="默认全部列表隐藏",
            user=self.author,
            extension_payload=discussion_tags_payload([hidden_tag.id]),
        )

        response = self.client.get("/api/discussions/")

        self.assertEqual(response.status_code, 200, response.content)
        ids = [item["id"] for item in response.json()["data"]]
        self.assertIn(visible_discussion.id, ids)
        self.assertNotIn(hidden_discussion.id, ids)

    def test_hidden_tag_discussions_are_visible_when_explicitly_filtering_that_tag(self):
        hidden_tag = Tag.objects.create(name="隐藏", slug="hidden-filter", is_hidden=True)
        discussion = create_runtime_discussion(
            title="隐藏标签过滤讨论",
            content="显式标签筛选可见",
            user=self.author,
            extension_payload=discussion_tags_payload([hidden_tag.id]),
        )

        response = self.client.get("/api/discussions/", {"tag": hidden_tag.slug})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([item["id"] for item in response.json()["data"]], [discussion.id])

    def test_hidden_tag_discussions_are_visible_when_filter_tag_param_is_active(self):
        hidden_tag = Tag.objects.create(name="隐藏", slug="hidden-filter-json-api", is_hidden=True)
        discussion = create_runtime_discussion(
            title="隐藏标签 JSON API 过滤讨论",
            content="filter[tag] 也应禁用全部列表隐藏标签排除。",
            user=self.author,
            extension_payload=discussion_tags_payload([hidden_tag.id]),
        )

        response = self.client.get("/api/discussions/", {"filter[tag]": hidden_tag.slug})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([item["id"] for item in response.json()["data"]], [discussion.id])

    def test_hidden_tag_discussions_are_not_hidden_from_fulltext_discussion_list_search(self):
        hidden_tag = Tag.objects.create(name="隐藏", slug="hidden-search-list", is_hidden=True)
        discussion = create_runtime_discussion(
            title="隐藏标签全文检索讨论",
            content="unique-hidden-tag-search-keyword",
            user=self.author,
            extension_payload=discussion_tags_payload([hidden_tag.id]),
        )

        response = self.client.get("/api/discussions/", {"q": "unique-hidden-tag-search-keyword"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([item["id"] for item in response.json()["data"]], [discussion.id])

    def test_hidden_tag_discussions_are_not_hidden_when_search_filter_is_active(self):
        hidden_tag = Tag.objects.create(name="隐藏", slug="hidden-token-list", is_hidden=True)
        discussion = create_runtime_discussion(
            title="隐藏标签 token 过滤讨论",
            content="search filter token should disable default hidden tag exclusion.",
            user=self.author,
            extension_payload=discussion_tags_payload([hidden_tag.id]),
        )

        def parse_filter(token):
            return "yes" if token == "token:yes" else None

        def apply_filter(queryset, value, context):
            return queryset if value == "yes" else queryset.none()

        get_forum_registry().register_search_filter(SearchFilterDefinition(
            code="token",
            label="Token",
            module_id="tags",
            target="discussion",
            parser=parse_filter,
            applier=apply_filter,
        ))

        discussions, total = list_runtime_discussions(q="token:yes", user=self.author)

        self.assertEqual(total, 1)
        self.assertEqual([item.id for item in discussions], [discussion.id])

    def test_discussion_detail_field_selection_keeps_tags_relationship(self):
        tag = Tag.objects.create(name="字段裁剪标签", slug="field-selection-tag", color="#4d698e")
        discussion = create_runtime_discussion(
            title="字段裁剪讨论",
            content="用于验证 fields",
            user=self.author,
            extension_payload=discussion_tags_payload([tag.id]),
        )

        response = self.client.get(
            f"/api/discussions/{discussion.id}",
            {"fields[discussion]": "can_reply"},
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertIn("tags", payload)
        self.assertEqual(payload["tags"][0]["slug"], tag.slug)

    def test_restricted_tag_discussion_rename_requires_tag_specific_permission_for_non_author(self):
        restricted_tag = Tag.objects.create(
            name="受限编辑标签",
            slug="restricted-edit-tag",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        admin = User.objects.create_superuser(
            username="restricted-edit-admin",
            email="restricted-edit-admin@example.com",
            password="password123",
        )
        editor = User.objects.create_user(
            username="restricted-edit-user",
            email="restricted-edit-user@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        editor_group = Group.objects.create(name="RestrictedDiscussionEditor", color="#4d698e")
        Permission.objects.create(group=editor_group, permission="discussion.rename")
        Permission.objects.create(group=editor_group, permission=f"tag{restricted_tag.id}.viewForum")
        editor.user_groups.add(editor_group)
        discussion = create_runtime_discussion(
            title="Restricted edit original",
            content="Original content",
            user=admin,
            extension_payload=discussion_tags_payload([restricted_tag.id]),
        )

        denied_response = self.client.patch(
            f"/api/discussions/{discussion.id}",
            data=json.dumps({"title": "Denied edit"}),
            content_type="application/json",
            **self.auth_header(editor),
        )

        self.assertEqual(denied_response.status_code, 403, denied_response.content)
        self.assertIn("没有权限修改讨论标题", denied_response.json()["error"])

        Permission.objects.create(group=editor_group, permission=f"tag{restricted_tag.id}.discussion.rename")
        if hasattr(editor, "_forum_permission_cache"):
            delattr(editor, "_forum_permission_cache")

        allowed_response = self.client.patch(
            f"/api/discussions/{discussion.id}",
            data=json.dumps({"title": "Allowed edit"}),
            content_type="application/json",
            **self.auth_header(editor),
        )

        self.assertEqual(allowed_response.status_code, 200, allowed_response.content)
        self.assertEqual(allowed_response.json()["title"], "Allowed edit")

    def test_author_can_rename_own_discussion_in_restricted_tag_without_tag_specific_permission(self):
        restricted_tag = Tag.objects.create(
            name="作者受限重命名标签",
            slug="restricted-author-rename-tag",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        author_group = Group.objects.create(name="RestrictedAuthorRenamer", color="#4d698e")
        Permission.objects.create(group=author_group, permission="startDiscussion")
        Permission.objects.create(group=author_group, permission="discussion.reply")
        Permission.objects.create(group=author_group, permission=f"tag{restricted_tag.id}.viewForum")
        Permission.objects.create(group=author_group, permission=f"tag{restricted_tag.id}.startDiscussion")
        self.author.user_groups.add(author_group)
        discussion = create_runtime_discussion(
            title="Author restricted rename original",
            content="Original content",
            user=self.author,
            extension_payload=discussion_tags_payload([restricted_tag.id]),
        )

        response = self.client.patch(
            f"/api/discussions/{discussion.id}",
            data=json.dumps({"title": "Author restricted rename allowed"}),
            content_type="application/json",
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["title"], "Author restricted rename allowed")

    def test_restricted_tag_discussion_delete_requires_tag_specific_permission(self):
        restricted_tag = Tag.objects.create(
            name="受限删除标签",
            slug="restricted-delete-tag",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        admin = User.objects.create_superuser(
            username="restricted-delete-admin",
            email="restricted-delete-admin@example.com",
            password="password123",
        )
        moderator = User.objects.create_user(
            username="restricted-delete-user",
            email="restricted-delete-user@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        moderator_group = Group.objects.create(name="RestrictedDiscussionDeleter", color="#4d698e")
        Permission.objects.create(group=moderator_group, permission="discussion.delete")
        Permission.objects.create(group=moderator_group, permission=f"tag{restricted_tag.id}.viewForum")
        moderator.user_groups.add(moderator_group)
        denied_discussion = create_runtime_discussion(
            title="Restricted delete denied",
            content="Original content",
            user=admin,
            extension_payload=discussion_tags_payload([restricted_tag.id]),
        )
        Discussion = type(denied_discussion)

        denied_response = self.client.delete(
            f"/api/discussions/{denied_discussion.id}",
            **self.auth_header(moderator),
        )

        self.assertEqual(denied_response.status_code, 403, denied_response.content)
        self.assertTrue(Discussion.objects.filter(id=denied_discussion.id).exists())

        Permission.objects.create(group=moderator_group, permission=f"tag{restricted_tag.id}.discussion.delete")
        if hasattr(moderator, "_forum_permission_cache"):
            delattr(moderator, "_forum_permission_cache")
        allowed_discussion = create_runtime_discussion(
            title="Restricted delete allowed",
            content="Original content",
            user=admin,
            extension_payload=discussion_tags_payload([restricted_tag.id]),
        )

        allowed_response = self.client.delete(
            f"/api/discussions/{allowed_discussion.id}",
            **self.auth_header(moderator),
        )

        self.assertEqual(allowed_response.status_code, 200, allowed_response.content)
        self.assertFalse(Discussion.objects.filter(id=allowed_discussion.id).exists())

    def test_discussion_list_hides_staff_only_tag_for_non_staff(self):
        staff_tag = Tag.objects.create(
            name="Staff",
            slug="staff-zone",
            view_scope=Tag.ACCESS_STAFF,
            start_discussion_scope=Tag.ACCESS_STAFF,
            reply_scope=Tag.ACCESS_STAFF,
        )
        admin = User.objects.create_superuser(
            username="discussion-admin",
            email="discussion-admin@example.com",
            password="password123",
        )
        discussion = create_runtime_discussion(
            title="仅管理员可见",
            content="内部讨论",
            user=admin,
            extension_payload=discussion_tags_payload([staff_tag.id]),
        )

        guest_response = self.client.get("/api/discussions/")
        self.assertEqual(guest_response.status_code, 200, guest_response.content)
        self.assertEqual(guest_response.json()["total"], 0)

        member_response = self.client.get("/api/discussions/", **self.auth_header(self.reader))
        self.assertEqual(member_response.status_code, 200, member_response.content)
        self.assertEqual(member_response.json()["total"], 0)

        admin_response = self.client.get("/api/discussions/", **self.auth_header(admin))
        self.assertEqual(admin_response.status_code, 200, admin_response.content)
        self.assertEqual(admin_response.json()["total"], 1)
        self.assertEqual(admin_response.json()["data"][0]["id"], discussion.id)

    def test_discussion_list_hides_untagged_discussions_without_global_view_permission(self):
        public_tag = Tag.objects.create(
            name="Public Permission Scope",
            slug="public-permission-scope",
        )
        untagged_discussion = create_runtime_discussion(
            title="Untagged without global view",
            content="Should be hidden without viewForum",
            user=self.author,
        )
        tagged_discussion = create_runtime_discussion(
            title="Tagged without global view",
            content="Can be visible through allowed tag",
            user=self.author,
            extension_payload=discussion_tags_payload([public_tag.id]),
        )
        untagged_post = create_runtime_post(
            discussion_id=untagged_discussion.id,
            content="Untagged reply",
            user=self.author,
        )
        tagged_post = create_runtime_post(
            discussion_id=tagged_discussion.id,
            content="Tagged reply",
            user=self.author,
        )

        limited_reader = User.objects.create_user(
            username="tag-scope-limited-reader",
            email="tag-scope-limited-reader@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        empty_group = Group.objects.create(name="TagScopeNoForumView", color="#4d698e")
        limited_reader.user_groups.add(empty_group)
        Discussion = type(untagged_discussion)

        visible_discussion_ids = set(
            TagService.filter_discussions_for_user(
                Discussion.objects.filter(id__in=[untagged_discussion.id, tagged_discussion.id]),
                limited_reader,
            ).values_list("id", flat=True)
        )
        visible_post_ids = set(
            TagService.filter_posts_for_user(
                Post.objects.filter(id__in=[untagged_post.id, tagged_post.id]),
                limited_reader,
            ).values_list("id", flat=True)
        )

        self.assertNotIn(untagged_discussion.id, visible_discussion_ids)
        self.assertIn(tagged_discussion.id, visible_discussion_ids)
        self.assertNotIn(untagged_post.id, visible_post_ids)
        self.assertIn(tagged_post.id, visible_post_ids)

    def test_tag_visibility_scopes_use_database_subqueries_for_forbidden_tags(self):
        staff_tag = Tag.objects.create(
            name="Subquery Staff",
            slug="subquery-staff-zone",
            view_scope=Tag.ACCESS_STAFF,
            start_discussion_scope=Tag.ACCESS_STAFF,
            reply_scope=Tag.ACCESS_STAFF,
        )
        admin = User.objects.create_superuser(
            username="subquery-tag-admin",
            email="subquery-tag-admin@example.com",
            password="password123",
        )
        discussion = create_runtime_discussion(
            title="Subquery visibility discussion",
            content="Hidden behind staff tag",
            user=admin,
            extension_payload=discussion_tags_payload([staff_tag.id]),
        )
        post = create_runtime_post(
            discussion_id=discussion.id,
            content="Hidden post behind staff tag",
            user=admin,
        )
        Discussion = type(discussion)

        with CaptureQueriesContext(connection) as queries:
            visible_discussions = TagService.filter_discussions_for_user(
                Discussion.objects.all(),
                self.reader,
            )
            self.assertNotIn(discussion.id, set(visible_discussions.values_list("id", flat=True)))
            visible_posts = TagService.filter_posts_for_user(
                Post.objects.all(),
                self.reader,
            )
            self.assertNotIn(post.id, set(visible_posts.values_list("id", flat=True)))

        sql = " ".join(query["sql"].lower() for query in queries)
        self.assertIn(" in (select", sql)
        self.assertNotIn('from "tags" where "tags"."is_restricted"', sql)

    def test_discussion_list_child_permission_does_not_bypass_restricted_parent(self):
        parent = Tag.objects.create(
            name="讨论受限父标签",
            slug="discussion-restricted-parent",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        child = Tag.objects.create(
            name="讨论受限子标签",
            slug="discussion-restricted-child",
            parent=parent,
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        admin = User.objects.create_superuser(
            username="discussion-restricted-parent-admin",
            email="discussion-restricted-parent-admin@example.com",
            password="password123",
        )
        discussion = create_runtime_discussion(
            title="Restricted parent child discussion",
            content="Child permission must not bypass parent visibility.",
            user=admin,
            extension_payload=discussion_tags_payload([parent.id, child.id]),
        )
        group = Group.objects.create(name="DiscussionChildOnlyVisible", color="#4d698e")
        Permission.objects.create(group=group, permission="viewForum")
        Permission.objects.create(group=group, permission=f"tag{child.id}.viewForum")
        self.reader.user_groups.add(group)

        denied_response = self.client.get("/api/discussions/", **self.auth_header(self.reader))

        self.assertEqual(denied_response.status_code, 200, denied_response.content)
        self.assertNotIn(discussion.id, {item["id"] for item in denied_response.json()["data"]})

        Permission.objects.create(group=group, permission=f"tag{parent.id}.viewForum")
        if hasattr(self.reader, "_forum_permission_cache"):
            delattr(self.reader, "_forum_permission_cache")
        allowed_response = self.client.get("/api/discussions/", **self.auth_header(self.reader))

        self.assertEqual(allowed_response.status_code, 200, allowed_response.content)
        self.assertIn(discussion.id, {item["id"] for item in allowed_response.json()["data"]})

    def test_discussion_list_does_not_re_enumerate_tags_for_permission_scopes(self):
        tags = [
            Tag.objects.create(
                name=f"Query Scope Tag {index}",
                slug=f"query-scope-tag-{index}",
                is_restricted=True,
            )
            for index in range(1, 13)
        ]
        groups = [
            Group.objects.create(name=f"QueryScopeGroup{index}", color="#4d698e")
            for index in range(1, 5)
        ]
        for index, group in enumerate(groups):
            Permission.objects.create(group=group, permission=f"tag{tags[index].id}.viewForum")
            Permission.objects.create(group=group, permission="startDiscussion")
            Permission.objects.create(group=group, permission="startDiscussionWithoutApproval")
            for tag in tags:
                Permission.objects.get_or_create(group=group, permission=f"tag{tag.id}.viewForum")
                Permission.objects.create(group=group, permission=f"tag{tag.id}.startDiscussion")

        reader_group = groups[0]
        Permission.objects.create(group=reader_group, permission="viewForum")
        self.reader.user_groups.add(reader_group)

        for index in range(10):
            author = User.objects.create_user(
                username=f"query-scope-author-{index}",
                email=f"query-scope-author-{index}@example.com",
                password="password123",
                is_email_confirmed=True,
            )
            for group in (groups[index % len(groups)], groups[(index + 1) % len(groups)]):
                author.user_groups.add(group)
            create_runtime_discussion(
                title=f"Permission scope discussion {index}",
                content="Exercise discussion list permission scopes.",
                user=author,
                extension_payload=discussion_tags_payload([tags[index % len(tags)].id]),
            )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/discussions/", **self.auth_header(self.reader))

        self.assertEqual(response.status_code, 200, response.content)

        tag_enumerations = [
            query["sql"]
            for query in queries
            if re.match(r'^select\s+.*\s+from\s+["`]?tags["`]?(?:\s|$)', query["sql"].lower())
            and not re.search(r'where\s+.*["`]?id["`]?\s*(=|in\b)', query["sql"].lower())
        ]
        regression_shape_queries = [
            sql
            for sql in tag_enumerations
            if re.match(
                r'^select\s+(?:(?:"id"|"tags"\."id"|`id`|`tags`\.`id`)\s*,\s*'
                r'(?:"is_restricted"|"tags"\."is_restricted"|`is_restricted`|`tags`\.`is_restricted`)|\*)'
                r'\s+from\s+["`]?tags["`]?\s*$',
                sql.lower(),
            )
        ]

        self.assertLessEqual(
            len(regression_shape_queries),
            1,
            "Discussion list should not repeatedly enumerate the tags table while resolving permission scopes.",
        )

    def test_cannot_create_discussion_in_staff_only_tag(self):
        restricted_tag = Tag.objects.create(
            name="管理员专用",
            slug="staff-only-start",
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_STAFF,
            reply_scope=Tag.ACCESS_PUBLIC,
        )

        response = self.client.post(
            "/api/discussions/",
            data=json.dumps(discussion_resource_payload(
                title="Should fail",
                content="Blocked by tag scope",
                tag_ids=[restricted_tag.id],
            )),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("没有权限", response.json()["error"])

    def test_cannot_reply_in_tag_without_reply_permission(self):
        member_group = Group.objects.create(name="TagReplyMember", color="#4d698e")
        Permission.objects.create(group=member_group, permission="discussion.reply")
        self.reader.user_groups.add(member_group)
        admin = User.objects.create_superuser(
            username="reply-admin",
            email="reply-admin@example.com",
            password="password123",
        )
        restricted_tag = Tag.objects.create(
            name="管理回复区",
            slug="staff-reply-only",
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_PUBLIC,
            reply_scope=Tag.ACCESS_STAFF,
        )
        restricted_discussion = create_runtime_discussion(
            title="限制回复讨论",
            content="只有管理员能回复",
            user=admin,
            extension_payload=discussion_tags_payload([restricted_tag.id]),
        )

        response = self.client.post(
            f"/api/discussions/{restricted_discussion.id}/posts",
            data='{"content":"尝试回复"}',
            content_type="application/json",
            **self.auth_header(self.reader),
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("没有权限", response.json()["error"])

    def test_update_discussion_tags_adjusts_tag_stats_without_refresh_event(self):
        tag_a = Tag.objects.create(name="标签A", slug="tag-a", color="#3498db")
        tag_b = Tag.objects.create(name="标签B", slug="tag-b", color="#2ecc71")
        discussion = create_runtime_discussion(
            title="Tag stats event discussion",
            content="Initial post",
            user=self.author,
            extension_payload=discussion_tags_payload([tag_a.id]),
        )
        newer_discussion = create_runtime_discussion(
            title="Newer tag A discussion",
            content="Keeps tag A latest after retag",
            user=self.author,
            extension_payload=discussion_tags_payload([tag_a.id]),
        )
        tag_a.refresh_from_db()
        tag_b.refresh_from_db()
        self.assertEqual(tag_a.discussion_count, 2)
        self.assertEqual(tag_a.last_posted_discussion_id, newer_discussion.id)
        self.assertEqual(tag_b.discussion_count, 0)

        events, dispatch_patch = capture_runtime_events()
        with patch("bias_ext_tags.backend.services.TagService.refresh_tag_stats") as refresh_tag_stats:
            with patch("bias_ext_tags.backend.listeners.refresh_runtime_tag_stats") as refresh_runtime_tag_stats:
                with patch("bias_ext_tags.backend.listeners.refresh_runtime_discussion_tag_stats") as refresh_discussion_tag_stats:
                    with dispatch_patch:
                        with self.captureOnCommitCallbacks(execute=True):
                            update_runtime_discussion(
                                discussion_id=discussion.id,
                                user=self.author,
                                extension_payload=discussion_tags_payload([tag_b.id]),
                            )

        tag_a.refresh_from_db()
        tag_b.refresh_from_db()
        self.assertEqual(tag_a.discussion_count, 1)
        self.assertEqual(tag_a.last_posted_discussion_id, newer_discussion.id)
        self.assertEqual(tag_b.discussion_count, 1)
        self.assertEqual(tag_b.last_posted_discussion_id, discussion.id)
        refresh_tag_stats.assert_not_called()
        refresh_runtime_tag_stats.assert_not_called()
        refresh_discussion_tag_stats.assert_not_called()
        self.assertFalse(any(isinstance(event, TagStatsRefreshRequestedEvent) for event in events))

    def test_update_discussion_tags_refreshes_removed_latest_tag(self):
        tag_a = Tag.objects.create(name="Latest标签A", slug="latest-tag-a", color="#3498db")
        tag_b = Tag.objects.create(name="Latest标签B", slug="latest-tag-b", color="#2ecc71")
        older_discussion = create_runtime_discussion(
            title="Older latest tag discussion",
            content="Older content",
            user=self.author,
            extension_payload=discussion_tags_payload([tag_a.id]),
        )
        latest_discussion = create_runtime_discussion(
            title="Latest tag discussion",
            content="Latest content",
            user=self.author,
            extension_payload=discussion_tags_payload([tag_a.id]),
        )

        with self.captureOnCommitCallbacks(execute=True):
            update_runtime_discussion(
                discussion_id=latest_discussion.id,
                user=self.author,
                extension_payload=discussion_tags_payload([tag_b.id]),
            )

        tag_a.refresh_from_db()
        tag_b.refresh_from_db()
        self.assertEqual(tag_a.discussion_count, 1)
        self.assertEqual(tag_a.last_posted_discussion_id, older_discussion.id)
        self.assertEqual(tag_b.discussion_count, 1)
        self.assertEqual(tag_b.last_posted_discussion_id, latest_discussion.id)

    def test_update_pending_discussion_tags_does_not_refresh_or_adjust_tag_stats(self):
        trusted_group = Group.objects.create(name="PendingRetagTrusted", color="#4d698e")
        Permission.objects.create(group=trusted_group, permission="startDiscussion")
        Permission.objects.create(group=trusted_group, permission="discussion.edit")
        self.author.user_groups.add(trusted_group)

        tag_a = Tag.objects.create(name="待审核旧标签", slug="pending-old-tag", color="#3498db")
        tag_b = Tag.objects.create(name="待审核新标签", slug="pending-new-tag", color="#2ecc71")
        discussion = create_runtime_discussion(
            title="Pending retag",
            content="Pending content",
            user=self.author,
            extension_payload=discussion_tags_payload([tag_a.id]),
        )
        discussion.approval_status = discussion.APPROVAL_PENDING
        discussion.approved_at = None
        discussion.approved_by = None
        discussion.save(update_fields=["approval_status", "approved_at", "approved_by"])
        Post.objects.filter(id=discussion.first_post_id).update(
            approval_status=Post.APPROVAL_PENDING,
            approved_at=None,
            approved_by=None,
        )
        TagService.refresh_tag_stats([tag_a.id, tag_b.id])
        tag_a.refresh_from_db()
        tag_b.refresh_from_db()
        self.assertEqual(tag_a.discussion_count, 0)
        self.assertEqual(tag_b.discussion_count, 0)

        events, dispatch_patch = capture_runtime_events()
        with patch("bias_ext_tags.backend.services.TagService.refresh_tag_stats") as refresh_tag_stats:
            with dispatch_patch:
                with self.captureOnCommitCallbacks(execute=True):
                    update_runtime_discussion(
                        discussion_id=discussion.id,
                        user=self.author,
                        extension_payload=discussion_tags_payload([tag_b.id]),
                    )

        tag_a.refresh_from_db()
        tag_b.refresh_from_db()
        self.assertEqual(tag_a.discussion_count, 0)
        self.assertEqual(tag_b.discussion_count, 0)
        refresh_tag_stats.assert_not_called()
        self.assertFalse(any(isinstance(event, TagStatsRefreshRequestedEvent) for event in events))

    def test_update_discussion_dispatches_discussion_tagged_event_with_all_affected_tag_ids(self):
        parent_tag = Tag.objects.create(name="父标签", slug="parent-tag", color="#3498db")
        old_child_tag = Tag.objects.create(
            name="旧子标签",
            slug="old-child-tag",
            color="#2ecc71",
            parent=parent_tag,
        )
        new_child_tag = Tag.objects.create(
            name="新子标签",
            slug="new-child-tag",
            color="#e67e22",
            parent=parent_tag,
        )
        discussion = create_runtime_discussion(
            title="Discussion tagged event",
            content="Initial post",
            user=self.author,
            extension_payload=discussion_tags_payload([parent_tag.id, old_child_tag.id]),
        )

        events, dispatch_patch = capture_runtime_events()
        with dispatch_patch:
            with self.captureOnCommitCallbacks(execute=True):
                update_runtime_discussion(
                    discussion_id=discussion.id,
                    user=self.author,
                    extension_payload=discussion_tags_payload([parent_tag.id, new_child_tag.id]),
                )

        tagged_event = next(
            event for event in events if isinstance(event, DiscussionTaggedEvent)
        )
        self.assertEqual(
            tagged_event.tag_ids,
            tuple(sorted((parent_tag.id, old_child_tag.id, new_child_tag.id))),
        )

    def test_replacing_discussion_tags_clears_prefetched_relationship_cache(self):
        from bias_ext_tags.backend.discussion_relationships import set_discussion_tags_relationship
        from bias_ext_tags.backend.tag_relationships import serialize_discussion_tag_summaries

        first_tag = Tag.objects.create(name="预取旧标签", slug="prefetched-old-tag", color="#2980b9")
        second_tag = Tag.objects.create(name="预取新标签", slug="prefetched-new-tag", color="#e67e22")
        discussion = create_runtime_discussion(
            title="Prefetched retag",
            content="Initial content",
            user=self.author,
            extension_payload=discussion_tags_payload([first_tag.id]),
        )
        Discussion = type(discussion)
        prefetched_discussion = Discussion.objects.prefetch_related("discussion_tags__tag").get(id=discussion.id)

        self.assertEqual(
            [item["slug"] for item in serialize_discussion_tag_summaries(prefetched_discussion)],
            [first_tag.slug],
        )

        set_discussion_tags_relationship(
            prefetched_discussion,
            {"data": [{"type": "tag", "id": str(second_tag.id)}]},
            {"user": self.author},
        )

        self.assertNotIn(
            "discussion_tags",
            getattr(prefetched_discussion, "_prefetched_objects_cache", {}),
        )
        self.assertEqual(
            [item["slug"] for item in serialize_discussion_tag_summaries(prefetched_discussion)],
            [second_tag.slug],
        )

    def test_replacing_discussion_tags_loads_previous_tags_once(self):
        from bias_ext_tags.backend.tag_relationships import replace_discussion_tags

        first_tag = Tag.objects.create(name="一次读取旧标签", slug="single-load-old-tag", color="#2980b9")
        second_tag = Tag.objects.create(name="一次读取新标签", slug="single-load-new-tag", color="#e67e22")
        discussion = create_runtime_discussion(
            title="Single load retag",
            content="Initial content",
            user=self.author,
            extension_payload=discussion_tags_payload([first_tag.id]),
        )

        with CaptureQueriesContext(connection) as queries:
            result = replace_discussion_tags(discussion, (second_tag,))

        self.assertEqual(result["previous_tag_ids"], (first_tag.id,))
        self.assertEqual(result["previous_tag_names"], (first_tag.name,))
        previous_tag_selects = [
            query["sql"]
            for query in queries
            if re.match(r'^\s*select\b', query["sql"].lower())
            and re.search(r'\bfrom\s+["`]?discussion_tag["`]?', query["sql"].lower())
            and re.search(r'["`]?discussion_id["`]?\s*=\s*\d+\b', query["sql"].lower())
        ]
        self.assertEqual(
            len(previous_tag_selects),
            1,
            f"Replacing discussion tags should load the previous tag relation once: {previous_tag_selects}",
        )

    def test_set_discussion_tags_reuses_previous_tag_snapshot_for_permission_and_replace(self):
        from bias_ext_tags.backend.discussion_relationships import set_discussion_tags_relationship

        first_tag = Tag.objects.create(name="复用旧标签", slug="reuse-old-tag", color="#2980b9")
        second_tag = Tag.objects.create(name="复用新标签", slug="reuse-new-tag", color="#e67e22")
        discussion = create_runtime_discussion(
            title="Reuse previous tags",
            content="Initial content",
            user=self.author,
            extension_payload=discussion_tags_payload([first_tag.id]),
        )

        with CaptureQueriesContext(connection) as queries:
            set_discussion_tags_relationship(
                discussion,
                {"data": [{"type": "tag", "id": str(second_tag.id)}]},
                {"user": self.author},
            )

        previous_tag_selects = [
            query["sql"]
            for query in queries
            if re.match(r'^\s*select\b', query["sql"].lower())
            and re.search(r'\bfrom\s+["`]?discussion_tag["`]?', query["sql"].lower())
            and re.search(r'["`]?discussion_id["`]?\s*=\s*\d+\b', query["sql"].lower())
        ]
        self.assertEqual(
            len(previous_tag_selects),
            1,
            f"Retag permission and replacement should share the previous tag snapshot: {previous_tag_selects}",
        )

    def test_replacing_discussion_tags_skips_write_when_tags_are_unchanged(self):
        from bias_ext_tags.backend.tag_relationships import replace_discussion_tags

        tag = Tag.objects.create(name="不变标签", slug="unchanged-retag", color="#2980b9")
        discussion = create_runtime_discussion(
            title="Unchanged retag",
            content="Initial content",
            user=self.author,
            extension_payload=discussion_tags_payload([tag.id]),
        )
        original_link_id = DiscussionTag.objects.get(discussion=discussion, tag=tag).id

        with patch("bias_ext_tags.backend.tag_relationships.DiscussionTag.objects.bulk_create") as bulk_create:
            result = replace_discussion_tags(discussion, (tag,))

        bulk_create.assert_not_called()
        self.assertTrue(DiscussionTag.objects.filter(id=original_link_id).exists())
        self.assertEqual(result["previous_tag_ids"], (tag.id,))
        self.assertEqual(result["current_tag_ids"], (tag.id,))
        self.assertEqual(result["affected_tag_ids"], ())
        self.assertEqual(result["added_tag_ids"], ())
        self.assertEqual(result["removed_tag_ids"], ())
        self.assertEqual(result["added_tags"], ())
        self.assertEqual(result["removed_tags"], ())

    def test_updating_discussion_tags_creates_discussion_tagged_event_post(self):
        member_group = Group.objects.create(name="DiscussionTagEditor", color="#4d698e")
        Permission.objects.create(group=member_group, permission="startDiscussion")
        Permission.objects.create(group=member_group, permission="discussion.reply")
        self.author.user_groups.add(member_group)

        original_tag = Tag.objects.create(name="后端", slug="backend", color="#2980b9")
        new_tag = Tag.objects.create(name="前端", slug="frontend", color="#e67e22")
        discussion = create_runtime_discussion(
            title="Retag me",
            content="Original content",
            user=self.author,
            extension_payload=discussion_tags_payload([original_tag.id]),
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/discussions/{discussion.id}",
                data=json.dumps(discussion_tags_payload([new_tag.id])),
                content_type="application/json",
                **self.auth_header(),
            )

        self.assertEqual(response.status_code, 200, response.content)
        discussion.refresh_from_db()
        tagged_post = Post.objects.get(discussion=discussion, number=2)
        self.assertEqual(tagged_post.type, "discussionTagged")
        self.assertEqual(discussion.last_post_id, discussion.first_post_id)
        self.assertEqual(discussion.last_post_number, 1)

        posts_response = self.client.get(f"/api/discussions/{discussion.id}/posts")
        payload = posts_response.json()["data"]
        event_post = next(item for item in payload if item["id"] == tagged_post.id)
        self.assertEqual(
            event_post["event_data"],
            {
                "kind": "discussionTagged",
                "added_tags": [new_tag.name],
                "removed_tags": [original_tag.name],
            },
        )

    def test_consecutive_discussion_tagged_event_posts_merge_like_flarum(self):
        member_group = Group.objects.create(name="DiscussionTagMergeEditor", color="#4d698e")
        Permission.objects.create(group=member_group, permission="startDiscussion")
        Permission.objects.create(group=member_group, permission="discussion.reply")
        self.author.user_groups.add(member_group)

        first_tag = Tag.objects.create(name="初始标签", slug="merge-initial", color="#2980b9")
        second_tag = Tag.objects.create(name="中间标签", slug="merge-middle", color="#e67e22")
        third_tag = Tag.objects.create(name="最终标签", slug="merge-final", color="#0f766e")
        discussion = create_runtime_discussion(
            title="Merge retag events",
            content="Original content",
            user=self.author,
            extension_payload=discussion_tags_payload([first_tag.id]),
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/discussions/{discussion.id}",
                data=json.dumps(discussion_tags_payload([second_tag.id])),
                content_type="application/json",
                **self.auth_header(),
            )
        self.assertEqual(response.status_code, 200, response.content)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/discussions/{discussion.id}",
                data=json.dumps(discussion_tags_payload([third_tag.id])),
                content_type="application/json",
                **self.auth_header(),
            )
        self.assertEqual(response.status_code, 200, response.content)

        tagged_posts = list(Post.objects.filter(discussion=discussion, type="discussionTagged").order_by("number"))
        self.assertEqual(len(tagged_posts), 1)
        self.assertEqual(tagged_posts[0].content, f"added:{third_tag.name}\nremoved:{first_tag.name}")

    def test_reverted_discussion_tagged_event_post_is_deleted_like_flarum(self):
        member_group = Group.objects.create(name="DiscussionTagRevertEditor", color="#4d698e")
        Permission.objects.create(group=member_group, permission="startDiscussion")
        Permission.objects.create(group=member_group, permission="discussion.reply")
        self.author.user_groups.add(member_group)

        first_tag = Tag.objects.create(name="原标签", slug="revert-initial", color="#2980b9")
        second_tag = Tag.objects.create(name="临时标签", slug="revert-temporary", color="#e67e22")
        discussion = create_runtime_discussion(
            title="Revert retag events",
            content="Original content",
            user=self.author,
            extension_payload=discussion_tags_payload([first_tag.id]),
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/discussions/{discussion.id}",
                data=json.dumps(discussion_tags_payload([second_tag.id])),
                content_type="application/json",
                **self.auth_header(),
            )
        self.assertEqual(response.status_code, 200, response.content)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/discussions/{discussion.id}",
                data=json.dumps(discussion_tags_payload([first_tag.id])),
                content_type="application/json",
                **self.auth_header(),
            )
        self.assertEqual(response.status_code, 200, response.content)

        self.assertFalse(Post.objects.filter(discussion=discussion, type="discussionTagged").exists())

    def test_author_can_retag_own_discussion_before_replies_by_default(self):
        member_group = Group.objects.create(name="DiscussionTagReplyWindow", color="#4d698e")
        Permission.objects.create(group=member_group, permission="startDiscussion")
        Permission.objects.create(group=member_group, permission="discussion.reply")
        self.author.user_groups.add(member_group)

        original_tag = Tag.objects.create(name="旧标签", slug="old-window-tag", color="#2980b9")
        new_tag = Tag.objects.create(name="新标签", slug="new-window-tag", color="#e67e22")
        discussion = create_runtime_discussion(
            title="Retag before reply",
            content="Original content",
            user=self.author,
            extension_payload=discussion_tags_payload([original_tag.id]),
        )

        response = self.client.patch(
            f"/api/discussions/{discussion.id}",
            data=json.dumps(discussion_tags_payload([new_tag.id])),
            content_type="application/json",
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            list(DiscussionTag.objects.filter(discussion=discussion).values_list("tag_id", flat=True)),
            [new_tag.id],
        )

    def test_author_cannot_retag_own_discussion_after_replies_by_default(self):
        member_group = Group.objects.create(name="DiscussionTagReplyLocked", color="#4d698e")
        Permission.objects.create(group=member_group, permission="startDiscussion")
        Permission.objects.create(group=member_group, permission="discussion.reply")
        self.author.user_groups.add(member_group)
        self.reader.user_groups.add(member_group)

        original_tag = Tag.objects.create(name="锁定旧标签", slug="locked-old-tag", color="#2980b9")
        new_tag = Tag.objects.create(name="锁定新标签", slug="locked-new-tag", color="#e67e22")
        discussion = create_runtime_discussion(
            title="Retag after reply",
            content="Original content",
            user=self.author,
            extension_payload=discussion_tags_payload([original_tag.id]),
        )
        create_runtime_post(
            discussion_id=discussion.id,
            content="Reply locks retagging",
            user=self.reader,
        )
        discussion.refresh_from_db()
        self.assertGreater(discussion.participant_count, 1)

        response = self.client.patch(
            f"/api/discussions/{discussion.id}",
            data=json.dumps(discussion_tags_payload([new_tag.id])),
            content_type="application/json",
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("没有权限修改", response.json()["error"])

    def test_author_can_retag_restricted_tag_only_with_tag_permission(self):
        member_group = Group.objects.create(name="DiscussionRestrictedTagEditor", color="#4d698e")
        Permission.objects.create(group=member_group, permission="startDiscussion")
        Permission.objects.create(group=member_group, permission="discussion.reply")
        self.author.user_groups.add(member_group)

        original_tag = Tag.objects.create(name="开放标签", slug="open-retag-tag", color="#2980b9")
        restricted_tag = Tag.objects.create(
            name="受限新标签",
            slug="restricted-retag-tag",
            color="#e67e22",
            is_restricted=True,
            view_scope=Tag.ACCESS_PUBLIC,
            start_discussion_scope=Tag.ACCESS_MEMBERS,
            reply_scope=Tag.ACCESS_MEMBERS,
        )
        discussion = create_runtime_discussion(
            title="Retag restricted",
            content="Original content",
            user=self.author,
            extension_payload=discussion_tags_payload([original_tag.id]),
        )

        denied_response = self.client.patch(
            f"/api/discussions/{discussion.id}",
            data=json.dumps(discussion_tags_payload([restricted_tag.id])),
            content_type="application/json",
            **self.auth_header(self.author),
        )

        self.assertEqual(denied_response.status_code, 403, denied_response.content)
        self.assertIn("没有权限将标签", denied_response.json()["error"])

        Permission.objects.create(group=member_group, permission=f"tag{restricted_tag.id}.viewForum")
        Permission.objects.create(group=member_group, permission=f"tag{restricted_tag.id}.startDiscussion")
        if hasattr(self.author, "_forum_permission_cache"):
            delattr(self.author, "_forum_permission_cache")

        allowed_response = self.client.patch(
            f"/api/discussions/{discussion.id}",
            data=json.dumps(discussion_tags_payload([restricted_tag.id])),
            content_type="application/json",
            **self.auth_header(self.author),
        )

        self.assertEqual(allowed_response.status_code, 200, allowed_response.content)

    def test_delete_non_latest_discussion_adjusts_tag_count_without_refresh(self):
        admin = User.objects.create_superuser(
            username="discussion-delete-tag-admin",
            email="discussion-delete-tag-admin@example.com",
            password="password123",
        )
        tag = Tag.objects.create(name="删除刷新标签", slug="delete-refresh-tag", color="#4d698e")
        discussion = create_runtime_discussion(
            title="Delete tagged discussion",
            content="Refresh tag stats after delete",
            user=admin,
            extension_payload=discussion_tags_payload([tag.id]),
        )
        newer_discussion = create_runtime_discussion(
            title="Newer delete tagged discussion",
            content="Keeps latest after deleting older discussion",
            user=admin,
            extension_payload=discussion_tags_payload([tag.id]),
        )

        events, dispatch_patch = capture_runtime_events()
        with patch("bias_ext_tags.backend.services.TagService.refresh_tag_stats") as refresh_tag_stats:
            with dispatch_patch:
                with self.captureOnCommitCallbacks(execute=True):
                    delete_runtime_discussion(discussion.id, admin)

        tag.refresh_from_db()
        self.assertEqual(tag.discussion_count, 1)
        self.assertEqual(tag.last_posted_discussion_id, newer_discussion.id)
        refresh_tag_stats.assert_not_called()
        self.assertFalse(any(isinstance(event, TagStatsRefreshRequestedEvent) for event in events))

    def test_delete_latest_discussion_refreshes_tag_latest_discussion(self):
        admin = User.objects.create_superuser(
            username="discussion-delete-latest-tag-admin",
            email="discussion-delete-latest-tag-admin@example.com",
            password="password123",
        )
        tag = Tag.objects.create(name="删除 latest 标签", slug="delete-latest-tag", color="#4d698e")
        older_discussion = create_runtime_discussion(
            title="Older delete tagged discussion",
            content="Older content",
            user=admin,
            extension_payload=discussion_tags_payload([tag.id]),
        )
        latest_discussion = create_runtime_discussion(
            title="Latest delete tagged discussion",
            content="Latest content",
            user=admin,
            extension_payload=discussion_tags_payload([tag.id]),
        )

        with self.captureOnCommitCallbacks(execute=True):
            delete_runtime_discussion(latest_discussion.id, admin)

        tag.refresh_from_db()
        self.assertEqual(tag.discussion_count, 1)
        self.assertEqual(tag.last_posted_discussion_id, older_discussion.id)

    def test_create_discussion_requires_tags_relationship_without_bypass_permission(self):
        group = Group.objects.create(name="StartWithoutBypass", color="#4d698e")
        Permission.objects.create(group=group, permission="startDiscussion")
        self.author.user_groups.add(group)
        if hasattr(self.author, "_forum_permission_cache"):
            delattr(self.author, "_forum_permission_cache")

        response = self.client.post(
            "/api/discussions/",
            data=json.dumps(discussion_resource_payload(
                title="Missing tags relationship",
                content="Creating without tags should be rejected by the resource schema.",
            )),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("缺少必填关系: tags", response.json()["error"])

    def test_create_discussion_allows_missing_tags_relationship_with_bypass_permission(self):
        group = Group.objects.create(name="StartWithBypass", color="#4d698e")
        Permission.objects.create(group=group, permission="startDiscussion")
        Permission.objects.create(group=group, permission="bypassTagCounts")
        self.author.user_groups.add(group)
        if hasattr(self.author, "_forum_permission_cache"):
            delattr(self.author, "_forum_permission_cache")

        response = self.client.post(
            "/api/discussions/",
            data=json.dumps(discussion_resource_payload(
                title="Missing tags with bypass",
                content="Bypass permission matches Flarum's optional tags relationship.",
            )),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["tags"], [])

    def test_cannot_create_discussion_with_secondary_tag_only(self):
        parent_tag = Tag.objects.create(name="开发", slug="dev")
        child_tag = Tag.objects.create(name="后端", slug="backend", parent=parent_tag)

        response = self.client.post(
            "/api/discussions/",
            data=json.dumps(discussion_resource_payload(
                title="Invalid child only",
                content="Blocked by tag combination",
                tag_ids=[child_tag.id],
            )),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("次标签", response.json()["error"])

    def test_can_create_discussion_with_root_secondary_tag_only(self):
        secondary_tag = Tag.objects.create(
            name="公告",
            slug="announcement-secondary",
            position=None,
            is_primary=False,
        )

        response = self.client.post(
            "/api/discussions/",
            data=json.dumps(discussion_resource_payload(
                title="Secondary root only",
                content="Allowed by root secondary semantics",
                tag_ids=[secondary_tag.id],
            )),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["tags"][0]["id"], secondary_tag.id)

    def test_cannot_create_discussion_below_minimum_primary_tags(self):
        set_tags_setting("min_primary_tags", 1)
        secondary_tag = Tag.objects.create(
            name="公告最小主标签",
            slug="announcement-min-primary",
            position=None,
            is_primary=False,
        )

        response = self.client.post(
            "/api/discussions/",
            data=json.dumps(discussion_resource_payload(
                title="Missing primary tag",
                content="Blocked by configured minimum primary count",
                tag_ids=[secondary_tag.id],
            )),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("至少需要选择 1 个主标签", response.json()["error"])

    def test_cannot_create_discussion_below_minimum_secondary_tags(self):
        set_tags_setting("min_secondary_tags", 1)
        primary_tag = Tag.objects.create(name="主标签缺次级", slug="primary-missing-secondary")

        response = self.client.post(
            "/api/discussions/",
            data=json.dumps(discussion_resource_payload(
                title="Missing secondary tag",
                content="Blocked by configured minimum secondary count",
                tag_ids=[primary_tag.id],
            )),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("至少需要选择 1 个次标签", response.json()["error"])

    def test_cannot_create_discussion_with_two_primary_tags(self):
        first_tag = Tag.objects.create(name="前端", slug="frontend")
        second_tag = Tag.objects.create(name="后端", slug="backend-main")

        response = self.client.post(
            "/api/discussions/",
            data=json.dumps(discussion_resource_payload(
                title="Too many primary tags",
                content="Blocked by primary count",
                tag_ids=[first_tag.id, second_tag.id],
            )),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("主标签", response.json()["error"])

    def test_bypass_tag_counts_allows_discussion_outside_configured_tag_counts(self):
        set_tags_setting("min_primary_tags", 1)
        set_tags_setting("max_primary_tags", 1)
        group = Group.objects.create(name="TagCountBypassers", color="#4d698e")
        Permission.objects.create(group=group, permission="startDiscussion")
        Permission.objects.create(group=group, permission="bypassTagCounts")
        self.author.user_groups.add(group)
        first_tag = Tag.objects.create(name="绕过一", slug="bypass-one")
        second_tag = Tag.objects.create(name="绕过二", slug="bypass-two")

        response = self.client.post(
            "/api/discussions/",
            data=json.dumps(discussion_resource_payload(
                title="Bypass tag counts",
                content="Allowed by bypassTagCounts",
                tag_ids=[first_tag.id, second_tag.id],
            )),
            content_type="application/json",
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            set(response.json()["tags"][index]["id"] for index in range(2)),
            {first_tag.id, second_tag.id},
        )

    def test_can_create_discussion_with_multiple_primary_tags_when_limit_allows(self):
        set_tags_setting("max_primary_tags", 2)
        first_tag = Tag.objects.create(name="前端多选", slug="frontend-multi")
        second_tag = Tag.objects.create(name="后端多选", slug="backend-multi")

        response = self.client.post(
            "/api/discussions/",
            data=json.dumps(discussion_resource_payload(
                title="Multiple primary tags",
                content="Allowed by configured primary count",
                tag_ids=[first_tag.id, second_tag.id],
            )),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            set(response.json()["tags"][index]["id"] for index in range(2)),
            {first_tag.id, second_tag.id},
        )

    def test_can_create_discussion_with_multiple_secondary_tags_when_limit_allows(self):
        set_tags_setting("max_secondary_tags", 2)
        first_tag = Tag.objects.create(
            name="公告多选",
            slug="announcement-multi",
            position=None,
            is_primary=False,
        )
        second_tag = Tag.objects.create(
            name="活动多选",
            slug="event-multi",
            position=None,
            is_primary=False,
        )

        response = self.client.post(
            "/api/discussions/",
            data=json.dumps(discussion_resource_payload(
                title="Multiple secondary tags",
                content="Allowed by configured secondary count",
                tag_ids=[first_tag.id, second_tag.id],
            )),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            set(response.json()["tags"][index]["id"] for index in range(2)),
            {first_tag.id, second_tag.id},
        )

    def test_multiple_child_tags_require_their_own_primary_parents(self):
        set_tags_setting("max_primary_tags", 2)
        set_tags_setting("max_secondary_tags", 2)
        first_parent = Tag.objects.create(name="父级一", slug="parent-one")
        second_parent = Tag.objects.create(name="父级二", slug="parent-two")
        first_child = Tag.objects.create(name="子级一", slug="child-one", parent=first_parent)
        second_child = Tag.objects.create(name="子级二", slug="child-two", parent=second_parent)

        response = self.client.post(
            "/api/discussions/",
            data=json.dumps(discussion_resource_payload(
                title="Mismatched multiple children",
                content="Each child needs its parent",
                tag_ids=[first_parent.id, first_child.id, second_child.id],
            )),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("对应的主标签", response.json()["error"])

    def test_cannot_retag_discussion_outside_configured_tag_counts(self):
        set_tags_setting("max_primary_tags", 1)
        first_tag = Tag.objects.create(name="编辑原标签", slug="edit-original")
        second_tag = Tag.objects.create(name="编辑额外标签", slug="edit-extra")
        discussion = create_runtime_discussion(
            title="Retag count limits",
            content="Original content",
            user=self.author,
            extension_payload=discussion_tags_payload([first_tag.id]),
        )

        response = self.client.patch(
            f"/api/discussions/{discussion.id}",
            data=json.dumps(discussion_tags_payload([first_tag.id, second_tag.id])),
            content_type="application/json",
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("最多只能选择 1 个主标签", response.json()["error"])

    def test_cannot_create_discussion_with_mismatched_parent_child_tags(self):
        first_tag = Tag.objects.create(name="前端", slug="frontend-main")
        second_tag = Tag.objects.create(name="后端", slug="backend-main")
        child_tag = Tag.objects.create(name="Vue", slug="vue-child", parent=first_tag)

        response = self.client.post(
            "/api/discussions/",
            data=json.dumps(discussion_resource_payload(
                title="Mismatched tags",
                content="Blocked by parent child mismatch",
                tag_ids=[second_tag.id, child_tag.id],
            )),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("主标签", response.json()["error"])


class AdminTagManagementApiTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        reset_extension_runtime_state()
        rebuild_runtime_urlconf()

    @classmethod
    def tearDownClass(cls):
        reset_extension_runtime_state()
        rebuild_runtime_urlconf()
        super().tearDownClass()

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="admin-tag-mgr",
            email="admin-tag@example.com",
            password="password123",
        )
        self.member = User.objects.create_user(
            username="member-tag-mgr",
            email="member-tag@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        self.other_root_tag = Tag.objects.create(
            name="产品",
            slug="product",
            color="#e67e22",
            position=2,
        )
        self.parent_tag = Tag.objects.create(
            name="开发",
            slug="development",
            color="#4d698e",
            position=0,
        )
        self.child_tag = Tag.objects.create(
            name="后端",
            slug="backend",
            color="#0f766e",
            position=1,
            parent=self.parent_tag,
        )

    def auth_header(self):
        token = RefreshToken.for_user(self.admin).access_token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def auth_header_for(self, user):
        token = RefreshToken.for_user(user).access_token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_admin_can_create_update_and_clear_tag_parent(self):
        response = self.client.post(
            "/api/admin/tags",
            data=json.dumps({
                "name": "接口设计",
                "slug": "api-design",
                "description": "讨论接口约定",
                "color": "#3c78d8",
                "icon": "fas fa-code",
                "parent_id": self.parent_tag.id,
                "position": 3,
                "is_hidden": True,
                "is_restricted": True,
                "view_scope": "members",
                "start_discussion_scope": "staff",
                "reply_scope": "members",
                "default_sort": "latest",
            }),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["slug"], "api-design")
        self.assertEqual(payload["parent_id"], self.parent_tag.id)
        self.assertEqual(payload["parent_name"], self.parent_tag.name)
        self.assertTrue(payload["is_hidden"])
        self.assertTrue(payload["is_restricted"])
        self.assertEqual(payload["view_scope"], "members")
        self.assertEqual(payload["start_discussion_scope"], "staff")
        self.assertEqual(payload["reply_scope"], "members")
        self.assertEqual(payload["default_sort"], "latest")

        created_tag = Tag.objects.get(id=payload["id"])
        self.assertEqual(created_tag.parent_id, self.parent_tag.id)
        self.assertEqual(created_tag.view_scope, "members")
        self.assertEqual(created_tag.default_sort, "latest")

        response = self.client.put(
            f"/api/admin/tags/{created_tag.id}",
            data=json.dumps({
                "name": "接口规范",
                "slug": "api-guidelines",
                "parent_id": None,
                "position": 6,
                "is_hidden": False,
                "is_restricted": False,
                "view_scope": "public",
                "start_discussion_scope": "members",
                "reply_scope": "staff",
                "default_sort": "top",
            }),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["name"], "接口规范")
        self.assertEqual(payload["slug"], "api-guidelines")
        self.assertIsNone(payload["parent_id"])
        self.assertIsNone(payload["parent_name"])
        self.assertFalse(payload["is_hidden"])
        self.assertFalse(payload["is_restricted"])
        self.assertEqual(payload["view_scope"], "public")
        self.assertEqual(payload["start_discussion_scope"], "members")
        self.assertEqual(payload["reply_scope"], "staff")
        self.assertEqual(payload["default_sort"], "top")

        created_tag.refresh_from_db()
        self.assertIsNone(created_tag.parent_id)
        self.assertEqual(created_tag.position, 6)
        self.assertEqual(created_tag.default_sort, "top")
        self.assertEqual(created_tag.reply_scope, "staff")

    def test_admin_tag_api_rejects_slug_with_slash_or_space(self):
        slash_response = self.client.post(
            "/api/admin/tags",
            data=json.dumps({"name": "后台非法 slug", "slug": "admin/invalid"}),
            content_type="application/json",
            **self.auth_header(),
        )
        space_response = self.client.post(
            "/api/admin/tags",
            data=json.dumps({"name": "后台非法空白 slug", "slug": "admin invalid"}),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(slash_response.status_code, 400, slash_response.content)
        self.assertEqual(space_response.status_code, 400, space_response.content)
        self.assertFalse(Tag.objects.filter(name__in=["后台非法 slug", "后台非法空白 slug"]).exists())

    def test_admin_tag_api_rejects_description_longer_than_flarum_limit(self):
        response = self.client.post(
            "/api/admin/tags",
            data=json.dumps({
                "name": "后台描述过长",
                "slug": "admin-long-description",
                "description": "x" * 701,
            }),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Tag.objects.filter(slug="admin-long-description").exists())

    def test_admin_can_create_secondary_root_and_promote_to_primary(self):
        response = self.client.post(
            "/api/admin/tags",
            data=json.dumps({
                "name": "次级入口",
                "slug": "secondary-entry",
                "is_primary": False,
            }),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertFalse(payload["is_primary"])
        self.assertIsNone(payload["position"])

        secondary = Tag.objects.get(id=payload["id"])
        self.assertIsNone(secondary.parent_id)
        self.assertIsNone(secondary.position)
        self.assertFalse(secondary.is_primary)

        response = self.client.put(
            f"/api/admin/tags/{secondary.id}",
            data=json.dumps({"is_primary": True}),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertTrue(payload["is_primary"])
        self.assertIsNotNone(payload["position"])

        secondary.refresh_from_db()
        self.assertIsNone(secondary.parent_id)
        self.assertIsNotNone(secondary.position)
        self.assertTrue(secondary.is_primary)

    def test_admin_unrestricting_tag_deletes_tag_specific_permissions(self):
        restricted_tag = Tag.objects.create(
            name="权限清理",
            slug="permission-cleanup",
            is_restricted=True,
        )
        group = Group.objects.create(name="TagPermissionCleanup", color="#4d698e")
        Permission.objects.create(group=group, permission=f"tag{restricted_tag.id}.startDiscussion")
        Permission.objects.create(group=group, permission=f"tag{restricted_tag.id}.discussion.reply")
        Permission.objects.create(group=group, permission="startDiscussion")

        response = self.client.put(
            f"/api/admin/tags/{restricted_tag.id}",
            data=json.dumps({"is_restricted": False}),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(Permission.objects.filter(permission__startswith=f"tag{restricted_tag.id}.").exists())
        self.assertTrue(Permission.objects.filter(permission="startDiscussion").exists())

    def test_admin_deleting_tag_deletes_tag_specific_permissions(self):
        restricted_tag = Tag.objects.create(
            name="删除权限清理",
            slug="delete-permission-cleanup",
            is_restricted=True,
        )
        group = Group.objects.create(name="TagDeletePermissionCleanup", color="#4d698e")
        Permission.objects.create(group=group, permission=f"tag{restricted_tag.id}.viewForum")
        Permission.objects.create(group=group, permission=f"tag{restricted_tag.id}.startDiscussion")

        response = self.client.delete(
            f"/api/admin/tags/{restricted_tag.id}",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(Permission.objects.filter(permission__startswith=f"tag{restricted_tag.id}.").exists())

    def test_admin_cannot_delete_tag_with_children(self):
        response = self.client.delete(
            f"/api/admin/tags/{self.parent_tag.id}",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("子标签", response.json()["error"])

    def test_admin_cannot_create_grandchild_tag(self):
        response = self.client.post(
            "/api/admin/tags",
            data=json.dumps({
                "name": "Django ORM",
                "slug": "django-orm",
                "parent_id": self.child_tag.id,
            }),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("顶级标签", response.json()["error"])

    def test_admin_cannot_create_child_under_secondary_root(self):
        secondary = Tag.objects.create(
            name="次级父级",
            slug="secondary-parent",
            position=None,
            is_primary=False,
        )

        response = self.client.post(
            "/api/admin/tags",
            data=json.dumps({
                "name": "错误子级",
                "slug": "invalid-admin-secondary-child",
                "parent_id": secondary.id,
            }),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("主标签", response.json()["error"])

    def test_admin_cannot_turn_parent_tag_with_children_into_child(self):
        response = self.client.put(
            f"/api/admin/tags/{self.parent_tag.id}",
            data=json.dumps({
                "parent_id": self.other_root_tag.id,
            }),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("已有子标签", response.json()["error"])

    def test_admin_cannot_set_posting_scopes_wider_than_view_scope(self):
        response = self.client.post(
            "/api/admin/tags",
            data=json.dumps({
                "name": "内部运营",
                "slug": "internal-ops",
                "view_scope": "staff",
                "start_discussion_scope": "members",
                "reply_scope": "staff",
            }),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("发帖权限不能比查看权限更宽松", response.json()["error"])

        response = self.client.put(
            f"/api/admin/tags/{self.parent_tag.id}",
            data=json.dumps({
                "view_scope": "members",
                "reply_scope": "public",
            }),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("回帖权限不能比查看权限更宽松", response.json()["error"])

    def test_admin_can_move_root_tag_up(self):
        response = self.client.post(
            f"/api/admin/tags/{self.other_root_tag.id}/move",
            data=json.dumps({"direction": "up"}),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertTrue(payload["moved"])

        self.other_root_tag.refresh_from_db()
        self.parent_tag.refresh_from_db()
        self.assertEqual(self.other_root_tag.position, 0)
        self.assertEqual(self.parent_tag.position, 1)

    def test_admin_can_move_child_tag_within_same_parent(self):
        sibling_child = Tag.objects.create(
            name="前端",
            slug="frontend",
            color="#3c78d8",
            position=2,
            parent=self.parent_tag,
        )

        response = self.client.post(
            f"/api/admin/tags/{sibling_child.id}/move",
            data=json.dumps({"direction": "up"}),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertTrue(payload["moved"])

        sibling_child.refresh_from_db()
        self.child_tag.refresh_from_db()
        self.parent_tag.refresh_from_db()

        self.assertEqual(sibling_child.position, 0)
        self.assertEqual(self.child_tag.position, 1)
        self.assertEqual(self.parent_tag.position, 0)

    def test_admin_can_order_tags_with_nested_tree_payload(self):
        sibling_child = Tag.objects.create(
            name="前端",
            slug="frontend-order",
            color="#3c78d8",
            position=2,
            parent=self.parent_tag,
        )

        response = self.client.post(
            "/api/admin/tags/order",
            data=json.dumps({
                "order": [
                    {"id": self.other_root_tag.id, "children": []},
                    {"id": self.parent_tag.id, "children": [sibling_child.id, self.child_tag.id]},
                ],
            }),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["data"] if item["parent_id"] is None], [
            self.other_root_tag.id,
            self.parent_tag.id,
        ])

        self.other_root_tag.refresh_from_db()
        self.parent_tag.refresh_from_db()
        self.child_tag.refresh_from_db()
        sibling_child.refresh_from_db()

        self.assertEqual(self.other_root_tag.position, 0)
        self.assertIsNone(self.other_root_tag.parent_id)
        self.assertEqual(self.parent_tag.position, 1)
        self.assertIsNone(self.parent_tag.parent_id)
        self.assertEqual(sibling_child.parent_id, self.parent_tag.id)
        self.assertEqual(sibling_child.position, 0)
        self.assertEqual(self.child_tag.parent_id, self.parent_tag.id)
        self.assertEqual(self.child_tag.position, 1)

    def test_admin_order_tags_turns_omitted_root_tags_into_secondary(self):
        response = self.client.post(
            "/api/admin/tags/order",
            data=json.dumps({
                "order": [
                    {"id": self.parent_tag.id, "children": [self.child_tag.id]},
                ],
            }),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)

        self.parent_tag.refresh_from_db()
        self.child_tag.refresh_from_db()
        self.other_root_tag.refresh_from_db()

        self.assertEqual(self.parent_tag.position, 0)
        self.assertEqual(self.child_tag.parent_id, self.parent_tag.id)
        self.assertEqual(self.child_tag.position, 0)
        self.assertIsNone(self.other_root_tag.parent_id)
        self.assertIsNone(self.other_root_tag.position)
        self.assertFalse(self.other_root_tag.is_primary)
        self.assertFalse(TagService.is_primary_tag(self.other_root_tag))

    def test_admin_order_tags_rejects_duplicate_ids(self):
        response = self.client.post(
            "/api/admin/tags/order",
            data=json.dumps({
                "order": [
                    {"id": self.parent_tag.id, "children": [self.child_tag.id]},
                    {"id": self.child_tag.id, "children": []},
                ],
            }),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("重复标签", response.json()["error"])

    def test_admin_order_tags_rejects_secondary_root_as_parent(self):
        secondary = Tag.objects.create(
            name="次级排序父级",
            slug="secondary-order-parent",
            position=None,
            is_primary=False,
        )

        response = self.client.post(
            "/api/admin/tags/order",
            data=json.dumps({
                "order": [
                    {"id": secondary.id, "children": [self.child_tag.id]},
                ],
            }),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("主标签", response.json()["error"])
        secondary.refresh_from_db()
        self.child_tag.refresh_from_db()
        self.assertFalse(secondary.is_primary)
        self.assertEqual(self.child_tag.parent_id, self.parent_tag.id)

    def test_member_cannot_order_tags(self):
        response = self.client.post(
            "/api/admin/tags/order",
            data=json.dumps({"order": [{"id": self.parent_tag.id, "children": []}]}),
            content_type="application/json",
            **self.auth_header_for(self.member),
        )

        self.assertEqual(response.status_code, 403, response.content)

    def test_flarum_compatible_order_tags_route_returns_no_content(self):
        response = self.client.post(
            "/api/tags/order",
            data=json.dumps({
                "order": [
                    {"id": self.other_root_tag.id, "children": []},
                    {"id": self.parent_tag.id, "children": [self.child_tag.id]},
                ],
            }),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(response.content, b"")

        self.other_root_tag.refresh_from_db()
        self.parent_tag.refresh_from_db()
        self.child_tag.refresh_from_db()

        self.assertEqual(self.other_root_tag.position, 0)
        self.assertIsNone(self.other_root_tag.parent_id)
        self.assertEqual(self.parent_tag.position, 1)
        self.assertIsNone(self.parent_tag.parent_id)
        self.assertEqual(self.child_tag.parent_id, self.parent_tag.id)
        self.assertEqual(self.child_tag.position, 0)

    def test_flarum_compatible_order_tags_route_requires_admin(self):
        response = self.client.post(
            "/api/tags/order",
            data=json.dumps({"order": [{"id": self.parent_tag.id, "children": []}]}),
            content_type="application/json",
            **self.auth_header_for(self.member),
        )

        self.assertEqual(response.status_code, 403, response.content)

    def test_flarum_compatible_order_tags_route_rejects_missing_order(self):
        response = self.client.post(
            "/api/tags/order",
            data=json.dumps({}),
            content_type="application/json",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 422, response.content)

    @patch("bias_ext_tags.backend.admin_api.dispatch_runtime_tag_stats_refresh")
    def test_admin_can_refresh_tag_stats(self, dispatch_runtime_tag_stats_refresh):
        dispatch_runtime_tag_stats_refresh.return_value = {
            "mode": "sync",
            "tag_ids": None,
            "message": "标签统计已同步刷新",
        }

        response = self.client.post(
            "/api/admin/tags/stats/refresh",
            **self.auth_header(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        dispatch_runtime_tag_stats_refresh.assert_called_once_with()
        self.assertEqual(response.json()["message"], "标签统计已同步刷新")
        audit_log = AuditLog.objects.get(action="admin.tag.refresh_stats")
        self.assertEqual(audit_log.target_type, "tag")
        self.assertEqual(audit_log.data["mode"], "sync")


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class TagRealtimeIntegrationTests(TestCase):
    def setUp(self):
        trusted_group = Group.objects.create(
            name="RealtimeTrusted",
            name_singular="RealtimeTrusted",
            name_plural="RealtimeTrusted",
            color="#4d698e",
        )
        Permission.objects.create(group=trusted_group, permission="startDiscussion")
        Permission.objects.create(group=trusted_group, permission="startDiscussionWithoutApproval")
        Permission.objects.create(group=trusted_group, permission="viewForum")
        Permission.objects.create(group=trusted_group, permission="discussion.reply")
        Permission.objects.create(group=trusted_group, permission="replyWithoutApproval")

        self.author = User.objects.create_user(
            username="realtime-author",
            email="realtime-author@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        self.author.user_groups.add(trusted_group)
        self.admin = User.objects.create_superuser(
            username="realtime-admin",
            email="realtime-admin@example.com",
            password="password123",
        )
        self.tag = Tag.objects.create(
            name="实时标签",
            slug="realtime-tag",
            color="#4d698e",
        )
        self.discussion = create_runtime_discussion(
            title="实时讨论",
            content="首帖内容",
            user=self.author,
            extension_payload=discussion_tags_payload([self.tag.id]),
        )
        for extension_id in ("users", "posts", "discussions", "tags"):
            ExtensionInstallation.objects.update_or_create(
                extension_id=extension_id,
                defaults={
                    "version": "0.1.0",
                    "source": "filesystem",
                    "enabled": True,
                    "installed": True,
                    "booted": True,
                },
            )
        reset_extension_runtime_state()
        self.extension_app = bootstrap_extension_application(force=True)

    def tearDown(self):
        get_forum_event_bus().clear()
        reset_extension_application_bootstrap_state()
        super().tearDown()

    def test_hidden_discussion_is_not_visible_to_anonymous_realtime_viewer(self):
        set_runtime_discussion_hidden_state(self.discussion, self.admin, True)

        self.discussion.refresh_from_db()
        self.assertFalse(can_view_model_instance(self.discussion.__class__, self.discussion, user=None, ability="view"))

    def test_visible_discussion_is_accessible_to_authenticated_realtime_viewer(self):
        self.discussion.refresh_from_db()
        self.assertTrue(can_view_model_instance(self.discussion.__class__, self.discussion, user=self.author, ability="view"))

    def test_visible_post_event_broadcasts_discussion_post_and_tag_payload(self):
        post = create_runtime_post(
            discussion_id=self.discussion.id,
            content="新增回复",
            user=self.author,
        )
        broadcasts, broadcast_patch = capture_realtime_discussion_events()
        with broadcast_patch:
            self.extension_app.event_bus.dispatch(
                build_runtime_event(
                    "posts.post.created",
                    post_id=post.id,
                    discussion_id=self.discussion.id,
                    actor_user_id=self.author.id,
                    is_approved=True,
                )
            )

        self.assertTrue(broadcasts)
        discussion_id, event_type, payload = broadcasts[-1]
        self.assertEqual(discussion_id, self.discussion.id)
        self.assertEqual(event_type, "post.created")
        self.assertEqual(payload["discussion"]["id"], self.discussion.id)
        self.assertEqual(payload["discussion"]["last_post_number"], post.number)
        self.assertEqual(payload["post"]["id"], post.id)
        self.assertEqual(payload["post"]["discussion_id"], self.discussion.id)
        self.assertEqual([item["id"] for item in payload["users"]], [self.author.id])
        self.assertEqual([item["id"] for item in payload["tags"]], [self.tag.id])
        self.assertEqual(payload["tags"][0]["last_posted_discussion"]["id"], self.discussion.id)
        self.assertEqual(payload["tags"][0]["last_posted_discussion"]["last_post_number"], post.number)

    def test_discussion_created_event_broadcasts_related_tag_resources(self):
        child_tag = Tag.objects.create(
            name="实时子标签",
            slug="realtime-child-tag",
            color="#e67e22",
            parent=self.tag,
        )

        discussion = create_runtime_discussion(
            title="第二个实时讨论",
            content="讨论内容",
            user=self.author,
            extension_payload=discussion_tags_payload([self.tag.id, child_tag.id]),
        )
        broadcasts, broadcast_patch = capture_realtime_discussion_events()
        with broadcast_patch:
            self.extension_app.event_bus.dispatch(
                build_runtime_event(
                    "discussions.discussion.created",
                    discussion_id=discussion.id,
                    actor_user_id=self.author.id,
                    is_approved=True,
                )
            )

        self.assertTrue(broadcasts)
        discussion_id, event_type, payload = broadcasts[-1]
        self.assertEqual(discussion_id, discussion.id)
        self.assertEqual(event_type, "discussion.created")
        self.assertEqual(payload["discussion"]["id"], discussion.id)
        self.assertEqual(payload["post"]["discussion_id"], discussion.id)
        self.assertEqual([item["id"] for item in payload["users"]], [self.author.id])
        self.assertEqual(
            sorted(item["id"] for item in payload["tags"]),
            sorted([self.tag.id, child_tag.id]),
        )
        self.assertTrue(
            all(item["last_posted_discussion"]["id"] == discussion.id for item in payload["tags"])
        )

    def test_hidden_post_event_broadcasts_minimal_signal_only(self):
        post = create_runtime_post(
            discussion_id=self.discussion.id,
            content="待隐藏回复",
            user=self.author,
        )

        set_runtime_post_hidden_state(post, self.admin, True)
        broadcasts, broadcast_patch = capture_realtime_discussion_events()
        with broadcast_patch:
            self.extension_app.event_bus.dispatch(
                build_runtime_event(
                    "posts.post.hidden",
                    post_id=post.id,
                    discussion_id=self.discussion.id,
                    actor_user_id=self.admin.id,
                    post_number=post.number,
                    is_hidden=True,
                )
            )

        self.assertTrue(broadcasts)
        discussion_id, event_type, payload = broadcasts[-1]
        self.assertEqual(discussion_id, self.discussion.id)
        self.assertEqual(event_type, "post.hidden")
        self.assertEqual(payload, {})
