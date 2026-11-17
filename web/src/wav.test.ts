import { describe, expect, it } from "vitest";

import { encodeMonoWav } from "./wav";

describe("wav encoder", () => {
  it("writes a mono RIFF file", async () => {
    const wav = encodeMonoWav(new Float32Array([0, 0.5, -0.5]), 8000);
    const bytes = new Uint8Array(await wav.arrayBuffer());
    expect(String.fromCharCode(...bytes.slice(0, 4))).toBe("RIFF");
    expect(String.fromCharCode(...bytes.slice(8, 12))).toBe("WAVE");
    expect(bytes.length).toBe(44 + 6);
    expect(wav.type).toBe("audio/wav");
  });
});
