# Future Improvements

What to build after the current plan (Phases 01–10) is implemented. Nothing here is in
scope for that plan. The aim is to turn a measured, local investigation agent into a
tool that someone who did not build it would choose to install, trust with their data,
and use during a real incident.

Each item says what it adds, what it builds on, and what makes it hard. Items are
grouped by theme and then ordered in §9.

## 1. Where the current plan stops

The finished plan delivers an agent that investigates files someone has collected and
dropped in, on one machine, and recommends actions without taking them. That leaves
five gaps between it and a tool for general use:

| Gap | Today | What general use needs |
|---|---|---|
| Getting data in | A person exports files and uploads them | The agent reads from where the data already lives |
| When it runs | A person starts it | An alert starts it |
| What it does about it | Recommends actions | Carries out safe ones, with approval |
| Who can use it | One person, locally | A team, with accounts and shared history |
| How much it can be trusted | Measured on one application's benchmark | Measured on varied systems and real incidents |

## 2. Reaching the data where it lives

The largest single step toward general usefulness. Nobody wants to export files in the
middle of an incident.

| Improvement | What it adds | Builds on | Difficulty |
|---|---|---|---|
| **Live connectors** | Read-only tools for metrics, logs and traces backends (Prometheus, Loki, Elasticsearch, Datadog, CloudWatch, OpenTelemetry collectors). The agent queries a time window directly. | The tool registry and result contract (Phase 03); connectors are new tools with the same shape | Each backend has its own query language, auth and rate limits; results must still be capped and citable |
| **Kubernetes and deploy context** | Read-only access to events, pod status, recent rollouts and config changes | Document handling and `dependency_graph` | Permissions; clusters differ widely |
| **Change history** | Recent deploys, feature flags and config changes pulled from CI/CD and version control, lined up against the onset time | `find_onset`; the config-diff field case | Most incidents follow a change, so this is high value; sources are scattered |
| **Service map import** | Topology from a service catalogue or mesh when traces do not provide it | `dependency_graph` | Keeping it current |
| **More file formats** | Archives, multi-line stack traces, OpenTelemetry exports, nested JSON, compressed logs | Ingest classifier and loaders | Mostly effort, little risk |
| **The agent fetches what it asks for** | A data request the agent raises is fulfilled by a connector automatically, within limits, instead of waiting for a person | Data requests and resumable runs (Phase 05) | Needs per-source permissions and a cost and volume budget per fetch |
| **MCP in both directions** | Consume existing MCP servers as tools; expose the investigator as an MCP server so other agents and IDEs can call it | Tool registry | Tool results from third-party servers are untrusted input |

## 3. Running at the right moment

| Improvement | What it adds | Builds on | Difficulty |
|---|---|---|---|
| **Alert-triggered investigations** | A webhook from an alerting system (Alertmanager, PagerDuty, Opsgenie) starts a run with the alert as the task | The run function shared by app and evals | Needs a long-running service, queueing and de-duplication of alert storms |
| **Chat integration** | Results posted to the incident channel (Slack, Teams), with follow-up questions answered in the thread | Report rendering | Message formatting; access control in shared channels |
| **Conversational follow-up** | After a report, ask "why not service X?" or "check the last hour too" and continue the same run | Resumable runs; the ledger | Keeping follow-ups grounded under the same citation rules |
| **Progressive answers** | A first, cheap answer within seconds from statistics alone, refined as the investigation proceeds | The single-call baseline (Phase 06) | Presenting a changing answer without confusing people |
| **Continuous watch** | Onset detection running on live data, proposing an investigation before anyone is paged | `find_onset` | False alarms; this is a monitoring product in its own right |
| **Scheduled health reviews** | A periodic "anything drifting?" pass over a system | `no_fault_found` outcome | Cost control; alert fatigue |

## 4. Acting, safely

The original idea for this project ended with "acts accordingly". The plan stops at
recommendations on purpose. These steps close that gap in order of risk.

| Step | What it adds | Guard |
|---|---|---|
| **1. Dry run** | For each recommended action, the exact commands or API calls it would make, and what would change | Nothing is executed |
| **2. Read-only checks, run automatically** | The verification checks the report lists (is the pod restarting, is the disk full) are run and their results attached | Read-only credentials only |
| **3. Approved actions** | A person approves a specific reversible action (restart, scale, roll back); the agent runs it and then verifies recovery from the data | One approval per action; an allow-list of action types; a recorded audit trail |
| **4. Runbook execution** | The agent follows the team's own runbook step by step, pausing where it says to | Runbooks as documents (Phase 03); approval at marked steps |
| **5. Recovery verification** | After any action, the agent checks whether the symptoms went away, and says so if they did not | The before/after tools, applied to a new window |

Requirements before any of step 3 onward: a test environment with fault injection (for
example a local cluster with a chaos tool) to measure whether actions actually fix
things, separate credentials for reading and writing, and a hard rule that disruptive
actions are never taken without a person.

## 5. Making it trustworthy

