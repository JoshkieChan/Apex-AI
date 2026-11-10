import { useRef, useState, type FormEvent } from "react";

import { interactionBody, readError, responseText, type InputPart } from "./api";
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
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);

  function saveToken(value: string) {
    setToken(value);
    sessionStorage.setItem("apex-token", value);
  }

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
        body: JSON.stringify(interactionBody({ parts, outputAudio: wantAudio })),
      });
      if (!response.ok) {
        throw new Error(await readError(response));
      }
      const parsed = responseText(await response.json());
      setTranscript(parsed.transcript);
      setAnswer(parsed.answer);
      setSpeechError(parsed.speechError);
      if (parsed.audioId) {
        const spoken = await fetch(`${apiBase}/v1/media/${parsed.audioId}/content`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (spoken.ok) {
          setAudioUrl(URL.createObjectURL(await spoken.blob()));
        }
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The request failed.");
    } finally {
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
            onChange={(event) => setImages(Array.from(event.target.files ?? []))}
          />
        </label>
        <div className="previews">
          {images.map((image) => (
            <img key={`${image.name}-${image.size}`} alt="" src={URL.createObjectURL(image)} />
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
          <button type="submit" disabled={busy || token.length === 0}>
            {busy ? "Sending" : "Send"}
          </button>
        </div>
      </form>
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
