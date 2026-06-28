TAG_MANAGEMENT_PERMISSIONS = {
    "tag.create",
    "tag.edit",
    "tag.delete",
}


def grant_staff_tag_management_permissions(user, permission_names, **kwargs):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
        return False
    return bool(TAG_MANAGEMENT_PERMISSIONS.intersection(set(permission_names or ())))
