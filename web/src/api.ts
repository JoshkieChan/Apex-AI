export type InputPart =
  | { kind: "text"; text: string }
  | { kind: "image"; mediaId: string }
  | { kind: "audio"; mediaId: string };

export interface InteractionRequest {
  conversationId?: string;
  outputAudio?: boolean;
  parts: InputPart[];
}

export interface InteractionPart {
  kind: string;
  text?: string;
  media_id?: string;
  provenance?: string;
  trust?: string;
}

export interface InteractionResponse {
  id: string;
  conversation_id: string;
  parts: InteractionPart[];
  speech_error?: string | null;
  provider?: string;
  model?: string;
}

export function interactionBody(request: InteractionRequest): Record<string, unknown> {
  const body: Record<string, unknown> = {
    output_modalities: request.outputAudio ? ["text", "audio"] : ["text"],
    parts: request.parts.map((part) => {
      if (part.kind === "text") {
        return { kind: "text", text: part.text };
      }
      return { kind: part.kind, media_id: part.mediaId };
    }),
  };
  if (request.conversationId) {
    body.conversation_id = request.conversationId;
  }
  return body;
}

export function responseText(response: InteractionResponse): {
  transcript: string;
  answer: string;
  audioId: string;
  speechError: string;
} {
  const transcript = response.parts.find((part) => part.kind === "transcript")?.text ?? "";
  const answer = response.parts.find((part) => part.kind === "text")?.text ?? "";
  const audioId = response.parts.find((part) => part.kind === "audio")?.media_id ?? "";
  return {
    transcript,
    answer,
    audioId,
    speechError: response.speech_error ?? "",
  };
}

export async function readError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { error?: string; message?: string };
    return body.message || body.error || `Request failed (${response.status})`;
  } catch {
    return `Request failed (${response.status})`;
  }
}
