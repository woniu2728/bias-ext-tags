from ninja import Body, Router
from django.db.models import Max
from django.shortcuts import get_object_or_404

from bias_core.extensions.platform import AccessTokenAuth
from bias_core.extensions.platform import api_error
from bias_core.extensions.platform import log_admin_action
from bias_core.extensions.platform import require_staff
from bias_core.extensions.runtime import (
    create_runtime_tag,
    delete_runtime_tag,
    dispatch_runtime_tag_stats_refresh,
    get_runtime_tag_scope_label,
    move_runtime_tag,
    order_runtime_tags,
    update_runtime_tag,
    validate_runtime_tag_parent_assignment,
    validate_runtime_tag_scope_configuration,
)
from bias_ext_tags.backend.models import Tag


router = Router()


def serialize_admin_tag(tag: Tag):
    return {
        "id": tag.id,
        "name": tag.name,
        "slug": tag.slug,
        "description": tag.description,
        "color": tag.color or "#888",
        "icon": tag.icon,
        "position": tag.position,
        "parent_id": tag.parent_id,
        "parent_name": tag.parent.name if tag.parent else None,
        "discussion_count": tag.discussion_count,
        "is_hidden": tag.is_hidden,
        "is_restricted": tag.is_restricted,
        "view_scope": tag.view_scope,
        "start_discussion_scope": tag.start_discussion_scope,
        "reply_scope": tag.reply_scope,
        "view_scope_label": get_runtime_tag_scope_label(tag.view_scope),
        "start_discussion_scope_label": get_runtime_tag_scope_label(tag.start_discussion_scope),
        "reply_scope_label": get_runtime_tag_scope_label(tag.reply_scope),
    }


def normalize_optional_tag_parent(payload):
    normalized = dict(payload)
    if "parent_id" in normalized:
        parent_id = normalized.get("parent_id")
        normalized["parent_id"] = None if parent_id in ("", 0, "0") else parent_id
    return normalized


def normalize_tag_position(payload, parent_id=None, current_tag: Tag = None) -> int:
    if "position" in payload and payload.get("position") is not None:
        return int(payload["position"])

    queryset = Tag.objects.filter(parent_id=parent_id)
    if current_tag is not None:
        queryset = queryset.exclude(id=current_tag.id)
    return (queryset.aggregate(max_position=Max("position")).get("max_position") or 0) + 1


@router.get("/tags", auth=AccessTokenAuth(), tags=["Admin"])
def list_admin_tags(request):
    denied = require_staff(request)
    if denied:
        return denied

    tags = Tag.objects.select_related("parent").all().order_by("position", "name")
    return [serialize_admin_tag(tag) for tag in tags]


@router.post("/tags", auth=AccessTokenAuth(), tags=["Admin"])
def create_admin_tag(request, payload: dict = Body(...)):
    denied = require_staff(request)
    if denied:
        return denied

    try:
        normalized = normalize_optional_tag_parent(payload)
        name = (normalized.get("name") or "").strip()
        if not name:
            raise ValueError("标签名称不能为空")
        parent_id = normalized.get("parent_id")
        tag = create_runtime_tag(
            name=name,
            slug=(normalized.get("slug") or "").strip() or None,
            description=normalized.get("description", ""),
            color=normalized.get("color") or "#888",
            icon=(normalized.get("icon") or "").strip(),
            position=normalize_tag_position(normalized, parent_id=parent_id),
            parent_id=parent_id,
            is_hidden=bool(normalized.get("is_hidden", False)),
            is_restricted=bool(normalized.get("is_restricted", False)),
            view_scope=normalized.get("view_scope") or Tag.ACCESS_PUBLIC,
            start_discussion_scope=normalized.get("start_discussion_scope") or Tag.ACCESS_MEMBERS,
            reply_scope=normalized.get("reply_scope") or Tag.ACCESS_MEMBERS,
            user=request.auth,
        )
        tag = Tag.objects.select_related("parent").get(id=tag.id)
        log_admin_action(
            request,
            "admin.tag.create",
            target_type="tag",
            target_id=tag.id,
            data={"name": tag.name, "slug": tag.slug, "parent_id": tag.parent_id},
        )
        return serialize_admin_tag(tag)
    except ValueError as exc:
        return api_error(str(exc), status=400)
    except Exception as exc:
        return api_error(str(exc), status=400)


@router.post("/tags/order", auth=AccessTokenAuth(), tags=["Admin"])
def order_admin_tags(request, payload: dict = Body(...)):
    denied = require_staff(request)
    if denied:
        return denied

    try:
        if not isinstance(payload, dict) or "order" not in payload:
            raise ValueError("缺少标签排序数据")
        tags = order_runtime_tags(
            order=payload.get("order"),
            user=request.auth,
        )
        log_admin_action(
            request,
            "admin.tag.order",
            target_type="tag",
            data={"ordered_count": len(payload.get("order") or [])},
        )
        return {
            "data": [serialize_admin_tag(item) for item in tags],
        }
    except ValueError as exc:
        return api_error(str(exc), status=400)
    except Exception as exc:
        return api_error(str(exc), status=400)


