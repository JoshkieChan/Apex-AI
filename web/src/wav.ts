export function encodeMonoWav(samples: Float32Array, sampleRate: number): Blob {
  const bytes = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(bytes);
  write(view, 0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  write(view, 8, "WAVE");
  write(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  write(view, 36, "data");
  view.setUint32(40, samples.length * 2, true);
  let offset = 44;
  for (const sample of samples) {
    const clamped = Math.max(-1, Math.min(1, sample));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    offset += 2;
  }
  return new Blob([bytes], { type: "audio/wav" });
}

export async function recordingToWav(blob: Blob): Promise<File> {
  if (typeof AudioContext === "undefined") {
    throw new Error("This browser cannot convert a recording to WAV.");
  }
  const context = new AudioContext();
  try {
    const decoded = await context.decodeAudioData(await blob.arrayBuffer());
    if (decoded.sampleRate > 48000) {
      throw new Error("The recording sample rate is above 48000 Hz.");
    }
    const wav = encodeMonoWav(decoded.getChannelData(0), decoded.sampleRate);
    return new File([wav], "recording.wav", { type: "audio/wav" });
  } finally {
    await context.close();
  }
}

function write(view: DataView, offset: number, value: string) {
  for (let index = 0; index < value.length; index += 1) {
    view.setUint8(offset + index, value.charCodeAt(index));
  }
}
