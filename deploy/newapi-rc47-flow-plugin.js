export const meta = {
  apiVersion: 1,
  key: "flow2api",
  name: "Google Flow (Veo 3.1 & Omni 1.2 Flash)",
  icon: "Gemini.Color",
  description: {
    en: "Google Flow video generation pool (Omni 1.2 Flash 3-15s, Veo 3.1 Lite/Fast/Quality)",
    zh: "Google Flow 视频号池（Omni 1.2 Flash 3-15秒、Veo 3.1 Lite/Fast/Quality 文生/图生视频）",
  },
  version: "1.0.0",
  author: { name: "MOMO" },
  channelTypes: [1, 24],
  models: [
    "omni-1.2-flash",
    "omni-1.2-flash-4s-landscape",
    "omni-1.2-flash-4s-portrait",
    "omni-1.2-flash-6s-landscape",
    "omni-1.2-flash-6s-portrait",
    "omni-1.2-flash-8s-landscape",
    "omni-1.2-flash-8s-portrait",
    "omni-1.2-flash-10s-landscape",
    "omni-1.2-flash-10s-portrait",
    "omni-1.2-flash-12s-portrait",
    "omni-1.2-flash-15s-portrait",
    "omni-1.1-flash",
    "veo-3.1-lite",
    "veo-3.1-fast",
    "veo-3.1-quality",
  ],
  fetchMode: "per_task",
  upstreams: ["vendor", "new_api"],
  usageSchema: {
    seconds: {
      type: "number",
      unit: "second",
      description: { en: "Video duration in seconds", zh: "视频时长（秒）" },
    },
    resolution: {
      enum: ["720p", "1080p", "4k"],
      enumLabels: {
        "720p": { en: "720p", zh: "720p" },
        "1080p": { en: "1080p", zh: "1080p" },
        "4k": { en: "4k", zh: "4k" },
      },
      description: { en: "Output video resolution", zh: "输出视频分辨率" },
    },
  },
  usageExamples: [
    { label: "8s × 720p", facts: { seconds: 8, resolution: "720p" } },
    { label: "15s × 720p", facts: { seconds: 15, resolution: "720p" } },
    { label: "8s × 1080p", facts: { seconds: 8, resolution: "1080p" } },
    { label: "8s × 4k", facts: { seconds: 8, resolution: "4k" } },
  ],
  protocols: [
    { name: "openai_responses", supports: ["stream", "sync", "background"] },
    "openai_video",
  ],
};

function trimmed(value) {
  return String(value || "").trim();
}

function responsesInput(req) {
  const texts = [],
    images = [];
  const input = req.input;
  if (typeof input === "string") texts.push(input);
  else if (Array.isArray(input)) {
    for (const item of input) {
      if (typeof item === "string") {
        texts.push(item);
        continue;
      }
      if (!item || typeof item !== "object" || Array.isArray(item)) continue;
      const content =
        item.content === undefined
          ? [item]
          : Array.isArray(item.content)
            ? item.content
            : [item.content];
      for (const part of content) {
        if (typeof part === "string") {
          texts.push(part);
          continue;
        }
        if (!part || typeof part !== "object" || Array.isArray(part)) continue;
        if (["input_text", "text"].includes(part.type) && typeof part.text === "string")
          texts.push(part.text);
        if (["input_image", "image_url"].includes(part.type)) {
          let image = part.image_url;
          if (image && typeof image === "object") image = image.url;
          if (trimmed(image)) images.push(trimmed(image));
        }
      }
    }
  }
  return {
    prompt: texts.filter((t) => trimmed(t)).join("\n"),
    images: images,
  };
}

function responsesVideoText(ctx) {
  const artifact = ctx && ctx.artifacts && ctx.artifacts.video;
  const url = trimmed(artifact && artifact.url);
  if (!url) throw new Error("video artifact is unavailable");
  const escaped = url
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return '<video controls src="' + escaped + '"></video>';
}

export function buildSubmitRequest(ctx) {
  const req = Object.assign({}, ctx.requestBody || {});
  if (!trimmed(req.prompt)) throw new Error("field prompt is required");
  req.model = ctx.upstreamModel;
  const headers = { Authorization: "Bearer " + ctx.apiKey };
  if ((ctx.files || []).length) {
    const parts = [];
    for (const key of Object.keys(req)) {
      if (req[key] !== undefined && req[key] !== null && typeof req[key] !== "object") {
        parts.push({ name: key, value: req[key] });
      }
    }
    for (const file of ctx.files) {
      parts.push({ name: file.field, fileRef: file.ref, filename: file.filename });
    }
    return {
      url: ctx.baseUrl + "/v1/videos",
      method: "POST",
      headers,
      bodyType: "multipart",
      parts,
    };
  }
  headers["Content-Type"] = "application/json";
  return {
    url: ctx.baseUrl + "/v1/videos",
    method: "POST",
    headers,
    body: req,
    action: req.images || req.input_reference || req.image ? "image_to_video" : "text_to_video",
  };
}

export function parseSubmitResponse(ctx, resp) {
  const body = resp.body || {};
  const taskId = body.id || body.task_id;
  if (!taskId) throw new Error("task_id is empty");
  return { taskId, taskData: body };
}

export function extractUsage(ctx) {
  const req = ctx.requestBody || {};
  const metadata = req.metadata || {};
  let seconds = Number(req.seconds || req.duration || 8);
  if (!Number.isFinite(seconds) || seconds <= 0) seconds = 8;
  const resolution = String(req.resolution || metadata.resolution || "720p").toLowerCase();
  return {
    seconds: Math.min(seconds, 15),
    resolution: ["720p", "1080p", "4k"].includes(resolution) ? resolution : "720p",
  };
}

