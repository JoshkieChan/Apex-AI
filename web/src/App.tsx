import { useEffect, useRef, useState, type FormEvent } from "react";

import {
  claimSubmit,
  interactionBody,
  keepConversation,
  readError,
  responseText,
  turnsFromConversation,
  type ConversationTurn,
  type InputPart,
} from "./api";
import { recordingToWav } from "./wav";

const apiBase = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000";

export function App() {
  const [token, setToken] = useState(sessionStorage.getItem("apex-token") ?? "");
  const [text, setText] = useState("");
  const [images, setImages] = useState<File[]>([]);
  const [audio, setAudio] = useState<File | null>(null);
  const [recordState, setRecordState] = useState("idle");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [transcript, setTranscript] = useState("");
  const [answer, setAnswer] = useState("");
  const [speechError, setSpeechError] = useState("");
  const [audioUrl, setAudioUrl] = useState("");
  const [wantAudio, setWantAudio] = useState(false);
  const [conversationId, setConversationId] = useState(
    sessionStorage.getItem("apex-conversation") ?? "",
  );
  const [turns, setTurns] = useState<ConversationTurn[]>([]);
  const [historyError, setHistoryError] = useState("");
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const spokenUrl = useRef("");
  const previewUrls = useRef<string[]>([]);
  const sending = useRef(false);

  function saveToken(value: string) {
    setToken(value);
    sessionStorage.setItem("apex-token", value);
  }

  function rememberConversation(value: string) {
    setConversationId(value);
    if (value) {
      sessionStorage.setItem("apex-conversation", value);
    } else {
      sessionStorage.removeItem("apex-conversation");
    }
  }

  async function loadHistory(id: string) {
    if (!id || !token) {
      return;
    }
    setHistoryError("");
    const response = await fetch(`${apiBase}/v1/conversations/${id}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) {
      setHistoryError(await readError(response));
      return;
    }
    const payload = (await response.json()) as {
      interactions?: Parameters<typeof turnsFromConversation>[0];
    };
    setTurns(turnsFromConversation(payload.interactions ?? []));
  }

  function startNewConversation() {
    rememberConversation("");
    setTurns([]);
    setHistoryError("");
    setError("");
    setAnswer("");
    setTranscript("");
  }

  useEffect(() => {
    return () => {
      if (spokenUrl.current) {
        URL.revokeObjectURL(spokenUrl.current);
      }
      previewUrls.current.forEach((url) => URL.revokeObjectURL(url));
    };
  }, []);

  useEffect(() => {
    if (!conversationId || !token) {
      return;
    }
    void loadHistory(conversationId).catch((caught: unknown) => {
      setHistoryError(caught instanceof Error ? caught.message : "The conversation could not be read.");
    });
  }, [conversationId, token]);

  async function toggleRecording() {
    if (!navigator.mediaDevices || typeof MediaRecorder === "undefined") {
      setRecordState("unsupported");
      return;
    }
    if (recorder.current && recorder.current.state === "recording") {
      recorder.current.stop();
      setRecordState("stopped");
      return;
    }
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const media = new MediaRecorder(stream);
    chunks.current = [];
    media.ondataavailable = (event) => {
      if (event.data.size > 0) {
        chunks.current.push(event.data);
      }
    };
    media.onstop = () => {
      stream.getTracks().forEach((track) => track.stop());
      const blob = new Blob(chunks.current, { type: media.mimeType || "audio/webm" });
      void recordingToWav(blob).then(
        (file) => setAudio(file),
        (caught: unknown) =>
          setError(caught instanceof Error ? caught.message : "The recording could not be converted."),
      );
    };
    media.start();
    recorder.current = media;
    setRecordState("recording");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!claimSubmit(sending)) {
      return;
    }
    setBusy(true);
    setError("");
    setAudioUrl("");
    try {
      const parts: InputPart[] = [];
      if (text.trim()) {
        parts.push({ kind: "text", text: text.trim() });
      }
      for (const image of images) {
        parts.push({ kind: "image", mediaId: await upload(image) });
      }
      if (audio) {
        parts.push({ kind: "audio", mediaId: await upload(audio) });
      }
      if (parts.length === 0) {
        throw new Error("Add text, an image, or audio.");
      }
      const response = await fetch(`${apiBase}/v1/interactions`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(
          interactionBody({
            conversationId: conversationId || undefined,
            idempotencyKey: crypto.randomUUID(),
            parts,
            outputAudio: wantAudio,
          }),
        ),
      });
      if (!response.ok) {
        rememberConversation(keepConversation(conversationId, true));
        throw new Error(await readError(response));
      }
      const body = (await response.json()) as Parameters<typeof responseText>[0];
      const parsed = responseText(body);
      rememberConversation(body.conversation_id);
      await loadHistory(body.conversation_id);
      setTranscript(parsed.transcript);
      setAnswer(parsed.answer);
      setSpeechError(parsed.speechError);
      if (parsed.audioId) {
        const spoken = await fetch(`${apiBase}/v1/media/${parsed.audioId}/content`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (spoken.ok) {
          if (spokenUrl.current) {
            URL.revokeObjectURL(spokenUrl.current);
          }
          spokenUrl.current = URL.createObjectURL(await spoken.blob());
          setAudioUrl(spokenUrl.current);
        }
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The request failed.");
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }

  async function upload(file: File): Promise<string> {
    const body = new FormData();
    body.append("file", file);
    const response = await fetch(`${apiBase}/v1/media`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body,
    });
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    const payload = (await response.json()) as { id?: string };
    if (!payload.id) {
      throw new Error("The upload did not return media.");
    }
    return payload.id;
  }

  const recording =
    typeof MediaRecorder === "undefined" ? "Recording is not available in this browser." : recordState;

  return (
    <main>
      <h1>Apex</h1>
      <p>Text, images, and audio use the same signed-in Apex API.</p>
      <form onSubmit={(event) => void submit(event)}>
        <label>
          Bearer token
          <input
            type="password"
            autoComplete="off"
            value={token}
            onChange={(event) => saveToken(event.target.value)}
          />
        </label>
        <label>
          Message
          <textarea value={text} onChange={(event) => setText(event.target.value)} rows={4} />
        </label>
        <label>
          Images
          <input
            type="file"
            accept="image/png,image/jpeg,image/webp"
            multiple
            onChange={(event) => {
              previewUrls.current.forEach((url) => URL.revokeObjectURL(url));
              const files = Array.from(event.target.files ?? []);
              previewUrls.current = files.map((file) => URL.createObjectURL(file));
              setImages(files);
            }}
          />
        </label>
        <div className="previews">
          {previewUrls.current.map((url) => (
            <img key={url} alt="" src={url} />
          ))}
        </div>
        <label>
          Audio file
          <input
            type="file"
            accept="audio/wav,audio/mpeg,audio/webm"
            onChange={(event) => setAudio(event.target.files?.[0] ?? null)}
          />
        </label>
        <div className="row">
          <button type="button" onClick={() => void toggleRecording()}>
            {recordState === "recording" ? "Stop recording" : "Record audio"}
          </button>
          <span>{recording}. Recordings are converted to mono WAV. WebM is not uploaded.</span>
          <label>
            <input
              type="checkbox"
              checked={wantAudio}
              onChange={(event) => setWantAudio(event.target.checked)}
            />
            Request spoken reply
          </label>
          <button type="button" onClick={startNewConversation}>
            New conversation
          </button>
          <button type="submit" disabled={busy || token.length === 0}>
            {busy ? "Sending" : "Send"}
          </button>
        </div>
      </form>
      {historyError ? <p className="error">{historyError}</p> : null}
      {turns.length > 0 ? (
        <ol>
          {turns.map((turn, index) => (
            <li key={`${turn.role}-${index}`}>
              {turn.role === "user" ? "You" : "Apex"}: {turn.text}
            </li>
          ))}
        </ol>
      ) : null}
      {error ? <p className="error">{error}</p> : null}
      {transcript ? (
        <section>
          <h2>Transcript</h2>
          <pre>{transcript}</pre>
        </section>
      ) : null}
      {answer ? (
        <section>
          <h2>Response</h2>
          <pre>{answer}</pre>
          {speechError ? <p>Speech was not generated: {speechError}</p> : null}
          {audioUrl ? <audio controls src={audioUrl} /> : null}
        </section>
      ) : null}
    </main>
  );
}
