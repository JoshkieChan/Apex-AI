# Apex sovereignty

Apex is a provider-independent intelligence and execution runtime. One coordinated system uses multiple models and multiple capabilities under one authority model. A chat surface, an agent builder, an MCP monitor, or a workflow engine can be an interface. None of those is the product.

Apex is not a chatbot with a different interface, a wrapper around one provider SDK, a generic agent builder, an MCP test dashboard, or a system that lets the current model decide identity, permission, memory, or execution.

External models are replaceable intelligence providers. They are not the system of record, the identity authority, the permission authority, the memory owner, or the execution authority.

## Principle

One coordinated intelligence, multiple models, multiple capabilities, multiple execution environments, and one consistent authority model.

## Sovereignty dimensions

These are the long-term objectives. A row is not a claim that the capability is finished.

| Dimension | Objective | What the code does now |
| --- | --- | --- |
| Model | Choose, configure, replace, and later host models | `APEX_MODEL_PROVIDER` selects mock, OpenAI, or Anthropic. Modality flags are per configured model and default off for the commercial adapters. There is no silent substitution |
| Intelligence | Keep orchestration independent of any one provider | Intent, planning, coordination, and interaction use normalized contracts. Vendor payloads stay in adapters |
| Data | Control where information is stored, sent, and deleted | PostgreSQL holds metadata and validated JSON. Raw media is a temporary file. Provider transmission happens only inside the selected adapter |
| Memory | Own scoped, versioned, governed memory | `apex.memory` is separate from conversation turns. Interaction does not write model text into trusted memory |
| Identity | Own principals and authentication | A validated RS256 bearer token maps to a principal. Body fields, headers, and query parameters are not authentication |
| Authority | Own permissions, delegation, and approval | Default deny. A plan, a role, and a model response are not grants. The caller cannot approve their own operation |
| Execution | Control tools and external systems | `apex.execution` is the only place an adapter runs. It re-checks the registry, schema, and authority. An operator hold stops a new attempt before dispatch and leaves an uncertain row uncertain |
| Infrastructure | Support operator-controlled deployment | One API process, one worker, and PostgreSQL. Compose is a local scaffold. Kubernetes is not part of this system |
| Governance | Own evaluation, verification, audit, and policy | Deterministic checks decide verification. Model notes are advisory. Interaction logs record ids, modality, size, latency, and outcome |
| Economic | See and later control cost | Token counts are stored when the provider sends them. This repository does not invent a price |

## Current baseline

The implemented path is: authenticate the bearer token, authorize the principal, validate and ingest media, normalize the turn, assemble governed context when a task or project is present, check the configured model's capabilities, call that model, normalize the response, and record the turn. Tool execution remains on the existing task and goal path.

Conversation history is ordered text from completed turns. It is not persistent memory. Request-scoped media is deleted when the turn finishes. Conversation retention requires both `retain_media` and `APEX_MEDIA_CONVERSATION_RETENTION`, and it still expires.

PNG, JPEG, and WebP can be attached. WAV and MPEG-1 Layer III can be transcribed by a model whose transcription capability is enabled. Speech is optional and does not replace the text response. The mock provider exercises these paths in CI. Live provider perception was not run.

The first-party client in `web/` talks to the same `/v1` API. It does not receive provider credentials.

Connections are records of external systems. The MCP fixture, the reference inventory, and the reference device run through the existing execution engine. A connection is not a principal. Discovery does not install a capability, and a model response cannot register one.

## Constraints

- The mock echo is a fixture. It does not measure image or audio understanding.
- OpenAI and Anthropic modality calls are covered with a mock HTTP transport. A live account was not called.
- JPEG and WebP are validated as containers and dimension headers. PNG inflation is bounded. They are not a full image decoder.
- GIF, M4A, and WebM are rejected.
- Temporary files live on the API host. They are not shared across API processes, and deletion here does not erase a provider's copy, a backup, or WAL.
- There is no real-time duplex voice, video, local model server, or public media URL.
- Fallback to another provider is not implemented. Adding one later requires an explicit configuration, a data-classification check, and a test. It must not be implicit.

## Extension points

A new provider implements `ModelProvider` and declares capabilities. A new modality should extend the interaction contract and the provider turn, then pass the existing authority boundary. Local or self-hosted inference belongs behind that same provider interface. It should not become a second orchestrator or a second permission system.
