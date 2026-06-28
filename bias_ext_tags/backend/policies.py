from bias_core.extensions import AuthorizationPolicy

from bias_ext_tags.backend.services import TagService


class DiscussionPolicy(AuthorizationPolicy):
    def can(self, user, ability, model, **context):
        if ability in {"view", "reply", "tag", "rename", "hide"}:
            return None
        return TagService.restricted_discussion_ability_decision(model, user, ability)

    def view(self, user, model, **context):
        return TagService.can_view_discussion_tags(model, user)

    def reply(self, user, model, **context):
        return TagService.can_reply_in_discussion(model, user)

    def rename(self, user, model, **context):
        decision = TagService.restricted_discussion_ability_decision(
            model,
            user,
            "rename",
            deny_without_permission=getattr(model, "user_id", None) != getattr(user, "id", None),
        )
        return decision

    def hide(self, user, model, **context):
        decision = TagService.restricted_discussion_ability_decision(
            model,
            user,
            "hide",
            deny_without_permission=getattr(model, "user_id", None) != getattr(user, "id", None),
        )
        return decision


class PostPolicy(AuthorizationPolicy):
    def view(self, user, model, **context):
        discussion = getattr(model, "discussion", None)
        if discussion is None:
            return None
        return TagService.can_view_discussion_tags(discussion, user)


class TagPolicy(AuthorizationPolicy):
    def can(self, user, ability, model, **context):
        return TagService.can_tag_ability(model, user, ability)

    def create(self, user, model, **context):
        return TagService.can_manage_tags(user, "tag.create")

    def create_tag(self, user, model, **context):
        return self.create(user, model, **context)

    def edit(self, user, model, **context):
        return TagService.can_manage_tags(user, "tag.edit")

    def delete(self, user, model, **context):
        return TagService.can_manage_tags(user, "tag.delete")

    def move(self, user, model, **context):
        return self.edit(user, model, **context)

    def order(self, user, model, **context):
        return self.edit(user, model, **context)

    def view(self, user, model, **context):
        return TagService.can_view_tag(model, user)

    def start_discussion(self, user, model, **context):
        return TagService.can_start_discussion_in_tag(model, user)

    def reply(self, user, model, **context):
        return TagService.can_reply_in_tag(model, user)

    def add_to_discussion(self, user, model, **context):
        return TagService.can_add_to_discussion(model, user)
