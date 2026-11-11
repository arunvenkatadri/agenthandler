# Terms of Service

**Effective 11 November 2026**

These terms cover the AgentHandler plugin (the `agenthandler` skill distributed
through agent plugin directories) and the AgentHandler open-source Python library.

## 1. The software is the agreement

AgentHandler is free and open-source software licensed under the
[Apache License, Version 2.0](https://github.com/arunvenkatadri/agenthandler/blob/main/LICENSE).
That licence governs your rights to use, copy, modify and distribute it. Where
these terms and the Apache 2.0 licence differ, **the licence controls.**

Nothing here grants rights the licence withholds, or withholds rights the licence
grants.

## 2. There is no service

We operate no servers and host nothing on your behalf. There is no account, no
uptime commitment, no support obligation, and no service level of any kind. The
plugin is a document; the library is code that runs on your own machines.

## 3. No warranty

As stated in Section 7 of the Apache 2.0 licence, the software is provided on an
**"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND**, express or
implied.

**This matters more than usual here.** AgentHandler is a governance library. Its
budgets, approval gates, circuit breakers and guardrails are engineering controls,
not guarantees. They can be misconfigured, they can contain defects, and they do
not make an autonomous agent safe. Known defects are tracked publicly in our
[issue tracker](https://github.com/arunvenkatadri/agenthandler/issues) — read it
before relying on any control for something that matters.

You are responsible for what your agents do. Do not deploy AgentHandler as the
only thing standing between an agent and an irreversible action.

## 4. Limitation of liability

As stated in Section 8 of the Apache 2.0 licence, in no event and under no legal
theory shall any contributor be liable for damages arising out of the use or
inability to use the software, including any direct, indirect, special,
incidental or consequential damages.

## 5. Your responsibilities

You agree to use AgentHandler in compliance with applicable law, and in
compliance with the terms of any model provider, message broker or other service
you connect it to. You are responsible for the credentials you configure it with
and for securing the audit logs it produces.

## 6. Changes

These terms may be revised; the effective date above will change and the history
is public in the
[repository](https://github.com/arunvenkatadri/agenthandler/commits/main/TERMS.md).
Your continued use after a revision constitutes acceptance. The Apache 2.0 licence
on any version you already received is irrevocable and is not affected.

## 7. Contact

Open an issue at
<https://github.com/arunvenkatadri/agenthandler/issues>.