| Improvement | What it adds | Notes |
|---|---|---|
| **Evidence on more systems** | Results on RCAEval's other applications (Sock Shop, Train Ticket) and other benchmarks, so claims are not limited to one system | The current results show generalisation to new fault scenarios in one application only |
| **Real incidents** | A set of real, anonymised incidents with known causes, contributed by users | The field set is self-authored and small; this is what would replace it |
| **Repeated runs** | Several runs per configuration, so differences come with a measured variance | Limited today by budget, not design |
| **Fair model comparison** | Prompts tuned per model before comparing, or a neutral prompt set | Today the comparison favours the model the prompts were tuned on |
| **Multiple root causes** | Diagnoses with more than one cause, or a cause plus a contributing condition | The ledger ranks hypotheses as alternatives; it needs a way to hold them jointly |
| **Calibrated confidence** | A confidence that means something: of the cases marked "high", most are right | Needs many more scored cases than the benchmark has |
| **Semantic retrieval** | Embeddings for documents and incident memory, behind the existing tools, so paraphrases match | Deferred in Phases 03 and 09 with the interface kept ready |
| **Learning from feedback** | Confirmed and rejected diagnoses improve retrieval ranking and seed regression cases automatically | Memory outcomes (Phase 09) already record the signal |
| **Stronger injection defences** | Beyond the planted-instruction test: a separate screening pass on retrieved text, and red-team cases contributed over time | One test case does not make a defence |
| **Regression suite from trajectories** | Saved trajectories replayed against new versions to catch behaviour changes | Replay exists (Phase 05); this makes it a gate |

## 6. Privacy and control over data

Often the deciding factor for whether a team is allowed to use a tool like this.

| Improvement | What it adds |
|---|---|
| **Local and self-hosted models** | Run entirely on the user's hardware through a local model server, so no data leaves the network. LiteLLM already supports this; the work is tuning and measuring quality on smaller models. |
| **Real redaction** | Structured detection of secrets and personal data before anything is sent to a provider, with a report of what was masked. The plan's version is pattern-based and best-effort. |
| **Data retention controls** | Configurable retention for uploads, trajectories and memory; export and deletion on request |
| **Per-source data policies** | Mark a source as "never send raw rows to a model"; the agent then works on aggregates only |
| **Audit log** | Who ran what, on which data, with which model, at what cost |
| **Provider choice per task** | A cheap model for routine steps, a stronger or a local one for sensitive data, set by policy |

## 7. Usable by a team, and by anyone

| Improvement | What it adds |
|---|---|
| **One-command install** | A published package and a container image, with `docker compose up` bringing up the app and a sample dataset |
| **A demo that needs no keys** | Bundled sample incidents with saved trajectories, so someone can see the tool work in a minute before configuring anything. Replay (Phase 05) makes this cheap. |
| **Setup wizard** | Guided first run: choose a provider or local model, set a budget, test the connection |
| **Accounts and shared history** | Sign-in, workspaces, and an incident history the whole team can search |
| **Roles and permissions** | Who can run investigations, who can approve actions, who can see which sources |
| **API and CLI as first-class interfaces** | Everything the page can do, scriptable; the CLI already exists in Phase 05 in a minimal form |
| **Shareable reports** | A link or exported page for a finished investigation, with the evidence expandable |
| **Postmortem drafts** | A structured incident write-up (timeline, cause, impact, actions, follow-ups) generated from the trajectory, for a person to edit |
| **Ticketing integration** | Follow-up actions filed as tickets; the incident linked both ways |
| **Explanations for non-specialists** | A plain-language summary beside the technical one, and a glossary of the terms used |
| **Cost transparency** | An estimate before each run and a per-team budget, building on the spend guard |
| **Plugin interface** | A documented way to add tools, loaders and connectors without changing the core |
| **Documentation** | Quickstart, how-to guides per connector, a description of what the agent can and cannot conclude, and contribution guidelines |

## 8. Engineering to support the above

| Improvement | Why it becomes necessary |
|---|---|
| **Service architecture** | Alert triggers, accounts and shared history need a server with a job queue and a real database, not a local Streamlit page |
| **Scaling ingest** | Large log volumes need sampling, streaming or pushing queries down to the source instead of loading everything locally |
| **Tracing of the agent itself** | Standard telemetry for runs (latency, cost, errors per tool and per model) so operators can monitor the tool like any other service |
| **Release process** | Versioned releases, a changelog, upgrade notes, and dependency and security scanning in CI |
| **Cross-platform support** | Tested on Linux, macOS and Windows |
| **Internationalisation** | Logs and documents in languages other than English; report output in the user's language |

## 9. Suggested order

Ordered by how much each step widens who can use the tool, against its cost and risk.

| Order | Step | Why here |
|---|---|---|
| 1 | Demo without keys; one-command install; documentation | Cheap, and nothing else matters if people cannot try it |
| 2 | Local and self-hosted models; real redaction | Removes the most common reason a team cannot use it at all |
| 3 | Live connectors for one metrics and one logs backend; change history | The largest gain in day-to-day usefulness |
| 4 | Evidence on more systems; repeated runs; multiple root causes | Trust has to grow with reach |
| 5 | Alert triggers and chat integration | Needs the service architecture, so it follows the connectors |
| 6 | Conversational follow-up; postmortem drafts; shareable reports | Makes the output part of the team's routine |
| 7 | Dry run, then automatic read-only checks | The first safe steps toward acting |
| 8 | Accounts, roles, shared history, audit log | Needed before approved actions, since approval needs identity |
| 9 | Approved actions with recovery verification; runbook execution | Last, because mistakes here cause incidents instead of explaining them |

## 10. Limits of the current plan, to state in the report

These are known and not fixed by the current plan. Phase 10 lists them in `REPORT.md`.

- **One application.** Tuning and held-out cases both come from Online Boutique.
- **Model comparison favours the tuned model.** Prompts are tuned with one model and
  run unchanged on the others.
- **One root cause per incident.** Simultaneous or contributing causes are reported as a
  winner and a loser.
- **No action is taken.** Recommendations only.
- **Upload-path evidence is thin.** Seven self-authored cases.
- **Fault start time is given** on the benchmark; detection is measured only for
  `find_onset` in isolation.
