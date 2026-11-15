# Documentation map

Every page, what it answers, and who it is for. Start with the [README](../README.md).

## Using the server

| Page | Answers |
|---|---|
| [Getting started](getting-started.md) ([NL](getting-started.nl.md)) | How do I install, configure, upgrade and troubleshoot it? |
| [Tool reference](tool-reference.md) | Which tools exist, how guarded writes work, what the limits are |
| [Workflow catalogue](workflow-catalogue.md) | Which end-to-end workflows are tested (generated, do not edit) |
| [OAuth](oauth.md) | How to connect with your own OAuth app instead of a token; scopes |
| [Reading PDF attachments](reading_pdf_attachments.md) | What PDF reading can and cannot do |
| [Moneybird API coverage](moneybird_api_coverage.md) | Which Moneybird API endpoints have a tool |

## Safety and data

| Page | Answers |
|---|---|
| [Deployment and safety](deployment-and-safety.md) | Read-only default, enabling writes, HTTP mode, credentials |
| [Threat model](threat_model.md) | What is protected, against what, and the remaining risk |
| [Data handling](data_handling.md) | What kinds of data the server touches and how it treats them |
| [Local data lifecycle](data-lifecycle.md) ([NL](data-lifecycle.nl.md)) | What is stored locally and how to back it up or delete it |

## Working on the code

| Page | Answers |
|---|---|
| [AGENTS.md](../AGENTS.md) | Rules and checks for anyone (human or AI) changing this repository |
| [Architecture](architecture.md) | Which files to read for each kind of change |
| [Roadmap](roadmap.md) | Open tasks |
| [Contributing](../CONTRIBUTING.md) | Licence terms for contributions, safety rules, PR checklist |
| [Releasing](releasing.md) | How a version is published to PyPI and GitHub |
| [Changelog](../CHANGELOG.md) | What changed in each version |

## Where facts live

- The version number lives only in `pyproject.toml`; other files must match it.
- `python scripts/check_docs.py` checks links, anchors and these facts offline.
- Words have exact meanings: *implemented* (in the code), *tested* (a test covers it),
  *published* (on PyPI) and *live-verified* (checked against a real Moneybird administration).
  A version bump is not a publication.
