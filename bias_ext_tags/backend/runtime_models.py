from bias_core.extensions import RuntimeModel


DISCUSSION_MODEL = RuntimeModel("discussions.service", description="discussions 扩展提供的讨论模型。")
POST_MODEL = RuntimeModel("posts.service", description="posts 扩展提供的帖子模型。")
