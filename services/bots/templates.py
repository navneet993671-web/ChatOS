"""Built-in bot templates.

A template is just a pre-filled create payload — name, description, suggested
capabilities and a default trigger. There is deliberately **no** template-
specific execution logic anywhere: at run time every bot is identical, and a
template only saves the user configuration typing.

Every template stays inside autonomy level 1 (assist) and refuses the
high-risk capabilities (email.send, shell.execute, files.write, mcp.external),
so accepting a template's defaults can never hand a new bot a dangerous grant.
"""

from typing import List, Dict, Any

TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "researcher",
        "name": "Research Bot",
        "icon": "🔎",
        "description": "Recurring research on a topic, with results you can review.",
        "instructions": (
            "Research the topic I give you using web search. Summarise findings "
            "with links, note disagreements between sources, and keep reports "
            "under 800 words unless asked otherwise."
        ),
        "autonomy_level": 1,
        "capabilities": ["web.search", "library.read", "memory.read", "notes.write"],
        "trigger": {"type": "schedule", "schedule": "weekly", "scheduled_time": "08:00",
                    "scheduled_day": 0},
        "needs": ["topic"],
    },
    {
        "id": "personal-assistant",
        "name": "Personal Assistant",
        "icon": "🗂",
        "description": "Daily briefing and small chores across your workspace.",
        "instructions": (
            "Prepare a short daily briefing: what is on my calendar today, "
            "overdue tasks, and anything I asked you to keep an eye on. Keep it "
            "to a few bullets."
        ),
        "autonomy_level": 1,
        "capabilities": ["calendar.read", "memory.read", "notes.write", "web.search"],
        "trigger": {"type": "schedule", "schedule": "daily", "scheduled_time": "07:30"},
        "needs": [],
    },
    {
        "id": "email-assistant",
        "name": "Email Assistant",
        "icon": "📬",
        "description": "Watches your inbox and drafts replies — never sends on its own.",
        "instructions": (
            "Review recent unread email. Draft replies where a response is "
            "clearly expected, flag anything urgent, and summarise the rest in "
            "one paragraph. Never send email — drafts only."
        ),
        "autonomy_level": 1,
        "capabilities": ["email.read", "email.draft", "memory.read"],
        "trigger": {"type": "interval", "hours": 6},
        "needs": ["email account"],
    },
    {
        "id": "news-monitor",
        "name": "News Monitor",
        "icon": "📰",
        "description": "Watches for news on topics you choose and reports changes.",
        "instructions": (
            "Monitor the topics I configured. Only report when there is "
            "something genuinely new since your last run — no repetition. Rank "
            "items by importance to me."
        ),
        "autonomy_level": 1,
        "capabilities": ["web.search", "memory.read", "notes.write"],
        "trigger": {"type": "interval", "hours": 12},
        "needs": ["topics"],
    },
    {
        "id": "task-manager",
        "name": "Task Manager",
        "icon": "✅",
        "description": "Keeps an eye on task lists and nudges about overdue work.",
        "instructions": (
            "Check for overdue or stale tasks. Suggest re-prioritisation and "
            "flag anything blocked for more than a few days. Do not modify "
            "tasks without asking."
        ),
        "autonomy_level": 1,
        "capabilities": ["memory.read", "notes.write", "calendar.read"],
        "trigger": {"type": "schedule", "schedule": "daily", "scheduled_time": "09:00"},
        "needs": [],
    },
    {
        "id": "document-analyst",
        "name": "Document Analyst",
        "icon": "📄",
        "description": "Reads documents in your library and answers questions over them.",
        "instructions": (
            "Analyse documents I point you at. Produce structured summaries: "
            "key claims, evidence, gaps, and open questions. Quote sparingly "
            "and cite the document you used."
        ),
        "autonomy_level": 0,
        "capabilities": ["library.read", "memory.read"],
        "trigger": {"type": "manual"},
        "needs": ["documents"],
    },
]


def template_by_id(template_id: str) -> Dict[str, Any]:
    for template in TEMPLATES:
        if template["id"] == template_id:
            return template
    return {}


# Kept as BOT_TEMPLATES for the route import.
BOT_TEMPLATES = TEMPLATES
