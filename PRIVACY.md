# Privacy Policy

**Effective 11 November 2026**

This policy covers the AgentHandler plugin (the `agenthandler` skill distributed
through agent plugin directories) and the AgentHandler open-source Python library.

## The short version

AgentHandler collects nothing. There is no server to collect it with.

## What the plugin is

The plugin contains a single skill: one Markdown document that teaches a coding
assistant how to use the AgentHandler library. It ships no executable code, makes
no network requests, and bundles no MCP server. Installing it adds documentation
to your assistant's context and nothing else.

## Data we collect

**None.** We operate no servers, no analytics, no telemetry, and no accounts. We
receive no data about you, your code, your agents, or your use of either the
plugin or the library.

## The library

The `agenthandler` library runs entirely inside your own process. It has no
required third-party dependencies and contains no telemetry, usage reporting, or
phone-home behaviour of any kind. Specifically:

- **Audit logs** are written to the sink you configure, in your own
  infrastructure. They are never transmitted to us.
- **Budgets, approvals and policy decisions** are evaluated locally. No
  supervision decision leaves your process.
- **Optional connectors** (Kafka, Redis, Kinesis) and **optional OAuth
  verification** connect only to the endpoints *you* configure, using credentials
  you supply. We are not a party to those connections.
- **Secrets redaction** operates on data in memory and writes nothing externally.

## Third parties

We share nothing with third parties, because we hold nothing to share.

Your use of other services alongside AgentHandler is governed by their own
policies, not this one. That includes your model provider (for example OpenAI or
Anthropic), whose terms govern the prompts and completions your agent sends them,
and GitHub, which hosts our repository and issue tracker.

If you contact us by opening a GitHub issue, that issue is public and governed by
[GitHub's Privacy Statement](https://docs.github.com/site-policy/privacy-policies/github-general-privacy-statement).

## Children

AgentHandler is a developer tool and is not directed to children under 13.

## Changes

Material changes will be published in this file, with the effective date above
updated. The revision history is public in the
[repository](https://github.com/arunvenkatadri/agenthandler/commits/main/PRIVACY.md).

## Contact

Open an issue at
<https://github.com/arunvenkatadri/agenthandler/issues>.
