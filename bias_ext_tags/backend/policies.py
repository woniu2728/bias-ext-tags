from bias_core.extensions import AuthorizationPolicy

from bias_ext_tags.backend.services import TagService


class DiscussionPolicy(AuthorizationPolicy):
    def can(self, user, ability, model, **context):
        if ability in {"view", "reply", "tag"}:
            return None
        return TagService.restricted_discussion_ability_decision(model, user, ability)

    def view(self, user, model, **context):
        return TagService.can_view_discussion_tags(model, user)

    def reply(self, user, model, **context):
        return TagService.can_reply_in_discussion(model, user)


class PostPolicy(AuthorizationPolicy):
    def view(self, user, model, **context):
        discussion = getattr(model, "discussion", None)
        if discussion is None:
            return None
        return TagService.can_view_discussion_tags(discussion, user)


class TagPolicy(AuthorizationPolicy):
    def view(self, user, model, **context):
        return TagService.can_view_tag(model, user)

    def start_discussion(self, user, model, **context):
        return TagService.can_start_discussion_in_tag(model, user)

    def reply(self, user, model, **context):
        return TagService.can_reply_in_tag(model, user)
