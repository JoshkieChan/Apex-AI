# Multimodal interaction stays one runtime

M5 closed on `3f91d65d01c2e22d23b57ebe9b3b271d8b0d358c`. This record starts M6 from that head. Decision 0013 described the system before this work and is unchanged.

## Decision

Text, images, and audio enter through `apex.interaction` and the existing provider interface. They do not get a second orchestrator, a second task lifecycle, or a provider SDK type in the core contract.

The authenticated principal comes from the bearer token. The interaction service does not import `apex.execution` and does not write governed memory. Model text is advisory. A transcript is derived. Instructions inside an image or a transcript do not grant permission.

Capabilities are explicit on the configured model. OpenAI and Anthropic image, transcription, and speech flags default to false. The runtime does not switch providers when a flag is off.

Raw bytes are files named by a generated key. PostgreSQL stores metadata and the normalized turn. Request-scoped files are deleted when the turn ends. Conversation retention is off unless both the setting and the request enable it, and those files still expire. Schema revision `0007_interaction` adds `conversations`, `interactions`, and `media_objects`.

Speech failure keeps the text response. A provider failure after the reservation is committed is not sent again. A repeated idempotency key returns the stored turn or `outcome_uncertain` when the reservation never completed.

The browser client is `web/`. It uses the same API and holds no provider key.

## Consequences

JPEG and WebP validation checks the container and dimensions. PNG inflation is bounded by the declared size. That is not a full rasterizer. GIF, M4A, and WebM are out. Live OpenAI and Anthropic perception is not claimed. Real-time duplex voice, video, and a local model server remain later work behind the same provider interface. Deleting a temporary file does not erase a provider log, a backup, or WAL.

## Alternatives rejected

A separate vision agent, a permanent object store, a new Python image library, and automatic provider fallback were rejected. The standard library covers the checks this milestone needs, and fallback would move user content to a provider the operator did not select.
