"""Social Media Manager agent package."""
from app.agents.base.plain import AgentGuide, Optional_, SettingField
from app.agents.social_media_manager import publishing  # noqa: F401 - registers the publisher and downloads
from app.agents.social_media_manager.agent import AGENT, SLUG, SocialMediaManagerAgent
from app.connectors.base.connector import Capability
from app.services.agent_results import asset_results

GUIDE = AgentGuide(
    summary=(
        "Plans a week of social media posts for Facebook, Instagram, LinkedIn, X and "
        "Pinterest, written from your brand profile, with AI images when an image "
        "integration is connected."
    ),
    effect=(
        "Each run writes posts for the seven days starting tomorrow and gives each one a "
        "posting time, in your workspace's time zone or UTC when none is set. It mixes tips, "
        "product or service highlights, questions and links to your recently published "
        "articles, and adds social proof only when your brand profile states something to "
        "base it on. Once a post is approved (or straight away, if this agent runs without "
        "review) it is scheduled; every 5 minutes, posts whose time has come are published, "
        "and a post approved after its time is published at once. If a network refuses a "
        "post, the reason is shown on it. Facebook, Instagram, LinkedIn, X and Pinterest "
        "publish directly when connected; for a network that is not connected, the post is "
        "ready to download as a spreadsheet or text file instead. Posts to X are published "
        "as text only. Instagram fetches each image from this platform's public web address, "
        "so Instagram posts are made as downloads when the platform is not reachable from "
        "the internet over https. Instagram and Pinterest posts without an image, and Pins "
        "without a link to your website, are always downloads. A second run in the same week "
        "only fills days that do not have a post yet."
    ),
    optional=(
        Optional_(
            Capability.PUBLISH_SOCIAL,
            "Publishes posts directly to the networks you connect. Without one, every post is "
            "ready to download and post yourself.",
        ),
        Optional_(
            Capability.GENERATE_IMAGE,
            "Creates an image for each post with OpenAI or Google Gemini. Without it, posts have "
            "no image, and Instagram and Pinterest posts become downloads.",
        ),
    ),
    uses_scope=False,
    settings=(
        SettingField(
            "posts_per_week",
            "Posts per week, per network",
            "How many posts each network gets in the coming week. Leave blank for 3.",
            kind="integer",
            default="3",
            minimum=1,
            maximum=14,
        ),
        SettingField(
            "hashtags_per_post",
            "Hashtags per post",
            "Leave blank for 3. Fewer are used where a post would otherwise be too long, and "
            "Instagram never gets more than 30.",
            kind="integer",
            default="3",
            minimum=0,
            maximum=30,
        ),
    ),
)


RESULTS = lambda db, tenant_id, cipher: asset_results(  # noqa: E731 - the package contract
    db,
    tenant_id,
    agent_slug=SLUG,
    title="Social posts",
    description="Posts this agent wrote, which network each one is for, and when it goes out.",
    empty="No posts yet. The first run plans the coming week.",
    channel_label="Network",
)


__all__ = ["AGENT", "GUIDE", "RESULTS", "SocialMediaManagerAgent"]
