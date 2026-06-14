"""Seed a store with demo data so contextgit can be tried in two commands."""

from __future__ import annotations

from typing import Dict

from contextgit.engine import ContextGit


_DEMO_TURNS = [
    ("We're starting a new project called Atlas — a customer analytics dashboard in Next.js.",
     "Great, I've noted the Atlas project: a customer analytics dashboard built with Next.js."),
    ("For Atlas, use PostgreSQL for storage. Remember that the database is Postgres 16 on RDS.",
     "Saved: Atlas uses PostgreSQL 16 on RDS for storage."),
    ("Actually, correction: use MySQL instead of PostgreSQL for Atlas — the platform team standardized on it.",
     "Understood — Atlas now uses MySQL; the earlier PostgreSQL decision is superseded."),
    ("Remember that I prefer concise answers with code examples and no emoji.",
     "Noted your style preference: concise answers, code examples, no emoji."),
    ("TODO: we still need to pick an auth provider for Atlas. Follow up next week.",
     "Open loop recorded: choose an auth provider for Atlas — follow-up next week."),
    ("What did we decide about the Atlas database?",
     "Atlas uses MySQL (this superseded the earlier PostgreSQL choice)."),
    ("By the way, did you watch the game last night? Crazy finish.",
     "I didn't, but it sounds like it was a close one!"),
    ("Here's a temporary scratch value for testing the importer: 0xDEADBEEF. Just a placeholder, ignore it later.",
     "Got it — treating 0xDEADBEEF as a temporary placeholder, not durable information."),
    ("Unrelated: my sister's birthday dinner is at that Italian place on Saturday.",
     "Sounds fun — hope the birthday dinner goes well!"),
    ("For the Atlas frontend, the design team delivered the Figma mockups. The dashboard has a dark theme with a sidebar navigation, KPI cards on top, a retention cohort chart in the middle, and a customer table at the bottom with server-side pagination.",
     "Thanks — I've noted the Atlas dashboard layout: dark theme, sidebar nav, KPI cards, retention cohort chart, and a paginated customer table."),
    ("The Atlas API rate limit from the platform team is 100 requests per second per tenant, burst to 250 for 10 seconds, enforced at the gateway. Retries should use exponential backoff starting at 200ms with jitter, max 5 attempts, and idempotency keys on all POST endpoints.",
     "Recorded Atlas API limits: 100 rps/tenant (burst 250 for 10s), exponential backoff from 200ms with jitter, max 5 retries, idempotency keys on POSTs."),
    ("Also for Atlas: deploys go through GitHub Actions to ECS Fargate, staging auto-deploys from main, production needs a manual approval, and rollbacks pin the previous task definition. Database migrations run in a pre-deploy step with dbmate.",
     "Noted the Atlas deploy pipeline: GitHub Actions to ECS Fargate, auto-deploy staging, manual-approval production, task-definition rollbacks, dbmate migrations pre-deploy."),
    ("Random thought — should I learn Rust this year? Not for Atlas, just curious.",
     "Rust is a great learning investment; for Atlas specifically it isn't needed, but for systems programming curiosity it's worth it."),
]


def seed_demo(engine: ContextGit, conversation_id: str = "demo") -> Dict[str, int]:
    before = engine.runtime.event_journal.event_count()
    for user_prompt, assistant_answer in _DEMO_TURNS:
        engine.commit_turn(user_prompt, assistant_answer, conversation_id=conversation_id)
    after = engine.runtime.event_journal.event_count()
    return {
        "turns": len(_DEMO_TURNS),
        "events_added": after - before,
        "wiki_pages": len(engine.runtime.wiki_store.list_pages()),
    }
