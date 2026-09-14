(() => {
  const { useEffect, useMemo, useRef, useState } = React;
  const h = React.createElement;

  const textExts = new Set([
    "css", "csv", "html", "htm", "ini", "js", "json", "jsx", "log", "md",
    "py", "rs", "sh", "sql", "toml", "ts", "tsx", "txt", "xml", "yaml", "yml",
  ]);

  function fileName(path) {
    return path.split("/").pop() || path;
  }

  function ext(path) {
    const name = fileName(path);
    const idx = name.lastIndexOf(".");
    return idx >= 0 ? name.slice(idx + 1).toLowerCase() : "";
  }

  function fmtSize(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
    return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }

  async function json(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      let detail = await res.text();
      try {
        detail = JSON.parse(detail).detail || detail;
      } catch (_) {
        /* keep raw text */
      }
      throw new Error(`${res.status}: ${detail}`);
    }
    return res.json();
  }

  function downloadUrl(path) {
    return `/_dev/api/files/${path.split("/").map(encodeURIComponent).join("/")}`;
  }

  function schemaDefault(schema) {
    if (!schema || typeof schema !== "object") return "";
    if ("default" in schema) return schema.default;
    if ("const" in schema) return schema.const;
    if (Array.isArray(schema.enum) && schema.enum.length) return schema.enum[0];
    const typ = Array.isArray(schema.type)
      ? schema.type.find((x) => x !== "null") || schema.type[0]
      : schema.type;
    if (typ === "boolean") return false;
    if (typ === "integer" || typ === "number") return "";
    if (typ === "array") return [];
    if (typ === "object") return {};
    return "";
  }

  function argsFromSkill(skill) {
    const props = skill?.input_schema?.properties || {};
    const out = {};
    for (const [key, schema] of Object.entries(props)) out[key] = schemaDefault(schema);
    return out;
  }

  function chatField(skill) {
    const props = skill?.input_schema?.properties || {};
    for (const key of ["prompt", "message", "query", "text", "input"]) {
      if (props[key]?.type === "string") return key;
    }
    const entries = Object.entries(props).filter(([, schema]) => schema?.type === "string");
    return entries.length === 1 ? entries[0][0] : null;
  }

  function parseCsv(text, delimiter) {
    const rows = [];
    let row = [];
    let cell = "";
    let quoted = false;
    for (let i = 0; i < text.length; i += 1) {
      const ch = text[i];
      const next = text[i + 1];
      if (quoted) {
        if (ch === '"' && next === '"') {
          cell += '"';
          i += 1;
        } else if (ch === '"') {
          quoted = false;
        } else {
          cell += ch;
        }
      } else if (ch === '"') {
        quoted = true;
      } else if (ch === delimiter) {
        row.push(cell);
        cell = "";
      } else if (ch === "\n") {
        row.push(cell);
        rows.push(row);
        row = [];
        cell = "";
      } else if (ch !== "\r") {
        cell += ch;
      }
    }
    row.push(cell);
    rows.push(row);
    return rows.filter((r) => r.some((c) => c.length));
  }

  function sseEvents(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let sawDone = false;
    return reader.read().then(function pump({ done, value }) {
      if (done) {
        if (!sawDone) throw new Error("stream ended before [DONE]");
        return;
      }
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";
      for (const chunk of chunks) {
        const line = chunk.split("\n").find((part) => part.startsWith("data: "));
        if (!line) continue;
        const raw = line.slice(6);
        if (raw === "[DONE]") {
          sawDone = true;
          return;
        }
        try {
          onEvent(JSON.parse(raw));
        } catch (err) {
          onEvent({ type: "error", detail: String(err) });
        }
      }
      return reader.read().then(pump);
    });
  }

  function setupValuesFrom(payload) {
    const values = {};
    for (const field of payload?.fields || []) {
      if (field.kind === "secret") {
        values[field.name] = "";
      } else {
        values[field.name] = field.value ?? "";
      }
    }
    return values;
  }

  function groupSetupFields(fields) {
    const groups = [];
    for (const field of fields || []) {
      let group = groups.find((item) => item.name === field.group);
      if (!group) {
        group = { name: field.group || "Setup", fields: [] };
        groups.push(group);
      }
      group.fields.push(field);
    }
    return groups;
  }

  function App() {
    const [card, setCard] = useState(null);
    const [files, setFiles] = useState([]);
    const [selectedSkill, setSelectedSkill] = useState("");
    const [args, setArgs] = useState({});
    const [messages, setMessages] = useState([]);
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);
    const [dragging, setDragging] = useState(false);
    const [preview, setPreview] = useState(null);
    const [uploadedPaths, setUploadedPaths] = useState([]);
    const [setup, setSetup] = useState(null);
    const [setupValues, setSetupValues] = useState({});
    const [setupBusy, setSetupBusy] = useState(false);
    const fileInput = useRef(null);

    async function refreshFiles() {
      setFiles(await json("/_dev/api/files"));
    }

    async function refreshSetup() {
      const next = await json("/_dev/api/setup");
      setSetup(next);
      setSetupValues(setupValuesFrom(next));
      return next;
    }

    useEffect(() => {
      json("/_dev/api/card")
        .then((next) => {
          setCard(next);
          const first = next.skills?.[0]?.name || "";
          const ask = next.skills?.find((skill) => skill.name === "ask")?.name;
          setSelectedSkill(ask || first);
        })
        .catch((ex) => setErr(ex.message || String(ex)));
      refreshFiles().catch((ex) => setErr(ex.message || String(ex)));
      refreshSetup().catch((ex) => setErr(ex.message || String(ex)));
    }, []);

    const skill = useMemo(
      () => card?.skills?.find((item) => item.name === selectedSkill),
      [card, selectedSkill],
    );
    const promptKey = chatField(skill);

    useEffect(() => {
      if (skill) setArgs(argsFromSkill(skill));
    }, [skill?.name]);

    async function upload(fileList) {
      const uploaded = [];
      for (const file of Array.from(fileList || [])) {
        const form = new FormData();
        form.append("file", file);
        form.append("prefix", "inputs");
        const meta = await json("/_dev/api/files", { method: "POST", body: form });
        uploaded.push(meta.path);
      }
      if (uploaded.length) {
        setUploadedPaths((cur) => [...cur, ...uploaded]);
        setMessages((cur) => [
          ...cur,
          {
            role: "progress",
            content: `uploaded\n${uploaded.map((path) => `- ${path}`).join("\n")}`,
          },
        ]);
      }
      await refreshFiles();
    }

    async function handleDrop(ev) {
      ev.preventDefault();
      setDragging(false);
      setErr(null);
      try {
        await upload(ev.dataTransfer.files);
      } catch (ex) {
        setErr(ex.message || String(ex));
      }
    }

    function updateArg(key, value) {
      setArgs((cur) => ({ ...cur, [key]: value }));
    }

    async function invoke() {
      if (!skill || busy) return;
      if (setup?.blocking) {
        setErr("Complete local setup before running skills.");
        return;
      }
      setBusy(true);
      setErr(null);
      const finalArgs = { ...args };
      if (promptKey && uploadedPaths.length) {
        const prompt = String(finalArgs[promptKey] || "");
        finalArgs[promptKey] = [
          "Uploaded files:",
          ...uploadedPaths.map((path) => `- ${path}`),
          "",
          "User request:",
          prompt,
        ].join("\n");
      }
      setMessages((cur) => [
        ...cur,
        { role: "user", content: summarizeArgs(finalArgs) },
      ]);
      try {
        const res = await fetch(`/invoke/${encodeURIComponent(skill.name)}`, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            accept: "text/event-stream",
          },
          body: JSON.stringify({ arguments: finalArgs }),
        });
        if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
        await sseEvents(res, (event) => {
          if (event.type === "event") {
            const message =
              event.payload?.message ||
              event.payload?.title ||
              JSON.stringify(event.payload || {});
            setMessages((cur) => [
              ...cur,
              { role: "progress", content: `[${event.kind}] ${message}` },
            ]);
          } else if (event.type === "result") {
            setMessages((cur) => [
              ...cur,
              { role: "assistant", content: stringifyResult(event.result) },
            ]);
          } else if (event.type === "error") {
            setMessages((cur) => [
              ...cur,
              {
                role: "error",
                content: event.detail || event.message || JSON.stringify(event),
              },
            ]);
          }
        });
        setUploadedPaths([]);
        await refreshFiles();
        await refreshSetup();
      } catch (ex) {
        const message = ex.message || String(ex);
        setErr(message);
        setMessages((cur) => [...cur, { role: "error", content: message }]);
      } finally {
        setBusy(false);
      }
    }

    async function saveSetup(ev) {
      ev.preventDefault();
      if (setupBusy) return;
      setSetupBusy(true);
      setErr(null);
      try {
        const next = await json("/_dev/api/setup", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ values: setupValues }),
        });
        setSetup(next);
        setSetupValues(setupValuesFrom(next));
      } catch (ex) {
        setErr(ex.message || String(ex));
      } finally {
        setSetupBusy(false);
      }
    }

    return h(
      "div",
      {
        className: "app",
        onDragOver: (ev) => {
          ev.preventDefault();
          setDragging(true);
        },
        onDragLeave: () => setDragging(false),
        onDrop: handleDrop,
      },
      h(
        "header",
        { className: "topbar" },
        h(
          "div",
          { className: "brand" },
          h("div", { className: "mark" }, "a2a"),
          h(
            "div",
            null,
            h("h1", null, card ? card.name : "local agent"),
            h("p", null, card ? `${card.version} · ${card.description || "dev mode"}` : "loading"),
          ),
        ),
        h("div", { className: "status" }, h("span", { className: "dot" }), "local dev"),
      ),
      h(
        "div",
        { className: "layout" },
        h(
          "aside",
          { className: "sidebar" },
          h(
            "div",
            { className: "panel" },
            h("h2", null, "Files"),
            h(
              "div",
              { className: `dropzone${dragging ? " dragging" : ""}` },
              h("strong", null, "Drop files here"),
              h("span", null, "Uploads go to inputs/"),
              h("div", { style: { marginTop: "0.75rem" } },
                h("button", { onClick: () => fileInput.current?.click() }, "Upload"),
                h("input", {
                  ref: fileInput,
                  type: "file",
                  multiple: true,
                  style: { display: "none" },
                  onChange: (ev) => upload(ev.target.files).catch((ex) => setErr(ex.message || String(ex))),
                }),
              ),
            ),
          ),
          h(
            "div",
            { className: "file-tree" },
            files.length
              ? files.map((file) =>
                  h(
                    "button",
                    {
                      key: file.path,
                      className: `file-row${preview?.path === file.path ? " active" : ""}`,
                      onClick: () => setPreview(file),
                    },
                    h("span", { className: "file-name" }, file.path),
                    h("span", { className: "file-size" }, fmtSize(file.size_bytes)),
                  ),
                )
              : h("div", { className: "empty" }, "No files yet."),
          ),
        ),
        h(
          "main",
          { className: "main" },
          h(
            "div",
            { className: "controls" },
            h(
              "div",
              { className: "field" },
              h("label", null, "Skill"),
              h(
                "select",
                {
                  value: selectedSkill,
                  onChange: (ev) => setSelectedSkill(ev.target.value),
                },
                (card?.skills || []).map((item) =>
                  h("option", { key: item.name, value: item.name }, item.name),
                ),
              ),
            ),
            h("div", { className: "muted" }, skill?.description || "Select a skill."),
            h("span", { className: "pill" }, `${files.length} files`),
          ),
          h(
            "div",
            { className: "workspace" },
            h(
              "section",
              { className: "chat" },
              h(
                "div",
                { className: "messages" },
                err ? h("div", { className: "message error" }, err) : null,
                messages.length
                  ? messages.map((msg, idx) =>
                      h("div", { key: idx, className: `message ${msg.role}` }, msg.content),
                    )
                  : h("div", { className: "empty" }, "Run a skill to see events and results."),
              ),
              h(
                "div",
                { className: "composer" },
                h(SchemaForm, {
                  skill,
                  args,
                  promptKey,
                  onChange: updateArg,
                }),
                h(
                  "div",
                  { className: "actions" },
                  h("button", { onClick: () => setMessages([]), disabled: busy }, "Clear"),
                  h("button", { className: "primary", onClick: invoke, disabled: busy || !skill || setup?.blocking }, busy ? "Running" : "Run"),
                ),
              ),
            ),
            h(Preview, { file: preview }),
          ),
        ),
      ),
      setup?.blocking
        ? h(SetupGate, {
            setup,
            values: setupValues,
            busy: setupBusy,
            onChange: (name, value) =>
              setSetupValues((cur) => ({ ...cur, [name]: value })),
            onSubmit: saveSetup,
          })
        : null,
    );
  }

  function SetupGate({ setup, values, busy, onChange, onSubmit }) {
    const fields = (setup.fields || []).filter((field) => {
      if (!field.configured) return true;
      return field.kind !== "secret";
    });
    return h(
      "div",
      { className: "setup-backdrop" },
      h(
        "form",
        { className: "setup-card", onSubmit },
        h("div", { className: "setup-kicker" }, "Local setup"),
        h("h2", null, "Credentials required"),
        h(
          "p",
          null,
          "Saved to ~/.a2a/credentials.json and loaded into this local dev server.",
        ),
        groupSetupFields(fields).map((group) =>
          h(
            "section",
            { key: group.name, className: "setup-group" },
            h("h3", null, group.name),
            group.fields.map((field) =>
              h(SetupField, {
                key: field.name,
                field,
                value: values[field.name] ?? "",
                onChange: (value) => onChange(field.name, value),
              }),
            ),
          ),
        ),
        h(
          "div",
          { className: "setup-actions" },
          h(
            "button",
            { className: "primary", type: "submit", disabled: busy },
            busy ? "Saving" : "Save credentials",
          ),
        ),
      ),
    );
  }

  function SetupField({ field, value, onChange }) {
    const label = h(
      "label",
      null,
      field.label || field.name,
      field.required && !field.configured ? h("span", null, "required") : null,
    );
    const common = {
      id: `setup-${field.name}`,
      value,
      placeholder: field.name,
      onChange: (ev) => onChange(ev.target.value),
      autoComplete: "off",
    };
    let control;
    if (Array.isArray(field.options) && field.options.length) {
      control = h(
        "select",
        common,
        field.options.map((option) =>
          h("option", { key: String(option), value: option }, String(option)),
        ),
      );
    } else if (field.input_type === "textarea") {
      control = h("textarea", common);
    } else {
      const type = field.input_type === "password"
        ? "password"
        : field.input_type === "url"
          ? "url"
          : field.input_type === "email"
            ? "email"
            : field.input_type === "number"
              ? "number"
              : "text";
      control = h("input", { ...common, type });
    }
    return h(
      "div",
      { className: "setup-field" },
      label,
      control,
      field.description ? h("p", null, field.description) : null,
    );
  }

  function SchemaForm({ skill, args, promptKey, onChange }) {
    const props = skill?.input_schema?.properties || {};
    const entries = Object.entries(props);
    if (!entries.length) return h("div", { className: "empty" }, "This skill has no inputs.");
    return h(
      "div",
      { className: "schema-grid" },
      entries.map(([key, schema]) =>
        h(SchemaField, {
          key,
          name: key,
          schema,
          value: args[key],
          wide: key === promptKey || schema?.type === "object" || schema?.type === "array",
          onChange: (value) => onChange(key, value),
        }),
      ),
    );
  }

  function SchemaField({ name, schema, value, wide, onChange }) {
    const typ = Array.isArray(schema?.type)
      ? schema.type.find((x) => x !== "null") || schema.type[0]
      : schema?.type;
    const label = h("label", null, name);
    if (Array.isArray(schema?.enum)) {
      return h("div", { className: `field${wide ? " wide" : ""}` }, label,
        h("select", { value: value ?? "", onChange: (ev) => onChange(ev.target.value) },
          schema.enum.map((item) => h("option", { key: String(item), value: item }, String(item))),
        ),
      );
    }
    if (typ === "boolean") {
      return h("div", { className: `field${wide ? " wide" : ""}` }, label,
        h("select", {
          value: value ? "true" : "false",
          onChange: (ev) => onChange(ev.target.value === "true"),
        }, h("option", { value: "false" }, "false"), h("option", { value: "true" }, "true")),
      );
    }
    if (typ === "integer" || typ === "number") {
      return h("div", { className: `field${wide ? " wide" : ""}` }, label,
        h("input", {
          type: "number",
          value: value ?? "",
          onChange: (ev) => {
            const raw = ev.target.value;
            onChange(raw === "" ? "" : typ === "integer" ? parseInt(raw, 10) : Number(raw));
          },
        }),
      );
    }
    if (typ === "object" || typ === "array") {
      return h("div", { className: `field${wide ? " wide" : ""}` }, label,
        h("textarea", {
          value: typeof value === "string" ? value : JSON.stringify(value ?? (typ === "array" ? [] : {}), null, 2),
          onChange: (ev) => {
            try {
              onChange(JSON.parse(ev.target.value));
            } catch (_) {
              onChange(ev.target.value);
            }
          },
        }),
      );
    }
    return h("div", { className: `field${wide ? " wide" : ""}` }, label,
      h("textarea", {
        value: value ?? "",
        onChange: (ev) => onChange(ev.target.value),
      }),
    );
  }

  function Preview({ file }) {
    const [blobUrl, setBlobUrl] = useState(null);
    const [text, setText] = useState(null);
    const [err, setErr] = useState(null);

    useEffect(() => {
      setBlobUrl(null);
      setText(null);
      setErr(null);
      if (!file) return undefined;
      let alive = true;
      let url = null;
      fetch(downloadUrl(file.path))
        .then((res) => {
          if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`);
          return res.blob();
        })
        .then(async (blob) => {
          if (!alive) return;
          url = URL.createObjectURL(blob);
          setBlobUrl(url);
          const kind = previewKind(file, blob);
          if (["text", "csv", "tsv", "json"].includes(kind) && blob.size <= 1024 * 1024) {
            setText(await blob.text());
          }
        })
        .catch((ex) => alive && setErr(ex.message || String(ex)));
      return () => {
        alive = false;
        if (url) URL.revokeObjectURL(url);
      };
    }, [file?.path]);

    if (!file) {
      return h("section", { className: "preview" },
        h("div", { className: "preview-head" }, h("strong", null, "Preview")),
        h("div", { className: "preview-body empty" }, "Select a file."),
      );
    }
    const kind = previewKind(file);
    return h("section", { className: "preview" },
      h("div", { className: "preview-head" },
        h("strong", null, file.path),
        h("a", { href: downloadUrl(file.path), download: fileName(file.path) }, h("button", null, "Download")),
      ),
      h("div", { className: "preview-body" },
        err ? h("div", { className: "message error" }, err) :
        !blobUrl ? h("div", { className: "empty" }, "Loading...") :
        renderPreview(kind, blobUrl, text),
      ),
    );
  }

  function previewKind(file, blob) {
    const type = (blob?.type || file.content_type || "").toLowerCase();
    const suffix = ext(file.path);
    if (type.startsWith("image/")) return "image";
    if (type.startsWith("video/")) return "video";
    if (type.startsWith("audio/")) return "audio";
    if (type === "application/pdf" || suffix === "pdf") return "pdf";
    if (suffix === "csv" || type.includes("csv")) return "csv";
    if (suffix === "tsv") return "tsv";
    if (suffix === "json" || type.includes("json")) return "json";
    if (type.startsWith("text/") || textExts.has(suffix)) return "text";
    return "binary";
  }

  function renderPreview(kind, url, text) {
    if (kind === "image") return h("img", { src: url, alt: "" });
    if (kind === "video") return h("video", { src: url, controls: true });
    if (kind === "audio") return h("audio", { src: url, controls: true });
    if (kind === "pdf") return h("iframe", { src: url, title: "PDF preview" });
    if (kind === "csv" || kind === "tsv") return h(CsvTable, { text: text || "", delimiter: kind === "csv" ? "," : "\t" });
    if (kind === "json") {
      try {
        return h("pre", null, JSON.stringify(JSON.parse(text || ""), null, 2));
      } catch (_) {
        return h("pre", null, text || "");
      }
    }
    if (kind === "text") return h("pre", null, text || "");
    return h("div", { className: "empty" }, "No inline preview for this file type.");
  }

  function CsvTable({ text, delimiter }) {
    const rows = parseCsv(text, delimiter).slice(0, 200);
    if (!rows.length) return h("div", { className: "empty" }, "Empty table.");
    const [head, ...body] = rows;
    return h("table", null,
      h("thead", null, h("tr", null, head.map((cell, idx) => h("th", { key: idx }, cell)))),
      h("tbody", null, body.map((row, ridx) =>
        h("tr", { key: ridx }, head.map((_, cidx) => h("td", { key: cidx }, row[cidx] || ""))),
      )),
    );
  }

  function summarizeArgs(args) {
    if (Object.keys(args).length === 1) return String(Object.values(args)[0] ?? "");
    return JSON.stringify(args, null, 2);
  }

  function stringifyResult(result) {
    return typeof result === "string" ? result : JSON.stringify(result, null, 2);
  }

  ReactDOM.createRoot(document.getElementById("root")).render(h(App));
})();
