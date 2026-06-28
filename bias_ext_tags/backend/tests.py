import json
import re
from pathlib import Path

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from ninja_jwt.tokens import RefreshToken
from io import StringIO
from unittest.mock import Mock, patch

from bias_core.extensions.runtime import (
    approve_runtime_discussion,
    create_runtime_discussion,
    delete_runtime_discussion,
    get_runtime_model_url_service,
    get_runtime_tag_state_model,
    set_runtime_discussion_hidden_state,
    to_runtime_model_slug,
    update_runtime_discussion,
)
from bias_core.extensions import ResourceEndpointDefinition
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
from bias_ext_tags.backend.events import DiscussionTaggedEvent, TagStatsRefreshRequestedEvent
from bias_ext_tags.backend.models import Tag
from bias_core.extensions.runtime import get_runtime_discussion_tag_model
from bias_ext_tags.backend.services import TagService
from bias_ext_tags.backend.resources import tag_resource_endpoints
from bias_core.extensions.runtime import (
    create_runtime_post,
    get_runtime_post_model,
    set_runtime_post_hidden_state,
)
from bias_core.extensions.runtime import (
    get_runtime_group_model,
    get_runtime_permission_model,
    get_runtime_user_model,
)


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

        with self.captureOnCommitCallbacks(execute=True):
            approve_runtime_discussion(discussion, admin)
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.discussion_count, 1)
        self.assertEqual(self.tag.last_posted_discussion_id, discussion.id)

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
        self.assertTrue(payload["can_add_to_discussion"])
        self.assertTrue(payload["can_reply"])
        self.assertEqual(payload["last_posted_discussion"]["id"], discussion.id)

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
        self.assertEqual(denied_response.status_code, 200, denied_response.content)
        self.assertFalse(denied_response.json()["can_start_discussion"])
        self.assertFalse(denied_response.json()["can_add_to_discussion"])

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

    def test_tag_slug_detail_accepts_id_with_slug_url(self):
        response = self.client.get(f"/api/tags/slug/{self.public_tag.id}-renamed")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["slug"], "public-tag")

    def test_tag_detail_static_route_uses_resource_endpoint_mutator(self):
        def mutate_endpoint(endpoint):
            def handler(context):
                payload = endpoint.handler(context)
                payload["mutated_by_resource_endpoint"] = True
                return payload

            return ResourceEndpointDefinition(
                resource=endpoint.resource,
                endpoint=endpoint.endpoint,
                module_id="test",
                handler=handler,
                methods=endpoint.methods,
            )

        registry = ResourceRegistry()
        for endpoint in tag_resource_endpoints():
            registry.register_endpoint(endpoint)
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

    def test_guest_cannot_view_staff_tag_detail(self):
        response = self.client.get(f"/api/tags/{self.staff_tag.id}")

        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("没有权限", response.json()["error"])

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


class TagForumSettingsTests(TestCase):
    def setUp(self):
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
        self.assertTrue(
            any(
                route["path"] == "/tags"
                and route["name"] == "tags"
                and route["component"] == "./TagsView.vue"
                for route in tags_extension["frontend_routes"]
            )
        )
        self.assertFalse(guest_payload["can_bypass_tag_counts"])
        self.assertEqual([item["slug"] for item in guest_payload["tags"]], ["announcements"])
        self.assertEqual(guest_payload["tags"][0]["children"][0]["slug"], "news")

        staff_response = self.client.get("/api/forum", **self.auth_header())
        self.assertEqual(staff_response.status_code, 200, staff_response.content)
        staff_payload = staff_response.json()
        self.assertTrue(staff_payload["can_bypass_tag_counts"])
        self.assertEqual(
            [item["slug"] for item in staff_payload["tags"]],
            ["announcements", "staff"],
        )

    def test_public_forum_settings_use_bypass_tag_counts_permission(self):
        bypass_group = Group.objects.create(name="BypassTagCounts", color="#4d698e")
        Permission.objects.create(group=bypass_group, permission="bypassTagCounts")
        self.member.user_groups.add(bypass_group)

        response = self.client.get("/api/forum", **self.auth_header(self.member))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["can_bypass_tag_counts"])

    def test_forum_tag_tree_prefetches_child_last_posted_discussions(self):
        permission_group = Group.objects.create(name="ForumTagTreePosting", color="#4d698e")
        Permission.objects.create(group=permission_group, permission="startDiscussion")
        Permission.objects.create(group=permission_group, permission="startDiscussionWithoutApproval")
        self.member.user_groups.add(permission_group)
        parent = Tag.objects.create(name="Forum Parent", slug="forum-parent")
        children = [
            Tag.objects.create(
                name=f"Forum Child {index}",
                slug=f"forum-child-{index}",
                parent=parent,
                position=index,
            )
            for index in range(6)
        ]
        for tag in (parent, *children):
            Permission.objects.create(group=permission_group, permission=f"tag{tag.id}.startDiscussion")
        for child in children:
            with self.captureOnCommitCallbacks(execute=True):
                create_runtime_discussion(
                    title=f"Child discussion {child.id}",
                    content="Forum child last posted discussion.",
                    user=self.member,
                    extension_payload=discussion_tags_payload([parent.id, child.id]),
                )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/forum", **self.auth_header(self.member))

        self.assertEqual(response.status_code, 200, response.content)
        payload_parent = next(item for item in response.json()["tags"] if item["slug"] == parent.slug)
        self.assertEqual(len(payload_parent["children"]), len(children))
        self.assertTrue(
            all(child["last_posted_discussion"] for child in payload_parent["children"])
        )
        child_discussion_fetches = [
            query["sql"]
            for query in queries
            if 'from "discussions"' in query["sql"].lower()
            and 'where "discussions"."id" =' in query["sql"].lower()
        ]
        self.assertEqual(
            child_discussion_fetches,
            [],
            "Forum tag tree should not issue per-child last_posted_discussion lookups.",
        )
        child_parent_fetches = [
            query["sql"]
            for query in queries
            if 'from "tags"' in query["sql"].lower()
            and 'where "tags"."id" =' in query["sql"].lower()
        ]
        self.assertEqual(
            child_parent_fetches,
            [],
            "Forum tag tree should not issue per-child parent tag lookups while resolving permissions.",
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

    def test_search_filters_api_exposes_registered_tag_filter_syntax(self):
        response = self.client.get("/api/search/filters", {"target": "discussions"})

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
        tagged_post = Post.objects.get(discussion=discussion, number=2)
        self.assertEqual(tagged_post.type, "discussionTagged")

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

        created_tag = Tag.objects.get(id=payload["id"])
        self.assertEqual(created_tag.parent_id, self.parent_tag.id)
        self.assertEqual(created_tag.view_scope, "members")

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

        created_tag.refresh_from_db()
        self.assertIsNone(created_tag.parent_id)
        self.assertEqual(created_tag.position, 6)
        self.assertEqual(created_tag.reply_scope, "staff")

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

    def test_member_cannot_order_tags(self):
        response = self.client.post(
            "/api/admin/tags/order",
            data=json.dumps({"order": [{"id": self.parent_tag.id, "children": []}]}),
            content_type="application/json",
            **self.auth_header_for(self.member),
        )

        self.assertEqual(response.status_code, 403, response.content)

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
