import { describe, expect, it } from "vitest";

import { interactionBody, responseText } from "./api";

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
});
