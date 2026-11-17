import { describe, expect, it } from "vitest";

import {
  claimSubmit,
  interactionBody,
  keepConversation,
  responseText,
  turnsFromConversation,
} from "./api";

describe("interaction request", () => {
  it("maps text, images, and audio without a principal", () => {
    const body = interactionBody({
      conversationId: "conversation-1",
      outputAudio: true,
      parts: [
        { kind: "text", text: "Compare these" },
        { kind: "image", mediaId: "image-1" },
        { kind: "audio", mediaId: "audio-1" },
      ],
    });
    expect(body).toEqual({
      conversation_id: "conversation-1",
      output_modalities: ["text", "audio"],
      parts: [
        { kind: "text", text: "Compare these" },
        { kind: "image", media_id: "image-1" },
        { kind: "audio", media_id: "audio-1" },
      ],
    });
    expect(body).not.toHaveProperty("principal_id");
    expect(body).not.toHaveProperty("idempotency_key");
  });

  it("sends one idempotency key and ignores a second submit", () => {
    const body = interactionBody({
      idempotencyKey: "turn-1",
      parts: [{ kind: "text", text: "hello" }],
    });
    expect(body.idempotency_key).toBe("turn-1");
    const inflight = { current: false };
    expect(claimSubmit(inflight)).toBe(true);
    expect(claimSubmit(inflight)).toBe(false);
    inflight.current = false;
    expect(claimSubmit(inflight)).toBe(true);
  });

  it("keeps the transcript distinct from the answer", () => {
    const parsed = responseText({
      id: "turn-1",
      conversation_id: "conversation-1",
      speech_error: "unsupported",
      parts: [
        { kind: "transcript", text: "heard", provenance: "derived" },
        { kind: "text", text: "answer", trust: "advisory" },
      ],
    });
    expect(parsed).toEqual({
      transcript: "heard",
      answer: "answer",
      audioId: "",
      speechError: "unsupported",
    });
  });

  it("keeps user and assistant turns in order", () => {
    const turns = turnsFromConversation([
      { request_text: "first question", parts: [{ kind: "text", text: "first answer" }] },
      { request_text: "second question", parts: [{ kind: "text", text: "second answer" }] },
    ]);
    expect(turns.map((turn) => `${turn.role}:${turn.text}`)).toEqual([
      "user:first question",
      "assistant:first answer",
      "user:second question",
      "assistant:second answer",
    ]);
  });

  it("keeps the conversation id when a turn fails", () => {
    expect(keepConversation("conversation-1", true)).toBe("conversation-1");
  });
});
