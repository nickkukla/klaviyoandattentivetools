# migtool

Command-line tools for the Klaviyo, Attentive and STOQ migration. See `docs/REQUIREMENTS.md` for what they do and `docs/BUILD_PLAN.md` for build status.

## Setup

```
uv sync
cp .env.example .env   # then fill in the keys
uv run migtool instances
```

Run the tests with `uv run pytest`.
