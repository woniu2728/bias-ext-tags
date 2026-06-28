from bias_core.extensions import setting_field


def setting_field_definitions():
    return (
        setting_field({
            "key": "min_primary_tags",
            "label": "最少主标签数",
            "type": "number",
            "default": 0,
            "help_text": "发起讨论时要求选择的最少主标签数。",
            "order": 10,
        }),
        setting_field({
            "key": "max_primary_tags",
            "label": "最多主标签数",
            "type": "number",
            "default": 1,
            "help_text": "发起讨论时允许选择的最多主标签数。",
            "order": 20,
        }),
        setting_field({
            "key": "min_secondary_tags",
            "label": "最少次标签数",
            "type": "number",
            "default": 0,
            "help_text": "发起讨论时要求选择的最少次标签数。",
            "order": 30,
        }),
        setting_field({
            "key": "max_secondary_tags",
            "label": "最多次标签数",
            "type": "number",
            "default": 1,
            "help_text": "发起讨论时允许选择的最多次标签数。",
            "order": 40,
        }),
        setting_field({
            "key": "allow_tag_change",
            "label": "作者可修改标签",
            "type": "select",
            "default": "reply",
            "help_text": "控制讨论作者在发布后是否仍可修改标签。",
            "order": 50,
            "options": (
                {"value": "reply", "label": "无人回复前"},
                {"value": "-1", "label": "始终允许"},
                {"value": "0", "label": "不允许"},
                {"value": "10", "label": "10 分钟内"},
                {"value": "60", "label": "1 小时内"},
            ),
        }),
    )
