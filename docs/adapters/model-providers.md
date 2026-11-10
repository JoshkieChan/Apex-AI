# Model adapters

`ModelProvider.complete_structured` accepts a `ModelRequest` and returns a `ModelResponse` with parsed JSON, provider name, model, latency, and token counts when the provider reports them. Callers validate `content` with Pydantic. Extra fields are rejected by the target schema.

## Selection

`APEX_MODEL_PROVIDER` is `mock` (default), `openai`, or `anthropic`. There is no cost or latency router.

The mock provider returns scripted JSON for `IntentDraft`, `CoordinationNote`, and `VerificationNote`. It is the provider used by tests. It does not open a socket.

## How model output is used

The Coordinator and Intent engines may call the provider. Capability selection stays on the deterministic interpreter. Names that are not registered, or not already selected, are recorded as ignored. The Verifier may store an advisory note. If that note disagrees with the checks, `advisory_disagrees` is true and the status stays failed or inconclusive.

Provider failures are logged and ignored for advisory calls. They do not grant permission and they do not block a deterministic task.

## OpenAI

`OpenAIAdapter` posts to `{OPENAI_BASE_URL}/chat/completions` with `response_format.type = json_schema` and `strict: true`. The shape matches the Chat Completions structured-output guide as checked on 2026-11-08. The key is `OPENAI_API_KEY`. The model is `OPENAI_MODEL`. Both must be set or the adapter raises `ProviderError` before any call. HTTP 401 and 403 become `authentication`. The response body is not copied into the error.

This adapter is unit-tested with `httpx.MockTransport`. It has not been called against a live account in this milestone.

## Anthropic

`AnthropicAdapter` posts to `{ANTHROPIC_BASE_URL}/v1/messages` with `output_config.format.type = json_schema` and header `anthropic-version` (default `2026-10-21`, from the structured-output docs checked on 2026-11-08). System messages are sent in the `system` field. The key is `ANTHROPIC_API_KEY` and the model is `ANTHROPIC_MODEL`.

Same limitation: mocked tests only, no live compatibility claim.

## Modalities

`ModelProvider.capabilities` returns text, image understanding, audio transcription, and audio generation for the configured model. The flags default to false on OpenAI and Anthropic. Setting the provider name does not enable a modality, and a failed modality does not select another provider.

`respond` accepts a `ProviderTurn`: policy text, prior text, and normalized parts. Images are bytes plus a media type. The adapter builds the vendor payload. OpenAI image input is a data URL on `{OPENAI_BASE_URL}/chat/completions` when `APEX_OPENAI_IMAGE_UNDERSTANDING` is true. Anthropic image input is a base64 source on `/v1/messages` when `APEX_ANTHROPIC_IMAGE_UNDERSTANDING` is true. Anthropic transcription and speech always raise `unsupported`.

OpenAI transcription is `POST /audio/transcriptions` when `APEX_OPENAI_TRANSCRIPTION` is true and `APEX_OPENAI_TRANSCRIPTION_MODEL` is set. OpenAI speech is `POST /audio/speech` with `response_format` `wav` when `APEX_OPENAI_SPEECH` is true and `APEX_OPENAI_SPEECH_MODEL` is set. The returned audio must be a RIFF file within `APEX_MEDIA_MAX_BYTES`. Token counts are copied only when the provider sends integers. This repository does not invent a price.

The mock provider declares all four capabilities. `respond` echoes `turns:{n}` and a short digest of each image. `transcribe` returns `transcript:{digest}`. `synthesize` returns a short WAV. Those strings are fixtures. They are not a measurement of perception quality. The tests use `httpx.MockTransport` for the commercial adapters and do not call live accounts.
