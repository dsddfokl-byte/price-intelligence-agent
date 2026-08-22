"""Safe delivery routing for assigned Threads post intents."""

import logging
from datetime import timedelta

from app.config import ConfigurationError, THREADS_PUBLISHING, THREADS_TOPIC_TAGS
from app.config import load_threads_access_token
from app.growth_content import generate_generic_growth_post, generate_growth_post
from app.post_intent import PostIntent
from app.publishers.threads import ThreadsAPIError
from app.topic_discovery import discover_growth_topic


LOGGER = logging.getLogger("affiliate_automation")
AFFILIATE_NO_ELIGIBLE_PRODUCT = "AFFILIATE_NO_ELIGIBLE_PRODUCT"


def resolve_delivered_intent(assigned_intent, affiliate_candidate_count):
    """Preserve ITT assignment while selecting the actual delivery path."""
    if (
        assigned_intent == PostIntent.AFFILIATE
        and affiliate_candidate_count == 0
    ):
        return PostIntent.GROWTH, AFFILIATE_NO_ELIGIBLE_PRODUCT
    return assigned_intent, None


def prepare_growth_candidate(database, now, search_terms, *, dry_run):
    """Use the single Growth generation path for assigned and fallback delivery."""
    try:
        candidate = generate_generic_growth_post(
            now, search_terms, THREADS_TOPIC_TAGS
        )
        growth_topic = candidate.topic_tag
        if not dry_run:
            try:
                growth_topic = discover_growth_topic(
                    database, load_threads_access_token(),
                    candidate.search_keyword, candidate.topic_tag, now,
                )
            except (ConfigurationError, ThreadsAPIError):
                LOGGER.warning("Growth topic fallback source=generic")
        growth_post = generate_growth_post(candidate, now, growth_topic)
        hash_since = (
            now - timedelta(days=THREADS_PUBLISHING.text_cooldown_days)
        ).isoformat()
        if database.has_published_text_hash_since(growth_post.text_hash, hash_since):
            return None, "DUPLICATE"
        return growth_post, None
    except ValueError:
        return None, "GENERATION_FAILED"