@router.put("/tags/{tag_id}", auth=AccessTokenAuth(), tags=["Admin"])
def update_admin_tag(request, tag_id: int, payload: dict = Body(...)):
    denied = require_staff(request)
    if denied:
        return denied

    try:
        get_object_or_404(Tag, id=tag_id)
        normalized = normalize_optional_tag_parent(payload)
        update_payload = {}

        if "name" in normalized:
            name = (normalized.get("name") or "").strip()
            if not name:
                raise ValueError("标签名称不能为空")
            update_payload["name"] = name
        if "slug" in normalized:
            update_payload["slug"] = (normalized.get("slug") or "").strip()
        if "description" in normalized:
            update_payload["description"] = normalized.get("description") or ""
        if "color" in normalized:
            update_payload["color"] = normalized.get("color") or "#888"
        if "icon" in normalized:
            update_payload["icon"] = (normalized.get("icon") or "").strip()
        if "position" in normalized and normalized.get("position") is not None:
            update_payload["position"] = int(normalized["position"])
        if "parent_id" in normalized:
            update_payload["parent_id"] = normalized.get("parent_id")
        if "is_hidden" in normalized:
            update_payload["is_hidden"] = bool(normalized["is_hidden"])
        if "is_restricted" in normalized:
            update_payload["is_restricted"] = bool(normalized["is_restricted"])
        if "view_scope" in normalized:
            update_payload["view_scope"] = normalized.get("view_scope")
        if "start_discussion_scope" in normalized:
            update_payload["start_discussion_scope"] = normalized.get("start_discussion_scope")
        if "reply_scope" in normalized:
            update_payload["reply_scope"] = normalized.get("reply_scope")
        tag = update_runtime_tag(tag_id, request.auth, **update_payload)
        tag = Tag.objects.select_related("parent").get(id=tag.id)
        log_admin_action(
            request,
            "admin.tag.update",
            target_type="tag",
            target_id=tag.id,
            data={"name": tag.name, "slug": tag.slug, "changed_fields": sorted(normalized.keys())},
        )
        return serialize_admin_tag(tag)
    except ValueError as exc:
        return api_error(str(exc), status=400)
    except Exception as exc:
        return api_error(str(exc), status=400)


@router.post("/tags/{tag_id}/move", auth=AccessTokenAuth(), tags=["Admin"])
def move_admin_tag(request, tag_id: int, payload: dict = Body(...)):
    denied = require_staff(request)
    if denied:
        return denied

    try:
        tag = get_object_or_404(Tag, id=tag_id)
        moved = move_runtime_tag(
            tag_id=tag_id,
            direction=(payload.get("direction") or "").strip(),
            user=request.auth,
        )
        tags = Tag.objects.select_related("parent").all().order_by("position", "name")
        log_admin_action(
            request,
            "admin.tag.move",
            target_type="tag",
            target_id=tag.id,
            data={"name": tag.name, "direction": (payload.get("direction") or "").strip(), "moved": bool(moved)},
        )
        return {
            "moved": moved,
            "data": [serialize_admin_tag(item) for item in tags],
        }
    except ValueError as exc:
        return api_error(str(exc), status=400)
    except Tag.DoesNotExist:
        return api_error("标签不存在", status=404)


@router.delete("/tags/{tag_id}", auth=AccessTokenAuth(), tags=["Admin"])
def delete_admin_tag(request, tag_id: int):
    denied = require_staff(request)
    if denied:
        return denied

    try:
        tag = get_object_or_404(Tag, id=tag_id)
        tag_snapshot = {"name": tag.name, "slug": tag.slug, "parent_id": tag.parent_id}
        delete_runtime_tag(tag_id, request.auth)
        log_admin_action(
            request,
            "admin.tag.delete",
            target_type="tag",
            target_id=tag_id,
            data=tag_snapshot,
        )
        return {"message": "标签删除成功"}
    except ValueError as exc:
        return api_error(str(exc), status=400)


@router.post("/tags/stats/refresh", auth=AccessTokenAuth(), tags=["Admin"])
def refresh_admin_tag_stats(request):
    denied = require_staff(request)
    if denied:
        return denied

    result = dispatch_runtime_tag_stats_refresh()
    log_admin_action(
        request,
        "admin.tag.refresh_stats",
        target_type="tag",
        data={
            "mode": result.get("mode"),
            "tag_ids": result.get("tag_ids"),
        },
    )
    return result
