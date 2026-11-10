# Evaluation

Apex separates deterministic system checks from model-output quality. A fluent model response is not a passed verification.

## Deterministic suite

`pytest` is the release evaluation. It runs without a paid provider key. The mock provider is the model fixture. The cases that gate v1.0 are:

| Area | Where it is asserted |
| --- | --- |
| Authentication and principal mapping | `tests/security/test_authentication.py`, `tests/security/test_dev_tokens.py`, `tests/security/test_jwks.py` |
| Authorization, delegation, and approval binding | `tests/security/test_boundaries.py`, `tests/security/test_resource_scope.py`, `tests/unit/test_intent_plan_authority.py` |
| Memory scope, conflict, and purge | `tests/security/test_memory.py`, `tests/integration/test_memory_purge_postgres.py` |
| Coordination, role ceilings, and recovery | `tests/security/test_coordination.py`, `tests/security/test_m51_reliability.py`, `tests/security/test_m52_operations.py` |
| Remote MCP certainty | `tests/integration/test_remote_dispatch.py`, `tests/security/test_remote_mcp.py` |
| Multimodal validation and untrusted instructions | `tests/security/test_m6_interaction.py`, `tests/unit/test_media_formats.py` |
| Connectors, devices, and schema drift | `tests/security/test_m7_connectors.py` |
| Execution hold | `tests/security/test_m8_release.py` |
| Fresh schema and downgrade | `tests/integration/test_migrations.py` |

CI also runs the disposable Compose probe and the disposable backup restore. Those are not unit tests.

## Model quality

There is no scored model benchmark in this repository. OpenAI and Anthropic adapters are tested with a mock HTTP transport. A live model was not graded. Instruction adherence, perception quality, and refusal quality of a commercial model are unknown until an operator runs that model against a recorded set and stores the provider, model name, configuration, and result outside this suite.

Do not treat the mock echo as a measure of understanding. Do not change `APEX_MODEL_PROVIDER` or a modality flag without recording the change. The factory does not substitute another provider when the configured one fails.

## Adversarial fixtures

The deterministic tests reject model text that asks for `authority.approve`, image or audio instructions that ask for a tool, connector metadata that tries to choose its own risk, and a delegation of `operations.suspend`. Those fixtures are security checks. They are not a claim that every prompt-injection wording has been enumerated.

## Improvement

A verified task, a corrected memory, or an evaluation note can inform an operator. The process does not train a model on user data, does not promote model text into verified memory, and does not let a model edit grants, approvals, or this architecture. A change to those controls is a reviewed code change.
