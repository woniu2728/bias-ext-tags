from celery import shared_task

from bias_ext_tags.backend.services import TagService


@shared_task(ignore_result=True)
def refresh_tag_stats_task(tag_ids=None):
    TagService.refresh_tag_stats(tag_ids)
