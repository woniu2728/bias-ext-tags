from django.conf import settings
from django.db import models


class Tag(models.Model):
    """
    标签模型，由 tags 扩展拥有。
    """

    ACCESS_PUBLIC = "public"
    ACCESS_MEMBERS = "members"
    ACCESS_STAFF = "staff"
    ACCESS_SCOPE_CHOICES = [
        (ACCESS_PUBLIC, "所有人"),
        (ACCESS_MEMBERS, "已登录用户"),
        (ACCESS_STAFF, "仅管理员"),
    ]

    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    color = models.CharField(max_length=20, blank=True)
    icon = models.CharField(max_length=100, blank=True)
    background_url = models.URLField(max_length=500, blank=True)
    position = models.IntegerField(null=True, blank=True, default=0)
    is_primary = models.BooleanField(default=True)
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )
    is_hidden = models.BooleanField(default=False)
    is_restricted = models.BooleanField(default=False)
    view_scope = models.CharField(max_length=20, choices=ACCESS_SCOPE_CHOICES, default=ACCESS_PUBLIC)
    start_discussion_scope = models.CharField(max_length=20, choices=ACCESS_SCOPE_CHOICES, default=ACCESS_MEMBERS)
    reply_scope = models.CharField(max_length=20, choices=ACCESS_SCOPE_CHOICES, default=ACCESS_MEMBERS)
    discussion_count = models.IntegerField(default=0)
    last_posted_at = models.DateTimeField(null=True, blank=True)
    last_posted_discussion = models.ForeignKey(
        "discussions.Discussion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "tags"
        db_table = "tags"
        ordering = ["position", "name"]
        indexes = [
            models.Index(fields=["parent"], name="tags_parent__7fcc39_idx"),
            models.Index(fields=["parent", "position", "name"], name="tags_parent_pos_name_idx"),
            models.Index(fields=["position", "parent"], name="tags_position_parent_idx"),
            models.Index(fields=["is_primary", "parent", "position"], name="tags_primary_parent_pos_idx"),
        ]

    def __str__(self):
        return self.name


class DiscussionTag(models.Model):
    """
    讨论标签关系，由 tags 扩展拥有。
    """

    discussion = models.ForeignKey("discussions.Discussion", on_delete=models.CASCADE, related_name="discussion_tags")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="discussion_tags")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "tags"
        db_table = "discussion_tag"
        unique_together = [["discussion", "tag"]]
        indexes = [
            models.Index(fields=["discussion"], name="discussion__discuss_d30c2c_idx"),
            models.Index(fields=["tag"], name="discussion__tag_id_4f1793_idx"),
        ]

    def __str__(self):
        return f"{self.discussion.title} - {self.tag.name}"


class TagState(models.Model):
    """
    用户-标签状态，由 tags 扩展拥有。

    对齐 tag_user 用户态表：每个用户在每个标签上可以有独立的
    marked_as_read_at 和 is_hidden 状态，不把用户态写入 Tag 本体。
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tag_states")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="user_states")
    marked_as_read_at = models.DateTimeField(null=True, blank=True)
    is_hidden = models.BooleanField(default=False)

    class Meta:
        app_label = "tags"
        db_table = "tag_user"
        unique_together = [["user", "tag"]]
        indexes = [
            models.Index(fields=["user"], name="tag_user_user_id_0f20f5_idx"),
            models.Index(fields=["tag"], name="tag_user_tag_id_8e3d59_idx"),
        ]

    def __str__(self):
        return f"{self.user_id} - {self.tag_id}"