export function extractUsageOnComplete(task, taskResult, body) {
  const facts = {};
  const seconds = Number((body || {}).seconds || (body || {}).duration || 0);
  if (Number.isFinite(seconds) && seconds > 0) facts.seconds = Math.min(seconds, 15);
  return facts;
}

export function buildQueryRequest(ctx) {
  return {
    url: ctx.baseUrl + "/v1/videos/" + encodeURIComponent(ctx.taskId),
    method: "GET",
    headers: { Authorization: "Bearer " + ctx.apiKey },
  };
}

export function parseTaskResult(ctx, body) {
  const statuses = {
    queued: "QUEUED",
    pending: "QUEUED",
    processing: "IN_PROGRESS",
    in_progress: "IN_PROGRESS",
    completed: "SUCCESS",
    failed: "FAILURE",
    cancelled: "FAILURE",
  };
  const mapped = statuses[body.status];
  const result = { status: mapped || "UNKNOWN" };
  if (!mapped) result.reason = "unrecognized status: " + String(body.status || "");
  if (body.progress > 0 && body.progress < 100) result.progress = body.progress + "%";
  if (result.status === "SUCCESS" && (body.video_url || body.url)) {
    result.remoteUrl = body.video_url || body.url;
  }
  if (result.status === "FAILURE") {
    result.reason = body.error && body.error.message ? body.error.message : "task failed";
  }
  return result;
}

export function listArtifacts(task) {
  return task.status === "SUCCESS" ? [{ key: "video", type: "video" }] : [];
}

export function buildContentRequest(ctx) {
  if (ctx.artifactKey !== "video") throw new Error("artifact_not_found");
  return {
    url: ctx.baseUrl + "/v1/videos/" + encodeURIComponent(ctx.upstreamTaskId) + "/content",
    method: ctx.clientRequest.method,
    headers: { Authorization: "Bearer " + ctx.apiKey },
  };
}

export const protocols = {
  openai_responses: {
    decodeRequest: function (ctx) {
      if (!ctx.body || ctx.body.kind !== "json") throw new Error("JSON body required");
      const req = ctx.body.value;
      if (!req || typeof req !== "object" || Array.isArray(req))
        throw new Error("request body must be an object");
      const model = trimmed(req.model);
      if (!model) throw new Error("model is required");
      const input = responsesInput(req);
      const prompt = input.prompt || trimmed(req.prompt);
      const images = [];
      for (const image of [req.image, req.input_reference].concat(req.images || [], input.images)) {
        if (trimmed(image) && !images.includes(trimmed(image))) images.push(trimmed(image));
      }
      if (!prompt && images.length === 0) throw new Error("input is required");
      const requestBody = { model: model, prompt: prompt };
      if (images.length) requestBody.images = images;
      if (req.seconds !== undefined) requestBody.seconds = req.seconds;
      else if (req.duration !== undefined) requestBody.duration = req.duration;
      if (req.size !== undefined) requestBody.size = req.size;
      if (req.resolution !== undefined) requestBody.image_size = req.resolution;
      return {
        kind: "submit",
        model: model,
        action: images.length ? "image_to_video" : "text_to_video",
        requestBody: requestBody,
      };
    },
    renderEvents: function (ctx, task, previousState) {
      const status = String(task.status || "UNKNOWN").toUpperCase();
      const value = Number(String(task.progress || "").replace("%", ""));
      const progress = Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
      const state = { status: status, progress: progress };
      if (status === "SUCCESS") {
        const text = responsesVideoText(ctx);
        const events =
          previousState && previousState.status === status ? [] : [{ type: "output", data: text }];
        return { events: events, state: state, done: true };
      }
      if (status === "FAILURE")
        return {
          events: [{ type: "error", code: "task_failed", message: task.fail_reason || "task failed" }],
          state: state,
          done: true,
        };
      if (previousState && previousState.status === status && previousState.progress === progress)
        return { events: [], state: state, done: false };
      const event = { type: "progress", message: status.toLowerCase() };
      if (progress !== null) event.progress = progress;
      return { events: [event], state: state, done: false };
    },
    renderFinal: function (ctx, _task) {
      return {
        output: [
          {
            type: "message",
            status: "completed",
            role: "assistant",
            content: [{ type: "output_text", text: responsesVideoText(ctx), annotations: [], logprobs: [] }],
          },
        ],
        metadata: { vendor: "google-flow" },
      };
    },
  },
  openai_video: {
    decodeRequest: function (ctx) {
      if (!ctx.body || (ctx.body.kind !== "json" && ctx.body.kind !== "multipart"))
        throw new Error("JSON or multipart body required");
      const req = ctx.body.kind === "json" ? Object.assign({}, ctx.body.value || {}) : {};
      if (ctx.body.kind === "multipart") {
        const fields = ctx.body.fields || {};
        for (const name of Object.keys(fields)) {
          req[name] = (fields[name] || [])[0];
        }
      }
      return {
        kind: "submit",
        model: ctx.model,
        action: req.input_reference || req.image ? "image_to_video" : "text_to_video",
        requestBody: Object.assign({}, req, { model: ctx.model }),
      };
    },
    render: function (ctx, task) {
      if (task.data && typeof task.data === "object" && !Array.isArray(task.data)) return task.data;
      const statuses = {
        NOT_START: "queued",
        SUBMITTED: "queued",
        QUEUED: "queued",
        IN_PROGRESS: "in_progress",
        SUCCESS: "completed",
        FAILURE: "failed",
      };
      return {
        id: task.task_id,
        object: "video",
        model: (task.properties || {}).origin_model_name || "omni-1.2-flash",
        status: statuses[task.status] || "unknown",
        progress: Number(String(task.progress || "0").replace("%", "")),
        created_at: Number(task.created_at || 0),
      };
    },
  },
};
