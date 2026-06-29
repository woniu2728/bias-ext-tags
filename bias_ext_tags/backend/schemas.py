from typing import Optional

from pydantic import BaseModel, Field, field_validator


class TagCreateSchema(BaseModel):
    """创建标签"""
    name: str = Field(..., min_length=1, max_length=100, description="标签名称")
    slug: Optional[str] = Field(None, max_length=100, description="标签slug（可选，自动生成）")
    description: Optional[str] = Field("", max_length=700, description="标签描述")
    color: Optional[str] = Field("", max_length=20, description="标签颜色")
    icon: Optional[str] = Field("", max_length=100, description="标签图标")
    background_url: Optional[str] = Field("", description="背景图片URL")
    position: Optional[int] = Field(0, description="排序位置")
    default_sort: Optional[str] = Field(None, max_length=50, description="标签讨论默认排序")
    is_primary: Optional[bool] = Field(True, description="是否主标签")
    parent_id: Optional[int] = Field(None, description="父标签ID")
    is_hidden: Optional[bool] = Field(False, description="是否隐藏")
    is_restricted: Optional[bool] = Field(False, description="是否限制发帖")
    view_scope: Optional[str] = Field("public", description="查看权限级别")
    start_discussion_scope: Optional[str] = Field("members", description="发帖权限级别")
    reply_scope: Optional[str] = Field("members", description="回帖权限级别")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        if not value.strip():
            raise ValueError('标签名称不能为空')
        return value.strip()

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value):
        if value is None:
            return value
        normalized = value.strip()
        if "/" in normalized or any(item.isspace() for item in normalized):
            raise ValueError("标签 slug 不能包含斜杠或空白字符")
        return normalized


class TagUpdateSchema(BaseModel):
    """更新标签"""
    name: Optional[str] = Field(None, min_length=1, max_length=100, description="标签名称")
    slug: Optional[str] = Field(None, max_length=100, description="标签slug")
    description: Optional[str] = Field(None, max_length=700, description="标签描述")
    color: Optional[str] = Field(None, max_length=20, description="标签颜色")
    icon: Optional[str] = Field(None, max_length=100, description="标签图标")
    background_url: Optional[str] = Field(None, description="背景图片URL")
    position: Optional[int] = Field(None, description="排序位置")
    default_sort: Optional[str] = Field(None, max_length=50, description="标签讨论默认排序")
    is_primary: Optional[bool] = Field(None, description="是否主标签")
    parent_id: Optional[int] = Field(None, description="父标签ID")
    is_hidden: Optional[bool] = Field(None, description="是否隐藏")
    is_restricted: Optional[bool] = Field(None, description="是否限制发帖")
    view_scope: Optional[str] = Field(None, description="查看权限级别")
    start_discussion_scope: Optional[str] = Field(None, description="发帖权限级别")
    reply_scope: Optional[str] = Field(None, description="回帖权限级别")

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value):
        if value is None:
            return value
        normalized = value.strip()
        if "/" in normalized or any(item.isspace() for item in normalized):
            raise ValueError("标签 slug 不能包含斜杠或空白字符")
        return normalized

