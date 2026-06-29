from __future__ import annotations

from dataclasses import dataclass

from bias_core.extensions.platform import DomainEvent


@dataclass(frozen=True)
class DiscussionTaggedEvent(DomainEvent):
    discussion_id: int
    actor_user_id: int
    added_tags: tuple[str, ...] = ()
    removed_tags: tuple[str, ...] = ()
    tag_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class TagCreatingEvent(DomainEvent):
    tag: object
    actor: object
    data: dict


@dataclass(frozen=True)
class TagSavingEvent(DomainEvent):
    tag: object
    actor: object
    data: dict


@dataclass(frozen=True)
class TagDeletingEvent(DomainEvent):
    tag: object
    actor: object


@dataclass(frozen=True)
class DiscussionTagStatsRefreshEvent(DomainEvent):
    discussion_id: int


@dataclass(frozen=True)
class TagStatsRefreshRequestedEvent(DomainEvent):
    tag_ids: tuple[int, ...] = ()

