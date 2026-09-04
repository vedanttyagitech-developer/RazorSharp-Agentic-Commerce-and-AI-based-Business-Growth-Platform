# Governed Agentic Commerce Platform

A multi-tenant agentic commerce platform where merchants become discoverable,
conversational and safely transactable by AI buyers.

> **Agents propose; deterministic systems authorize and execute.**

An independent project proposal. Not an official Razorpay, Zepto, Google, OpenAI or NPCI
product, and not live inside any external AI surface.

## The problem it addresses

Most conversational commerce demos stop when the model says the basket is ready. The hard
part is what happens next: an approved payment must stay correct under concurrent
requests, merchant-state changes, uncertain provider outcomes, buyer revocation and
post-capture failure. A deterministic Transaction Assurance Kernel decides whether any
money-moving action is admissible. Razorpay test mode executes. PostgreSQL records truth.

## Current state

See [docs/STATUS.md](docs/STATUS.md). Nothing is claimed as working without a test.

## Development

```bash
uv sync
uv run pytest packages/ -q
uv run ruff check packages/
uv run mypy packages/commerce-domain/src
```

Requires Python 3.14 (managed by `uv`) and PostgreSQL 16.

## Specification

[PROJECT_SPECIFICATION.md](PROJECT_SPECIFICATION.md) is the authoritative design.
