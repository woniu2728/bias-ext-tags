from bias_core.extensions import AdminPageDefinition, PermissionDefinition

from bias_ext_tags.backend.constants import EXTENSION_ID


def admin_page_definitions():
    return (
        AdminPageDefinition(
            path="/admin/tags",
            label="标签管理",
            icon="fas fa-tags",
            module_id=EXTENSION_ID,
            nav_section="feature",
            description="维护标签结构、排序与访问范围。",
        ),
    )


def permission_definitions():
    return (
        PermissionDefinition(
            code="tag.create",
            label="创建标签",
            section="tags",
            section_label="标签",
            module_id=EXTENSION_ID,
            icon="fas fa-plus",
            description="允许通过标签资源端点创建标签。",
            required_permissions=("viewForum",),
        ),
        PermissionDefinition(
            code="tag.edit",
            label="编辑标签",
            section="tags",
            section_label="标签",
            module_id=EXTENSION_ID,
            icon="fas fa-pencil-alt",
            description="允许通过标签资源端点编辑标签。",
            required_permissions=("viewForum",),
        ),
        PermissionDefinition(
            code="tag.delete",
            label="删除标签",
            section="tags",
            section_label="标签",
            module_id=EXTENSION_ID,
            icon="fas fa-trash",
            description="允许通过标签资源端点删除标签。",
            required_permissions=("tag.edit",),
        ),
        PermissionDefinition(
            code="bypassTagCounts",
            label="绕过标签数量限制",
            section="tags",
            section_label="标签",
            module_id=EXTENSION_ID,
            icon="fas fa-tags",
            description="允许发帖时绕过主标签、次标签数量限制。",
        ),
    )
