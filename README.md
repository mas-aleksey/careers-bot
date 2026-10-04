# careers-bot

A Telegram bot that watches companies' career pages and sends a person only the
openings that actually fit them.

You send a CV and answer five short questions. The bot builds a profile, walks
company job boards twice a day, scores new openings against your profile and
delivers whatever clears your threshold.

Self-hosted, long-polling (no public URL or webhook needed), SQLite for state.

## What it does

| | |
| --- | --- |
| `/profile` | how the bot understood you, with buttons |
| `/cv` | send a new CV, profile is rebuilt |
| `/edit <text>` | fix the profile in plain words: "I'd consider hybrid in Lisbon" |
| `/add <link or name>` | track a company: it finds the job board itself |
| `/settings` | match threshold and delivery mode, as buttons |
| `/pause`, `/resume` | mute and unmute |
| `/invite`, `/users`, `/revoke` | admin only |

## Where the jobs come from

Eleven ATS providers through their public APIs, no keys required: **Greenhouse,
Ashby, Lever** (including the EU host), **SmartRecruiters, Workable, Recruitee,
Teamtailor, Pinpoint, Personio** (XML feed), **Revolut People**, plus the
WordPress REST API and openings embedded straight into a Next.js page.

Given a company name or a link, the bot tries each provider until a board
answers. Roughly half of the companies you throw at it turn out to have one.
For the rest it watches the career page and reports when it changes.

What it stores per opening, when the provider exposes it: publication date
(shown as "posted 3 days ago" or "open for 1 y 10 mo"), salary range, hiring
manager, workplace type. Openings that disappear from a board are marked closed
and never sent.

## Matching

A hundred points split three ways: stack and domain 40, role and seniority 35,
location and employment 25. Money is not part of the score — ranges are
published for about one opening in seven, so scoring them would just shift the
scale for everyone equally.

Blockers score a flat zero rather than a deduction: seniority below yours, a
country you can't be hired from, onsite or relocation when you don't want it,
a different profession. Without that rule a perfect stack match in a city you
can't move to would still clear an 80% threshold.

## Setup

```bash
cp .env.example .env     # bot token from @BotFather, OpenRouter key
chmod 600 .env
docker compose up -d --build
docker compose logs -f careers-bot
```

The first person to message the bot becomes the admin — recorded once. After
that access is invite-only: `/invite` mints a code that lives 48 hours.

## Cost

The model is called twice: once to build a profile, then in batches of twenty
to score new openings. At a typical load — fifty new openings a day — that is
about **$3/month** on Claude Sonnet 5 through OpenRouter. Cheaper models work
too; swap `OPENROUTER_MODEL`, see `.env.example` for figures.

## Layout

```
src/storage.py   one connection, the whole schema, migrations, audit log
src/jobs.py      collector: registry -> ATS APIs -> jobs, companies
src/llm.py       OpenRouter client: one POST, JSON back
src/bot.py       Telegram, profiles, scoring, delivery
tests/           no network, no tokens, no fixtures
```

Three threads in one process: Telegram polling, collection every 12 hours,
scoring every 15 minutes. All state lives in `data/bot.db`.

CVs are not kept: the PDF lands in `/tmp`, text is extracted, the file is
deleted. Only the resulting profile and the answers stay in the database.

## Tests

No network, no tokens, no fixtures — everything runs against an in-memory
database and hand-written payloads:

```bash
pip install pytest
python3 -m pytest
```

## License

MIT
