export class ApiError extends Error {
  constructor(code, status = 0) {
    super(code);
    this.code = code;
    this.status = status;
  }
}
const utf8 = new TextDecoder("utf-8", { fatal: true });
export async function readJson(response) {
  if ([401, 403].includes(response.status)) {
    await response.body?.cancel().catch(() => {});
    throw new ApiError("forbidden", response.status);
  }
  if (!response.headers.get("content-type")?.startsWith("application/json"))
    throw new ApiError("invalid_response", response.status);
  let bytes = 0;
  const pieces = [];
  const reader = response.body.getReader();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > 131072) throw new ApiError("response_limit");
      pieces.push(value);
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
  const all = new Uint8Array(bytes);
  let at = 0;
  for (const piece of pieces) {
    all.set(piece, at);
    at += piece.length;
  }
  let value;
  try {
    value = JSON.parse(utf8.decode(all));
  } catch {
    throw new ApiError("invalid_json");
  }
  if (!response.ok) {
    const code = value?.detail?.code;
    throw new ApiError(
      typeof code === "string" && /^[a-z_]{1,80}$/.test(code)
        ? code
        : "request_rejected",
      response.status,
    );
  }
  return value;
}
export async function request(
  path,
  { signal, body, key, fetcher = fetch } = {},
) {
  const registration = [
    "/v1/environments",
    "/v1/workloads",
    "/v1/plans",
  ].includes(path);
  if (
    (!registration &&
      !/^\/v1\/plans\/[A-Za-z0-9][A-Za-z0-9_/-]*$/.test(path)) ||
    (registration && body === undefined)
  )
    throw new ApiError("invalid_api_path");
  const headers = { Accept: "application/json" };
  if (body !== undefined) {
    if (typeof key !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(key))
      throw new ApiError("idempotency_key_required");
    Object.assign(headers, {
      "Content-Type": "application/json",
      "X-Bravo-Console": "1",
      "Idempotency-Key": key,
    });
  }
  const response = await fetcher(path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    redirect: "error",
    cache: "no-store",
    signal,
    headers,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  return readJson(response);
}
export class SSEParser {
  constructor(onEvent, onHeartbeat = () => {}) {
    this.buffer = "";
    this.onEvent = onEvent;
    this.onHeartbeat = onHeartbeat;
  }
  feed(text) {
    this.buffer += text;
    // Server contract always emits LF; accepting CRLF is useful through compliant proxies.
    this.buffer = this.buffer.replace(/\r\n/g, "\n");
    let at;
    while ((at = this.buffer.indexOf("\n\n")) !== -1) {
      const frame = this.buffer.slice(0, at);
      this.buffer = this.buffer.slice(at + 2);
      if (frame.length > 65536) throw new ApiError("event_limit");
      let id, type;
      const lines = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith(":")) {
          this.onHeartbeat();
          continue;
        }
        if (line.startsWith("id:")) {
          if (id !== undefined) throw new ApiError("duplicate_event_id");
          id = line.slice(3).trimStart();
        } else if (line.startsWith("event:")) {
          if (type !== undefined) throw new ApiError("duplicate_event_type");
          type = line.slice(6).trimStart();
        } else if (line.startsWith("data:"))
          lines.push(line.slice(5).trimStart());
        else if (line && !/^retry: [0-9]+$/.test(line))
          throw new ApiError("invalid_sse");
      }
      if (!lines.length) {
        if (id || type) throw new ApiError("incomplete_event");
        continue;
      }
      let value;
      try {
        value = JSON.parse(lines.join("\n"));
      } catch {
        throw new ApiError("invalid_event_json");
      }
      if (!id || !type || value.event_id !== id || value.event_type !== type)
        throw new ApiError("event_envelope_mismatch");
      this.onEvent(value);
    }
    if (this.buffer.length > 65536) throw new ApiError("event_limit");
  }
  finish() {
    if (this.buffer.trim()) throw new ApiError("truncated_event");
  }
}
export async function stream(
  plan,
  cursor,
  { signal, onEvent, onHeartbeat, follow = true, fetcher = fetch },
) {
  const headers = { Accept: "text/event-stream" };
  if (cursor) headers["Last-Event-ID"] = `${plan}:${cursor}`;
  const response = await fetcher(
    `/v1/plans/${plan}/events${follow ? "" : "?follow=false"}`,
    {
      headers,
      credentials: "same-origin",
      redirect: "error",
      cache: "no-store",
      signal,
    },
  );
  if (!response.ok) {
    await readJson(response);
    throw new ApiError("stream_rejected");
  }
  if (!response.headers.get("content-type")?.startsWith("text/event-stream"))
    throw new ApiError("invalid_stream");
  const parser = new SSEParser(onEvent, onHeartbeat),
    decoder = new TextDecoder("utf-8", { fatal: true }),
    reader = response.body.getReader();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      let decoded;
      try {
        decoded = decoder.decode(value, { stream: true });
      } catch {
        throw new ApiError("invalid_stream_encoding");
      }
      parser.feed(decoded);
    }
    let final;
    try {
      final = decoder.decode();
    } catch {
      throw new ApiError("invalid_stream_encoding");
    }
    parser.feed(final);
    parser.finish();
  } finally {
    await reader.cancel().catch(() => {});
  }
}
