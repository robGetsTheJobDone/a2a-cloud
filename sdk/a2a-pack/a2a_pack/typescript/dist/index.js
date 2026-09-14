import { mkdir, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { dirname } from "node:path";
import { createHash, createPrivateKey, createPublicKey, randomBytes, sign as cryptoSign, verify as cryptoVerify } from "node:crypto";
export function uploadedFileSchema(options = {}) {
    const upload = { required_upload: true };
    const accept = options.accept?.map((item) => String(item).trim()).filter(Boolean) ?? [];
    if (accept.length > 0)
        upload.accept = accept;
    const maxBytes = options.maxBytes ?? options.max_bytes;
    if (typeof maxBytes === "number")
        upload.max_bytes = maxBytes;
    if (options.multiple === true)
        upload.multiple = true;
    if (options.description)
        upload.description = options.description;
    return {
        title: "UploadedFile",
        type: "object",
        properties: {
            path: { type: "string" },
            filename: { type: "string" },
            media_type: { type: "string" },
            size_bytes: { type: "integer", minimum: 0 }
        },
        required: ["path", "filename", "media_type", "size_bytes"],
        additionalProperties: false,
        "x-a2a-file-upload": upload
    };
}
export const fileUploadSchema = uploadedFileSchema;
export class A2AAgent {
    static agent;
    static auth = publicAuth();
    static skills = [];
    static capabilities = {};
    static inputModes = ["application/json"];
    static outputModes = ["application/json"];
    static requiredSecrets = [];
    static requiredEnv = [];
    static consumerSetup = { fields: [] };
    static runtime = {};
    static workspaceAccess = { enabled: false };
    static configSchema = null;
    static stateSchema = null;
    static templateLineage = null;
    static metaAgentManifest = null;
    config;
    constructor(config = {}) {
        const schema = this.constructor.configSchema;
        const normalized = schema ? applyJsonSchemaDefaults(schema, config) : { ...config };
        if (schema)
            validateJsonSchemaValue(schema, normalized, "config");
        this.config = normalized;
    }
    async startup(_ctx) {
        // Hook for warm/session agents.
    }
    async shutdown(_ctx) {
        // Hook for warm/session agents.
    }
    async health() {
        return true;
    }
    async dispatch(request) {
        const handler = this[request.handler];
        if (typeof handler !== "function") {
            throw new SkillNotFound(request.handler);
        }
        const skillSpec = skillForHandler(this.constructor, request.handler);
        if (skillSpec) {
            try {
                validateJsonSchemaValue(skillSpec.input_schema, request.arguments, "input");
            }
            catch (error) {
                throw new SkillInputError(error instanceof Error ? error.message : String(error));
            }
            request.skill = skillSpec.name;
        }
        const context = new RunContext(request);
        if (skillSpec?.scopes?.length)
            context.requireScopes(skillSpec.scopes);
        const legacyRequest = { ...request, context };
        const startedAt = Math.floor(Date.now() / 1000);
        const recorder = skillSpec ? new EventRecorder({
            agent_name: this.constructor.agent.name,
            agent_version: this.constructor.agent.version ?? "",
            skill_name: skillSpec.name,
            caller: callerPrincipal(request.caller ?? null),
            task_id: context.taskId,
            random_seed: String(context.randomSeed ?? ""),
            input_hash: hashInput(request.arguments),
            started_at: startedAt
        }) : null;
        recorder?.record("skill_start", { args_hash: hashInput(request.arguments), args: request.arguments });
        try {
            const raw = handler.length >= 2
                ? await handler.call(this, context, request.arguments)
                : await handler.call(this, legacyRequest);
            const response = normalizeWorkerResponse(raw);
            if (skillSpec) {
                try {
                    validateJsonSchemaValue(skillSpec.output_schema, response.result, "output");
                }
                catch (error) {
                    throw new SkillOutputError(error instanceof Error ? error.message : String(error));
                }
            }
            recorder?.record("skill_end", { status: "ok" });
            if (skillSpec && recorder) {
                await emitReceiptAndReplay(context, {
                    agentName: this.constructor.agent.name,
                    agentVersion: this.constructor.agent.version ?? "",
                    skillName: skillSpec.name,
                    startedAt,
                    inputs: request.arguments,
                    result: response.result,
                    status: "ok",
                    errorType: "",
                    recorder
                });
            }
            return context.finalizeResponse(response);
        }
        catch (error) {
            const errorType = error instanceof Error ? error.name : typeof error;
            recorder?.record("error", { type: errorType });
            if (skillSpec && recorder) {
                await emitReceiptAndReplay(context, {
                    agentName: this.constructor.agent.name,
                    agentVersion: this.constructor.agent.version ?? "",
                    skillName: skillSpec.name,
                    startedAt,
                    inputs: request.arguments,
                    result: undefined,
                    status: "error",
                    errorType,
                    recorder
                });
            }
            throw error;
        }
    }
    async localInvoke(skillName, args = {}, options = {}) {
        const spec = skillForName(this.constructor, skillName);
        if (!spec)
            throw new SkillNotFound(skillName);
        const response = await this.dispatch({
            agent: this.constructor.agent.name,
            skill: spec.name,
            handler: spec.handler ?? spec.name,
            arguments: args,
            task_id: options.task_id ?? options.taskId ?? "local-task",
            grant: options.grant ?? null,
            cp_url: options.cp_url ?? options.cpUrl ?? null,
            cp_jwt: options.cp_jwt ?? options.cpJwt ?? null,
            llm_creds: options.llm_creds ?? options.llmCreds ?? null,
            consumer_config: options.consumer_config ?? options.consumerConfig ?? null,
            consumer_secrets: options.consumer_secrets ?? options.consumerSecrets ?? null,
            auth: options.auth ?? {},
            caller: options.caller ?? null,
            grant_ids: options.grant_ids ?? options.grantIds ?? null,
            random_seed: options.random_seed ?? options.randomSeed ?? null,
            a2a_client: options.a2a_client ?? options.a2a,
            workspace_client: options.workspace_client ?? options.workspace,
            sandbox_client: options.sandbox_client ?? options.sandbox,
            discovery_client: options.discovery_client ?? options.discover,
            secrets: options.secrets,
            emit: options.emit
        });
        return response.result;
    }
    async local_invoke(skillName, args = {}, options = {}) {
        return this.localInvoke(skillName, args, options);
    }
}
export class ConsumerSetupField {
    name;
    kind;
    label;
    description;
    required;
    input_type;
    options;
    constructor(options) {
        const name = options.name.trim();
        if (!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(name)) {
            throw new Error("consumer setup field names must use environment variable syntax");
        }
        const inputType = options.input_type ?? (options.kind === "secret" ? "password" : "text");
        if (!isConsumerSetupInputType(inputType)) {
            throw new Error(`invalid consumer setup input_type: ${inputType}`);
        }
        this.name = name;
        this.kind = options.kind ?? "config";
        this.label = options.label?.trim() || null;
        this.description = options.description ?? "";
        this.required = options.required ?? true;
        this.input_type = inputType;
        this.options = [...(options.options ?? [])];
    }
    static config(name, options = {}) {
        return new ConsumerSetupField({ name, kind: "config", ...options });
    }
    static secret(name, options = {}) {
        return new ConsumerSetupField({ name, kind: "secret", input_type: "password", ...options });
    }
    toJSON() {
        return {
            name: this.name,
            kind: this.kind,
            label: this.label,
            description: this.description,
            required: this.required,
            input_type: this.input_type,
            options: [...this.options]
        };
    }
}
export class ConsumerSetup {
    fields;
    constructor(fields = []) {
        const normalized = fields.map((field) => field instanceof ConsumerSetupField
            ? field
            : new ConsumerSetupField(field));
        const names = new Set();
        for (const field of normalized) {
            if (names.has(field.name))
                throw new Error(`duplicate consumer setup field: ${field.name}`);
            names.add(field.name);
        }
        this.fields = normalized;
    }
    static none() {
        return new ConsumerSetup();
    }
    static fromFields(...fields) {
        return new ConsumerSetup(fields);
    }
    static from_fields(...fields) {
        return ConsumerSetup.fromFields(...fields);
    }
    get requiredNames() {
        return this.fields.filter((field) => field.required).map((field) => field.name);
    }
    get required_names() {
        return this.requiredNames;
    }
    toJSON() {
        return { fields: this.fields.map((field) => field.toJSON()) };
    }
}
const pendingAnswers = new Map();
const pendingInputs = new Map();
const pendingScopeRequests = new Map();
export class WorkspaceDenied extends Error {
    constructor(message) {
        super(message);
        this.name = "WorkspaceDenied";
    }
}
export class ConsumerSetupMissing extends Error {
    constructor(message) {
        super(message);
        this.name = "ConsumerSetupMissing";
    }
}
export class CancelledByCaller extends Error {
    constructor(message = "operation cancelled") {
        super(message);
        this.name = "CancelledByCaller";
    }
}
export class SandboxUnavailable extends Error {
    constructor(message = "no sandbox runtime attached to this context") {
        super(message);
        this.name = "SandboxUnavailable";
    }
}
export class SandboxRuntimeError extends Error {
    statusCode;
    responseBody;
    operation;
    method;
    url;
    timeoutSeconds;
    causeType;
    causeMessage;
    constructor(message, options = {}) {
        super(message);
        this.name = "SandboxRuntimeError";
        this.statusCode = options.statusCode;
        this.responseBody = options.responseBody;
        this.operation = options.operation;
        this.method = options.method;
        this.url = options.url;
        this.timeoutSeconds = options.timeoutSeconds;
        this.causeType = options.causeType;
        this.causeMessage = options.causeMessage;
    }
    toErrorPayload() {
        return {
            type: this.name,
            message: this.message,
            status_code: this.statusCode,
            response_body: this.responseBody,
            operation: this.operation,
            method: this.method,
            url: this.url,
            timeout_seconds: this.timeoutSeconds,
            cause_type: this.causeType,
            cause_message: this.causeMessage
        };
    }
}
export class SkillNotFound extends Error {
    constructor(skill) {
        super(`unknown skill or handler: ${skill}`);
        this.name = "SkillNotFound";
    }
}
export class SkillInputError extends Error {
    constructor(message) {
        super(message);
        this.name = "SkillInputError";
    }
}
export class SkillOutputError extends Error {
    constructor(message) {
        super(message);
        this.name = "SkillOutputError";
    }
}
export class ReceiptInvalid extends Error {
    constructor(message) {
        super(message);
        this.name = "ReceiptInvalid";
    }
}
export class ReplayInvalid extends Error {
    constructor(message) {
        super(message);
        this.name = "ReplayInvalid";
    }
}
export class GrantInvalid extends Error {
    constructor(message) {
        super(message);
        this.name = "GrantInvalid";
    }
}
export class GrantDelegationDenied extends Error {
    constructor(message) {
        super(message);
        this.name = "GrantDelegationDenied";
    }
}
export const EVENT_KINDS = [
    "skill_start",
    "llm_call",
    "llm_response",
    "tool_call",
    "tool_response",
    "workspace_read",
    "workspace_write",
    "scope_request",
    "scope_approve",
    "handoff_start",
    "handoff_end",
    "artifact_write",
    "eval",
    "error",
    "skill_end"
];
export class EventRecorder {
    agent_name;
    agent_version;
    skill_name;
    caller;
    task_id;
    random_seed;
    input_hash;
    started_at;
    items = [];
    constructor(options) {
        this.agent_name = options.agent_name;
        this.agent_version = options.agent_version ?? "";
        this.skill_name = options.skill_name;
        this.caller = options.caller ?? "";
        this.task_id = options.task_id ?? "";
        this.random_seed = options.random_seed ?? "";
        this.input_hash = options.input_hash ?? "";
        this.started_at = options.started_at ?? Math.floor(Date.now() / 1000);
    }
    record(kind, payload = {}) {
        if (!EVENT_KINDS.includes(kind)) {
            throw new Error(`unknown event kind: ${kind}`);
        }
        const event = {
            idx: this.items.length,
            kind,
            ts_ms: Date.now(),
            payload: { ...payload }
        };
        this.items.push(event);
        return event;
    }
    get events() {
        return this.items.map((item) => ({ ...item, payload: { ...item.payload } }));
    }
    buildSession(options = {}) {
        return {
            session_id: options.session_id ?? randomBytes(8).toString("hex"),
            schema_version: 1,
            agent_name: this.agent_name,
            agent_version: this.agent_version,
            caller: this.caller,
            task_id: this.task_id,
            skill_name: this.skill_name,
            random_seed: this.random_seed,
            input_hash: this.input_hash,
            started_at: this.started_at,
            ended_at: Math.floor(Date.now() / 1000),
            events: this.events,
            receipt_id: options.receipt_id ?? "",
            nonce: randomBytes(8).toString("hex")
        };
    }
    build_session(options = {}) {
        return this.buildSession(options);
    }
}
export function hashInput(payload) {
    return createHash("sha256").update(stableJson(payload)).digest("hex");
}
export const hash_input = hashInput;
export function sealReceipt(options) {
    const ended = options.ended_at ?? Math.floor(Date.now() / 1000);
    const receipt = {
        receipt_id: randomBytes(8).toString("hex"),
        schema_version: 1,
        agent_name: options.agent_name,
        agent_version: options.agent_version ?? "",
        caller: options.caller ?? "",
        task_id: options.task_id ?? "",
        skill_name: options.skill_name,
        input_hash: options.inputs === undefined ? "" : hashInput(options.inputs),
        input_preview: options.inputs === undefined ? "" : previewJson(options.inputs),
        grant_ids: [...(options.grant_ids ?? [])],
        file_ops: { reads: 0, writes: 0, bytes_read: 0, bytes_written: 0, read_paths_preview: [], write_paths_preview: [] },
        tool_calls: [],
        artifacts: [...(options.artifacts ?? [])],
        handoffs: [],
        status: options.status ?? "ok",
        error_type: options.error_type ?? "",
        result_preview: options.result === undefined ? "" : previewJson(options.result),
        eval_score: null,
        reviewer: "",
        started_at: options.started_at,
        ended_at: ended,
        elapsed_ms: Math.max(0, (ended - options.started_at) * 1000),
        nonce: randomBytes(8).toString("hex")
    };
    return [receipt, signReceipt(receipt)];
}
export const seal_receipt = sealReceipt;
export function signReceipt(receipt) {
    const key = process.env.A2A_RECEIPT_SIGNING_KEY?.trim();
    if (!key)
        throw new Error("A2A_RECEIPT_SIGNING_KEY is required");
    const payload = Buffer.from(JSON.stringify(receipt), "utf8");
    return `${payload.toString("base64url")}.${cryptoSign(null, payload, signingKeyFromEnv(key)).toString("base64url")}`;
}
export const sign_receipt = signReceipt;
export function verifyReceipt(token) {
    const key = (process.env.A2A_RECEIPT_VERIFYING_KEY || process.env.A2A_RECEIPT_SIGNING_KEY || "").trim();
    if (!key)
        throw new ReceiptInvalid("A2A_RECEIPT_VERIFYING_KEY is required");
    const [payloadPart, sigPart] = splitSignedToken(token, ReceiptInvalid);
    const payload = Buffer.from(payloadPart, "base64url");
    const sig = Buffer.from(sigPart, "base64url");
    if (!cryptoVerify(null, payload, verifyingKeyFromEnv(key), sig))
        throw new ReceiptInvalid("receipt signature mismatch");
    const parsed = JSON.parse(payload.toString("utf8"));
    if (!isObject(parsed))
        throw new ReceiptInvalid("receipt payload invalid");
    return parsed;
}
export const verify_receipt = verifyReceipt;
export function sealReplaySession(session) {
    return [session, signReplaySession(session)];
}
export const seal_replay_session = sealReplaySession;
export function signReplaySession(session) {
    const key = process.env.A2A_REPLAY_SIGNING_KEY?.trim();
    if (!key)
        throw new Error("A2A_REPLAY_SIGNING_KEY is required");
    const payload = Buffer.from(JSON.stringify(session), "utf8");
    return `${payload.toString("base64url")}.${cryptoSign(null, payload, signingKeyFromEnv(key)).toString("base64url")}`;
}
export const sign_replay_session = signReplaySession;
export function verifyReplaySession(token) {
    const key = (process.env.A2A_REPLAY_VERIFYING_KEY || process.env.A2A_REPLAY_SIGNING_KEY || "").trim();
    if (!key)
        throw new ReplayInvalid("A2A_REPLAY_VERIFYING_KEY is required");
    const [payloadPart, sigPart] = splitSignedToken(token, ReplayInvalid);
    const payload = Buffer.from(payloadPart, "base64url");
    const sig = Buffer.from(sigPart, "base64url");
    if (!cryptoVerify(null, payload, verifyingKeyFromEnv(key), sig))
        throw new ReplayInvalid("replay signature mismatch");
    const parsed = JSON.parse(payload.toString("utf8"));
    if (!isObject(parsed))
        throw new ReplayInvalid("replay payload invalid");
    return parsed;
}
export const verify_replay_session = verifyReplaySession;
export function iterEvents(session) {
    return [...session.events].sort((left, right) => left.idx - right.idx);
}
export const iter_events = iterEvents;
export function filterEvents(session, kind) {
    const kinds = Array.isArray(kind) ? new Set(kind) : new Set([kind]);
    return iterEvents(session).filter((event) => kinds.has(event.kind));
}
export const filter_events = filterEvents;
export function mintGrant(options) {
    const now = Math.floor(Date.now() / 1000);
    const grant = {
        grant_id: randomBytes(8).toString("hex"),
        issuer: options.issuer,
        audience: options.audience,
        bucket: options.bucket,
        mode: options.mode ?? "read_only",
        allow_patterns: options.allow_patterns ?? ["**"],
        deny_patterns: options.deny_patterns ?? [],
        outputs_prefix: options.outputs_prefix ?? null,
        write_prefixes: normalizeWritePrefixes(options.outputs_prefix ?? null, options.write_prefixes ?? []),
        llm_models: options.llm_models ?? [],
        llm_max_budget_usd: options.llm_max_budget_usd ?? null,
        llm_rpm_limit: options.llm_rpm_limit ?? null,
        llm_tpm_limit: options.llm_tpm_limit ?? null,
        source_grants: options.source_grants ?? [],
        parent_grant_id: options.parent_grant_id ?? null,
        delegation_depth: options.delegation_depth ?? 0,
        max_delegation_depth: options.max_delegation_depth ?? 40,
        expires_at: now + (options.ttl_seconds ?? 300),
        issued_at: now,
        nonce: randomBytes(8).toString("hex")
    };
    return [grant, signGrant(grant)];
}
export const mint_grant = mintGrant;
export function signGrant(grant) {
    const key = process.env.A2A_GRANT_SIGNING_KEY?.trim();
    if (!key)
        throw new Error("A2A_GRANT_SIGNING_KEY is required");
    const normalized = {
        ...grant,
        write_prefixes: normalizeWritePrefixes(grant.outputs_prefix ?? null, grant.write_prefixes ?? [])
    };
    const payload = Buffer.from(JSON.stringify(normalized), "utf8");
    return `${payload.toString("base64url")}.${cryptoSign(null, payload, signingKeyFromEnv(key)).toString("base64url")}`;
}
export const sign_grant = signGrant;
export function verifyGrant(token) {
    const key = (process.env.A2A_GRANT_VERIFYING_KEY || process.env.A2A_GRANT_SIGNING_KEY || "").trim();
    if (!key)
        throw new GrantInvalid("A2A_GRANT_VERIFYING_KEY is required");
    const [payloadPart, sigPart] = splitSignedToken(token, GrantInvalid);
    const payload = Buffer.from(payloadPart, "base64url");
    const sig = Buffer.from(sigPart, "base64url");
    if (!cryptoVerify(null, payload, verifyingKeyFromEnv(key), sig))
        throw new GrantInvalid("grant signature mismatch");
    const parsed = JSON.parse(payload.toString("utf8"));
    if (!isObject(parsed))
        throw new GrantInvalid("grant payload invalid");
    const grant = parsed;
    if (grant.expires_at && grant.expires_at < Math.floor(Date.now() / 1000)) {
        throw new GrantInvalid(`grant expired at ${grant.expires_at}`);
    }
    if ((!grant.write_prefixes || grant.write_prefixes.length === 0) && grant.outputs_prefix) {
        grant.write_prefixes = normalizeWritePrefixes(grant.outputs_prefix, []);
    }
    return grant;
}
export const verify_grant = verifyGrant;
export function delegateGrant(parent, options) {
    const bucket = options.bucket ?? parent.bucket;
    if (!bucket || bucket !== parent.bucket)
        throw new GrantDelegationDenied("child grant bucket must match parent grant");
    const parentDepth = parent.delegation_depth ?? 0;
    const maxDepth = parent.max_delegation_depth ?? 40;
    if (parentDepth >= maxDepth)
        throw new GrantDelegationDenied("maximum grant delegation depth reached");
    const mode = options.mode ?? "read_only";
    if (modeRank(mode) > modeRank(parent.mode ?? "read_only")) {
        throw new GrantDelegationDenied(`child grant mode ${mode} exceeds parent mode ${parent.mode ?? "read_only"}`);
    }
    const childAllow = options.allow_patterns ?? ["**"];
    for (const pattern of childAllow) {
        if (!patternCovered(pattern, parent.allow_patterns ?? [])) {
            throw new GrantDelegationDenied(`child read pattern ${pattern} is outside parent grant`);
        }
    }
    const childWrites = normalizeWritePrefixes(options.outputs_prefix ?? null, options.write_prefixes ?? []);
    assertWriteScopeWithinParent(parent, childWrites);
    const remainingTtl = parent.expires_at ? Math.max(0, parent.expires_at - Math.floor(Date.now() / 1000)) : options.ttl_seconds ?? 300;
    return mintGrant({
        issuer: options.issuer,
        audience: options.audience,
        bucket,
        mode,
        allow_patterns: childAllow,
        deny_patterns: mergePatterns(parent.deny_patterns ?? [], options.deny_patterns ?? []),
        outputs_prefix: options.outputs_prefix ?? null,
        write_prefixes: childWrites,
        parent_grant_id: parent.grant_id ?? null,
        delegation_depth: parentDepth + 1,
        max_delegation_depth: maxDepth,
        ttl_seconds: Math.min(options.ttl_seconds ?? 300, remainingTtl)
    });
}
export const delegate_grant = delegateGrant;
export class A2AClient {
}
export class InMemoryA2AClient extends A2AClient {
    agents;
    ctxFactory;
    constructor(options) {
        super();
        this.agents = options.agents;
        this.ctxFactory = options.ctxFactory ?? options.ctx_factory;
    }
    async call(target, skillName, options = {}) {
        const agent = this.agents[target];
        if (!agent)
            throw new Error(`no agent registered: ${target}`);
        const agentClass = agent.constructor;
        const spec = skillForName(agentClass, skillName);
        if (!spec)
            throw new SkillNotFound(skillName);
        const grant = options.grant ?? null;
        const emitted = [];
        const factoryRequest = this.ctxFactory?.(agent, grant) ?? {};
        const response = await agent.dispatch({
            agent: agentClass.agent.name,
            skill: spec.name,
            handler: spec.handler,
            arguments: options.args ?? {},
            task_id: `a2a-${target}`,
            grant,
            cp_jwt: options.cp_jwt ?? options.cpJwt ?? null,
            cp_url: options.cp_url ?? options.cpUrl ?? null,
            llm_creds: options.llm_creds ?? options.llmCreds ?? null,
            consumer_config: options.consumer_config ?? options.consumerConfig ?? null,
            consumer_secrets: options.consumer_secrets ?? options.consumerSecrets ?? null,
            composition: options.composition ?? null,
            auth: {},
            ...factoryRequest,
            emit: async (event) => {
                emitted.push(event);
                await factoryRequest.emit?.(event);
            }
        });
        return {
            result: response.result,
            events: response.events ?? emitted,
            artifacts: response.artifacts ?? [],
            grant_id: grantIdFromToken(grant)
        };
    }
}
export class HttpA2AClient extends A2AClient {
    defaultTimeout;
    default_timeout;
    discovery;
    resolvedUrls = new Map();
    constructor(options = {}) {
        super();
        this.defaultTimeout = options.defaultTimeout ?? options.default_timeout ?? 60;
        this.default_timeout = this.defaultTimeout;
        this.discovery = options.discovery ?? null;
    }
    async call(target, skillName, options = {}) {
        const timeoutSeconds = options.timeoutSeconds ?? options.timeout ?? this.defaultTimeout;
        const grant = options.grant ?? null;
        const cpJwt = options.cp_jwt ?? options.cpJwt ?? null;
        const baseUrl = await this.resolveTarget(target);
        const response = await fetch(`${baseUrl}/invoke/${encodeURIComponent(skillName)}`, {
            method: "POST",
            headers: {
                "content-type": "application/json",
                ...(cpJwt ? { authorization: `bearer ${cpJwt}` } : {})
            },
            body: JSON.stringify({
                arguments: options.args ?? {},
                ...(grant !== null ? { grant } : {}),
                ...(cpJwt ? { cp_jwt: cpJwt } : {}),
                ...(options.cp_url ?? options.cpUrl ? { cp_url: options.cp_url ?? options.cpUrl } : {}),
                ...(options.llm_creds ?? options.llmCreds ? { llm_creds: options.llm_creds ?? options.llmCreds } : {}),
                ...(options.consumer_config ?? options.consumerConfig ? { consumer_config: options.consumer_config ?? options.consumerConfig } : {}),
                ...(options.consumer_secrets ?? options.consumerSecrets ? { consumer_secrets: options.consumer_secrets ?? options.consumerSecrets } : {}),
                ...(options.composition ? { composition: options.composition } : {})
            }),
            signal: AbortSignal.timeout(Math.max(1, timeoutSeconds) * 1000)
        });
        const text = await response.text();
        const payload = text ? JSON.parse(text) : null;
        if (response.status >= 400) {
            throw new Error(`agent call failed: ${response.status}${text ? `: ${text.slice(0, 500)}` : ""}`);
        }
        return {
            result: isObject(payload) && "result" in payload ? payload.result : payload,
            events: isObject(payload) && Array.isArray(payload.events) ? payload.events.filter(isObject) : [],
            artifacts: isObject(payload) && Array.isArray(payload.artifacts) ? payload.artifacts.filter(isObject) : [],
            grant_id: grantIdFromToken(grant)
        };
    }
    async resolveTarget(target) {
        const clean = target.replace(/\/+$/, "");
        if (clean.startsWith("http://") || clean.startsWith("https://"))
            return clean;
        const cached = this.resolvedUrls.get(clean);
        if (cached)
            return cached;
        if (!this.discovery) {
            throw new Error(`a2a target ${target} is not a URL and no discovery client is attached`);
        }
        const discovered = this.discovery.getAgent
            ? await this.discovery.getAgent(clean)
            : await this.discovery.get_agent(clean);
        const url = discovered.url?.replace(/\/+$/, "");
        if (!url)
            throw new Error(`a2a target ${target} resolved without a URL`);
        this.resolvedUrls.set(clean, url);
        return url;
    }
}
export class HttpSandboxHandle {
    baseUrl;
    name;
    timeoutSeconds;
    authToken;
    grantToken;
    constructor(options) {
        this.baseUrl = options.baseUrl.replace(/\/+$/, "");
        this.name = options.name;
        this.timeoutSeconds = options.timeoutSeconds ?? 1200;
        this.authToken = options.authToken ?? undefined;
        this.grantToken = options.grantToken ?? undefined;
    }
    async exec(cmd, args = [], options = {}) {
        return this.postExec({
            cmd,
            args,
            timeout_seconds: options.timeout ?? this.timeoutSeconds
        });
    }
    async shell(script, options = {}) {
        return this.postExec({
            script,
            timeout_seconds: options.timeout ?? this.timeoutSeconds
        });
    }
    async stop() {
        await this.kill();
    }
    async kill() {
        await sandboxFetch(`${this.baseUrl}/v1/sandboxes/${encodeURIComponent(this.name)}`, {
            method: "DELETE",
            headers: sandboxHeaders(this.authToken, this.grantToken),
            timeoutSeconds: 10
        });
    }
    async logs(_options = {}) {
        return "";
    }
    async postExec(body) {
        const timeoutSeconds = Number(body.timeout_seconds ?? this.timeoutSeconds) + 30;
        const response = await sandboxFetch(`${this.baseUrl}/v1/sandboxes/${encodeURIComponent(this.name)}/exec`, {
            method: "POST",
            headers: { ...sandboxHeaders(this.authToken, this.grantToken), "content-type": "application/json" },
            body: JSON.stringify(body),
            timeoutSeconds
        });
        return normalizeExecResult(await response.json());
    }
}
export class HttpSandboxClient {
    baseUrl;
    defaultWorkspace;
    timeoutSeconds;
    authToken;
    grantToken;
    constructor(baseUrl, options = {}) {
        this.baseUrl = baseUrl.replace(/\/+$/, "");
        this.defaultWorkspace = options.defaultWorkspace ?? undefined;
        this.timeoutSeconds = options.timeoutSeconds ?? 1200;
        this.authToken = options.authToken ?? undefined;
        this.grantToken = options.grantToken ?? undefined;
    }
    async create(spec) {
        const body = {
            name: spec.name,
            bucket: spec.workspace ?? this.defaultWorkspace ?? `agent-${spec.name}`,
            image: spec.image ?? "python:3.11-slim",
            memory_mib: spec.memory_mib ?? 512,
            cpus: spec.cpus ?? 1,
            labels: spec.labels ?? {}
        };
        const response = await sandboxFetch(`${this.baseUrl}/v1/sandboxes`, {
            method: "POST",
            headers: { ...sandboxHeaders(this.authToken, this.grantToken), "content-type": "application/json" },
            body: JSON.stringify(body),
            timeoutSeconds: this.timeoutSeconds + 30
        });
        const data = await response.json();
        const name = isObject(data) && typeof data.name === "string" ? data.name : spec.name;
        return this.get(name);
    }
    async get(name) {
        return new HttpSandboxHandle({
            baseUrl: this.baseUrl,
            name,
            timeoutSeconds: this.timeoutSeconds,
            authToken: this.authToken,
            grantToken: this.grantToken
        });
    }
    async list() {
        const response = await sandboxFetch(`${this.baseUrl}/v1/sandboxes`, {
            headers: sandboxHeaders(this.authToken, this.grantToken),
            timeoutSeconds: 10
        });
        const data = await response.json();
        return Array.isArray(data) ? data.filter((item) => typeof item === "string") : [];
    }
    async remove(name) {
        await sandboxFetch(`${this.baseUrl}/v1/sandboxes/${encodeURIComponent(name)}`, {
            method: "DELETE",
            headers: sandboxHeaders(this.authToken, this.grantToken),
            timeoutSeconds: 10
        });
    }
    async runPython(code, options = {}) {
        return this.runOneShot("/v1/run_python", {
            code,
            image: options.image ?? "python:3.11-slim",
            ...options
        });
    }
    async run_python(code, options = {}) {
        return this.runPython(code, options);
    }
    async runShell(script, options = {}) {
        return this.runOneShot("/v1/run_shell", {
            script,
            image: options.image ?? "python:3.11-slim",
            ...options
        });
    }
    async run_shell(script, options = {}) {
        return this.runShell(script, options);
    }
    async runOneShot(path, body) {
        const payload = { ...body };
        const workspace = payload.workspace;
        delete payload.workspace;
        payload.bucket ??= workspace ?? this.defaultWorkspace ?? "agent-workspace";
        payload.timeout_seconds ??= this.timeoutSeconds;
        const response = await sandboxFetch(`${this.baseUrl}${path}`, {
            method: "POST",
            headers: { ...sandboxHeaders(this.authToken, this.grantToken), "content-type": "application/json" },
            body: JSON.stringify(payload),
            timeoutSeconds: Number(payload.timeout_seconds) + 30
        });
        return normalizeExecResult(await response.json());
    }
}
export class WorkspaceClient {
    grantToken;
    grant;
    baseUrl;
    scopeRequester;
    constructor(options) {
        this.baseUrl = options.cpUrl.replace(/\/+$/, "");
        this.grantToken = options.grantToken;
        this.grant = options.grant ?? decodeGrantPayload(options.grantToken);
        this.scopeRequester = options.requestScope;
    }
    get outputsPrefix() {
        return this.grant?.outputs_prefix ?? null;
    }
    get outputs_prefix() {
        return this.outputsPrefix;
    }
    get bucket() {
        return this.grant?.bucket ?? null;
    }
    get mode() {
        return this.grant?.mode ?? null;
    }
    get allowPatterns() {
        return this.grant?.allow_patterns ?? [];
    }
    get allow_patterns() {
        return this.allowPatterns;
    }
    get denyPatterns() {
        return this.grant?.deny_patterns ?? [];
    }
    get deny_patterns() {
        return this.denyPatterns;
    }
    get writePrefixes() {
        return this.grant?.write_prefixes ?? [];
    }
    get write_prefixes() {
        return this.writePrefixes;
    }
    get currentGrantId() {
        return this.grant?.grant_id ?? null;
    }
    get current_grant_id() {
        return this.currentGrantId;
    }
    get currentGrant() {
        return this.grant;
    }
    get current_grant() {
        return this.currentGrant;
    }
    isWritableOutput(path) {
        const writePrefixes = this.writePrefixes;
        if (writePrefixes.length > 0)
            return writePrefixes.some((prefix) => path.startsWith(prefix));
        const outputsPrefix = this.outputsPrefix;
        return Boolean(outputsPrefix && path.startsWith(outputsPrefix));
    }
    is_writable_output(path) {
        return this.isWritableOutput(path);
    }
    async delegate(options) {
        if (!this.bucket)
            throw new WorkspaceDenied("workspace grant does not expose a bucket");
        if (process.env.A2A_GRANT_SIGNING_KEY && this.grant) {
            const [, token] = delegateGrant(this.grant, {
                issuer: this.grant.issuer ?? "self",
                audience: options.audience,
                bucket: this.bucket,
                mode: options.mode ?? "read_only",
                allow_patterns: options.allow_patterns ?? options.allowPatterns ?? ["**"],
                deny_patterns: options.deny_patterns ?? options.denyPatterns ?? [],
                outputs_prefix: options.outputs_prefix ?? options.outputsPrefix ?? null,
                write_prefixes: options.write_prefixes ?? options.writePrefixes ?? [],
                ttl_seconds: options.ttl_seconds ?? options.ttlSeconds ?? 300
            });
            return token;
        }
        const nowSeconds = Math.floor(Date.now() / 1000);
        const parent = this.grant;
        const payload = {
            grant_id: randomBytes(8).toString("hex"),
            issuer: parent?.issuer ?? "self",
            audience: options.audience,
            bucket: this.bucket,
            mode: options.mode ?? "read_only",
            allow_patterns: options.allow_patterns ?? options.allowPatterns ?? ["**"],
            deny_patterns: [
                ...(parent?.deny_patterns ?? []),
                ...(options.deny_patterns ?? options.denyPatterns ?? [])
            ],
            outputs_prefix: options.outputs_prefix ?? options.outputsPrefix ?? null,
            write_prefixes: options.write_prefixes ?? options.writePrefixes ?? [],
            parent_grant_id: parent?.grant_id ?? null,
            delegation_depth: (parent?.delegation_depth ?? 0) + 1,
            expires_at: nowSeconds + (options.ttl_seconds ?? options.ttlSeconds ?? 300)
        };
        return grantTokenFromPayload(payload);
    }
    async exists(path) {
        const response = await fetch(this.url(`files/${path}`), {
            method: "HEAD",
            headers: this.headers()
        });
        if (response.status === 404)
            return false;
        if (response.status >= 400)
            throw new WorkspaceDenied(`workspace stat failed: ${response.status}`);
        return true;
    }
    async readBytes(path) {
        const response = await fetch(this.url(`files/${path}`), { headers: this.headers() });
        if (response.status === 404)
            throw new Error(`workspace file not found: ${path}`);
        if (response.status >= 400)
            throw new WorkspaceDenied(`workspace read failed: ${response.status}`);
        return new Uint8Array(await response.arrayBuffer());
    }
    async read_bytes(path) {
        return this.readBytes(path);
    }
    async readText(path) {
        return new TextDecoder().decode(await this.readBytes(path));
    }
    async read_text(path) {
        return this.readText(path);
    }
    async writeBytes(path, content, contentType = "application/octet-stream") {
        const response = await fetch(this.url(`files/${path}`), {
            method: "PUT",
            headers: { ...this.headers(), "content-type": contentType },
            body: content
        });
        if (response.status >= 400) {
            const detail = await response.text().catch(() => "");
            throw new WorkspaceDenied(`workspace write failed: ${response.status}${detail ? `: ${detail.slice(0, 500)}` : ""}`);
        }
    }
    async write_bytes(path, content, contentType = "application/octet-stream") {
        return this.writeBytes(path, content, contentType);
    }
    async writeText(path, content, contentType = "text/plain; charset=utf-8") {
        await this.writeBytes(path, Buffer.from(content, "utf8"), contentType);
    }
    async write_text(path, content, contentType = "text/plain; charset=utf-8") {
        return this.writeText(path, content, contentType);
    }
    async delete(path) {
        const response = await fetch(this.url(`files/${path}`), {
            method: "DELETE",
            headers: this.headers()
        });
        if (![204, 404].includes(response.status) && response.status >= 400) {
            throw new WorkspaceDenied(`workspace delete failed: ${response.status}`);
        }
    }
    async delete_path(path) {
        return this.delete(path);
    }
    async list() {
        const response = await fetch(this.url("files"), { headers: this.headers() });
        if (response.status >= 400)
            throw new WorkspaceDenied(`workspace list failed: ${response.status}`);
        const data = await response.json();
        return Array.isArray(data) ? data.filter(isObject).map((item) => ({
            path: String(item.path ?? ""),
            size_bytes: typeof item.size_bytes === "number" ? item.size_bytes : undefined,
            file_type: typeof item.file_type === "string" ? item.file_type : undefined
        })).filter((item) => item.path) : [];
    }
    async iterPaths() {
        return (await this.list()).map((item) => item.path);
    }
    async iter_paths() {
        return this.iterPaths();
    }
    async search(query, options = {}) {
        const limit = options.limit ?? 50;
        const prefix = options.prefix ?? "";
        const matcher = options.regex ? new RegExp(query, "i") : null;
        const normalizedQuery = query.toLowerCase();
        const files = await this.list();
        return files
            .filter((item) => !prefix || item.path.startsWith(prefix))
            .filter((item) => {
            const fileType = options.fileType ?? options.file_type;
            return !fileType || item.file_type === fileType;
        })
            .filter((item) => matcher ? matcher.test(item.path) : item.path.toLowerCase().includes(normalizedQuery))
            .slice(0, limit);
    }
    async view(path, options = {}) {
        const maxBytes = options.maxBytes ?? options.max_bytes ?? 64_000;
        const bytes = await this.readBytes(path);
        const sliced = bytes.byteLength > maxBytes ? bytes.slice(0, maxBytes) : bytes;
        return {
            path,
            content: new TextDecoder().decode(sliced),
            truncated: bytes.byteLength > maxBytes,
            bytes: bytes.byteLength
        };
    }
    async requestAccess(options) {
        if (!this.scopeRequester)
            throw new Error("workspace requestAccess requires a RunContext-bound workspace client");
        return this.scopeRequester(options);
    }
    async request_access(options) {
        return this.requestAccess(options);
    }
    headers() {
        return { "x-a2a-grant": this.grantToken };
    }
    url(path) {
        const suffix = path.split("/").map(encodeURIComponent).join("/");
        return `${this.baseUrl}/v1/workspace-grants/${suffix}`;
    }
}
export class LocalWorkspaceClient {
    files = new Map();
    bucket;
    issuer;
    mode;
    allowPatterns;
    allow_patterns;
    denyPatterns;
    deny_patterns;
    outputsPrefix;
    outputs_prefix;
    writePrefixes;
    write_prefixes;
    currentGrantId = null;
    current_grant_id = null;
    currentGrant = null;
    current_grant = null;
    constructor(files = {}, options = {}) {
        this.bucket = options.bucket ?? "local";
        this.issuer = options.issuer ?? "local";
        this.mode = options.mode ?? "read_write_overlay";
        this.allowPatterns = options.allow_patterns ?? options.allowPatterns ?? ["**"];
        this.allow_patterns = this.allowPatterns;
        this.denyPatterns = options.deny_patterns ?? options.denyPatterns ?? [];
        this.deny_patterns = this.denyPatterns;
        this.outputsPrefix = options.outputs_prefix ?? options.outputsPrefix ?? "outputs/";
        this.outputs_prefix = this.outputsPrefix;
        this.writePrefixes = normalizeWritePrefixes(this.outputsPrefix, options.write_prefixes ?? options.writePrefixes ?? []);
        this.write_prefixes = this.writePrefixes;
        for (const [path, value] of Object.entries(files)) {
            this.files.set(path, bytesFromArtifactData(value));
        }
    }
    installGrant(grant) {
        this.currentGrant = grant;
        this.current_grant = grant;
        this.currentGrantId = grant.grant_id ?? null;
        this.current_grant_id = this.currentGrantId;
        this.mode = grant.mode ?? this.mode;
        this.allowPatterns = grant.allow_patterns ?? this.allowPatterns;
        this.allow_patterns = this.allowPatterns;
        this.denyPatterns = grant.deny_patterns ?? this.denyPatterns;
        this.deny_patterns = this.denyPatterns;
        this.outputsPrefix = grant.outputs_prefix ?? this.outputsPrefix;
        this.outputs_prefix = this.outputsPrefix;
        this.writePrefixes = normalizeWritePrefixes(this.outputsPrefix, grant.write_prefixes ?? this.writePrefixes);
        this.write_prefixes = this.writePrefixes;
    }
    install_grant(grant) {
        this.installGrant(grant);
    }
    isWritableOutput(path) {
        if (this.mode === "read_only")
            return false;
        if (this.writePrefixes.length > 0)
            return this.writePrefixes.some((prefix) => path.startsWith(prefix));
        return !this.isDenied(path);
    }
    is_writable_output(path) {
        return this.isWritableOutput(path);
    }
    exists(path) {
        return this.files.has(path);
    }
    read(path) {
        return this.readBytes(path);
    }
    readBytes(path) {
        const value = this.files.get(path);
        if (!value)
            throw new Error(`workspace file not found: ${path}`);
        return value;
    }
    read_bytes(path) {
        return this.readBytes(path);
    }
    readText(path) {
        return new TextDecoder().decode(this.readBytes(path));
    }
    read_text(path) {
        return this.readText(path);
    }
    write(path, data) {
        this.writeBytes(path, data);
    }
    writeBytes(path, data) {
        if (!this.isWritableOutput(path))
            throw new WorkspaceDenied(`workspace write denied: ${path}`);
        this.files.set(path, bytesFromArtifactData(data));
    }
    write_bytes(path, data) {
        this.writeBytes(path, data);
    }
    writeText(path, data) {
        this.writeBytes(path, data);
    }
    write_text(path, data) {
        this.writeText(path, data);
    }
    delete(path) {
        this.files.delete(path);
    }
    delete_path(path) {
        this.delete(path);
    }
    list() {
        return [...this.files.entries()]
            .filter(([path]) => !this.isDenied(path))
            .sort(([left], [right]) => left.localeCompare(right))
            .map(([path, bytes]) => ({
            path,
            size_bytes: bytes.byteLength,
            file_type: detectFileType(path)
        }));
    }
    iterPaths() {
        return this.list().map((item) => item.path);
    }
    iter_paths() {
        return this.iterPaths();
    }
    search(query, options = {}) {
        const terms = query.toLowerCase().split(/\W+/).filter(Boolean);
        const prefix = options.prefix ?? "";
        const fileType = options.fileType ?? options.file_type;
        const scored = this.list()
            .filter((item) => !prefix || item.path.startsWith(prefix))
            .filter((item) => !fileType || item.file_type === fileType)
            .map((item) => {
            const content = this.readText(item.path).toLowerCase();
            const haystack = `${item.path.toLowerCase()}\n${content}`;
            const score = terms.length === 0
                ? 1
                : terms.reduce((total, term) => total + countOccurrences(haystack, term), 0);
            return { item, score };
        })
            .filter(({ score }) => score > 0)
            .sort((left, right) => right.score - left.score);
        return scored.slice(0, options.limit ?? 20).map(({ item }) => item);
    }
    view(path, options = {}) {
        const maxBytes = options.maxBytes ?? options.max_bytes ?? 64_000;
        const bytes = this.readBytes(path);
        const sliced = bytes.byteLength > maxBytes ? bytes.slice(0, maxBytes) : bytes;
        return {
            path,
            content: new TextDecoder().decode(sliced),
            truncated: bytes.byteLength > maxBytes,
            bytes: bytes.byteLength
        };
    }
    async delegate(options) {
        const parent = this.currentGrant ?? {
            grant_id: this.currentGrantId ?? "local-parent",
            issuer: this.issuer,
            bucket: this.bucket,
            mode: this.mode ?? "read_write_overlay",
            allow_patterns: this.allowPatterns,
            deny_patterns: this.denyPatterns,
            outputs_prefix: this.outputsPrefix,
            write_prefixes: this.writePrefixes,
            delegation_depth: 0,
            max_delegation_depth: 40,
            expires_at: Math.floor(Date.now() / 1000) + 300
        };
        if (process.env.A2A_GRANT_SIGNING_KEY) {
            return delegateGrant(parent, {
                issuer: parent.issuer ?? this.issuer,
                audience: options.audience,
                bucket: this.bucket,
                mode: options.mode ?? "read_only",
                allow_patterns: options.allow_patterns ?? options.allowPatterns ?? ["**"],
                deny_patterns: options.deny_patterns ?? options.denyPatterns ?? [],
                outputs_prefix: options.outputs_prefix ?? options.outputsPrefix ?? null,
                write_prefixes: options.write_prefixes ?? options.writePrefixes ?? [],
                ttl_seconds: options.ttl_seconds ?? options.ttlSeconds ?? 300
            })[1];
        }
        return grantTokenFromPayload({
            ...parent,
            grant_id: randomBytes(8).toString("hex"),
            audience: options.audience,
            parent_grant_id: parent.grant_id ?? null,
            delegation_depth: (parent.delegation_depth ?? 0) + 1
        });
    }
    isDenied(path) {
        if (this.denyPatterns.some((pattern) => patternMatchesPath(pattern, path)))
            return true;
        return !this.allowPatterns.some((pattern) => patternMatchesPath(pattern, path));
    }
}
export const FileSystemWorkspaceClient = LocalWorkspaceClient;
export class MemoryClient {
    ctx;
    root;
    agentNamespace;
    agent_namespace;
    userNamespace;
    user_namespace;
    constructor(ctx, options = {}) {
        this.ctx = ctx;
        this.root = safeMemorySegment(options.rootPrefix ?? options.root_prefix ?? "memory");
        this.agentNamespace = safeMemorySegment(options.agentNamespace ?? options.agent_namespace ?? ctx.agent ?? "agent");
        this.agent_namespace = this.agentNamespace;
        this.userNamespace = safeMemorySegment(options.userNamespace ?? options.user_namespace ?? callerNamespace(ctx.caller));
        this.user_namespace = this.userNamespace;
    }
    get rootPrefix() {
        return this.root;
    }
    get root_prefix() {
        return this.rootPrefix;
    }
    forScope(options = {}) {
        return new MemoryClient(this.ctx, {
            rootPrefix: this.root,
            agentNamespace: options.agentNamespace ?? options.agent_namespace ?? this.agentNamespace,
            userNamespace: options.userNamespace ?? options.user_namespace ?? this.userNamespace
        });
    }
    for_scope(options = {}) {
        return this.forScope(options);
    }
    async putNote(key, value, options = {}) {
        const namespace = options.namespace ?? "notes";
        const path = this.notePath(namespace, key);
        const now = new Date().toISOString();
        const previous = await this.getNote(key, { namespace });
        const record = {
            key,
            namespace,
            value,
            metadata: { ...(options.metadata ?? {}) },
            created_at: previous?.created_at ?? now,
            updated_at: now
        };
        await this.ctx.workspace.writeText(path, JSON.stringify(record));
        return record;
    }
    async put_note(key, value, options = {}) {
        return this.putNote(key, value, options);
    }
    async remember(key, value, options = {}) {
        return this.putNote(key, value, options);
    }
    async getNote(key, options = {}) {
        const namespace = options.namespace ?? "notes";
        const path = this.notePath(namespace, key);
        if (!(await this.ctx.workspace.exists(path)))
            return null;
        return JSON.parse(await this.ctx.workspace.readText(path));
    }
    async get_note(key, options = {}) {
        return this.getNote(key, options);
    }
    async recall(key, options = {}) {
        return this.getNote(key, options);
    }
    async listNotes(options = {}) {
        const namespace = options.namespace ?? "notes";
        const prefix = this.namespacePrefix(namespace);
        const paths = await this.ctx.workspace.iterPaths();
        const records = [];
        for (const path of paths.sort()) {
            if (records.length >= (options.limit ?? 100))
                break;
            if (!path.startsWith(prefix) || !path.endsWith(".json"))
                continue;
            try {
                records.push(JSON.parse(await this.ctx.workspace.readText(path)));
            }
            catch {
                // Ignore malformed memory files; Python does the same.
            }
        }
        records.sort((left, right) => right.updated_at.localeCompare(left.updated_at));
        return records.slice(0, options.limit ?? 100);
    }
    async list_notes(options = {}) {
        return this.listNotes(options);
    }
    async searchNotes(query, options = {}) {
        const needle = query.toLowerCase().trim();
        const records = await this.listNotes({ namespace: options.namespace, limit: 500 });
        if (!needle)
            return records.slice(0, options.limit ?? 10);
        return records
            .filter((record) => JSON.stringify(record).toLowerCase().includes(needle))
            .slice(0, options.limit ?? 10);
    }
    async search_notes(query, options = {}) {
        return this.searchNotes(query, options);
    }
    async search(query, options = {}) {
        return this.searchNotes(query, options);
    }
    async appendLog(name, value, options = {}) {
        const path = this.logPath(name);
        const entry = {
            log: name,
            value,
            metadata: { ...(options.metadata ?? {}) },
            created_at: new Date().toISOString()
        };
        let existing = "";
        if (await this.ctx.workspace.exists(path)) {
            existing = await this.ctx.workspace.readText(path);
            if (existing && !existing.endsWith("\n"))
                existing += "\n";
        }
        await this.ctx.workspace.writeText(path, `${existing}${JSON.stringify(entry)}\n`, "application/jsonl");
        return entry;
    }
    async append_log(name, value, options = {}) {
        return this.appendLog(name, value, options);
    }
    async readLog(name, options = {}) {
        const path = this.logPath(name);
        if (!(await this.ctx.workspace.exists(path)))
            return [];
        const lines = (await this.ctx.workspace.readText(path)).split(/\r?\n/).filter((line) => line.trim());
        return lines.map((line) => JSON.parse(line)).slice(-(options.limit ?? 100));
    }
    async read_log(name, options = {}) {
        return this.readLog(name, options);
    }
    namespacePrefix(namespace) {
        return `${this.root}/${this.agentNamespace}/${this.userNamespace}/${safeMemorySegment(namespace)}/`;
    }
    notePath(namespace, key) {
        const clean = safeMemoryKey(key);
        return `${this.namespacePrefix(namespace)}${clean.endsWith(".json") ? clean : `${clean}.json`}`;
    }
    logPath(name) {
        const clean = safeMemoryKey(name);
        return `${this.root}/${this.agentNamespace}/${this.userNamespace}/logs/${clean.endsWith(".jsonl") ? clean : `${clean}.jsonl`}`;
    }
}
export class MetaAgentRunsClient {
    cpUrl;
    cpJwt;
    agentName;
    constructor(ctx, options = {}) {
        const cpUrl = ctx.cpUrl();
        const cpJwt = ctx.cpJwt();
        if (!cpUrl || !cpJwt) {
            throw new Error("meta-run persistence requires cp_url/cp_jwt; declare wants_cp_jwt=true on the agent");
        }
        this.cpUrl = cpUrl.replace(/\/+$/, "");
        this.cpJwt = cpJwt;
        this.agentName = safeMemorySegment(options.agentName ?? options.agent_name ?? ctx.agent ?? "agent");
    }
    async create(payload) {
        return this.request("POST", "", payload);
    }
    async list(options = {}) {
        const params = new URLSearchParams({ limit: String(options.limit ?? 50) });
        if (options.thread_id)
            params.set("thread_id", options.thread_id);
        if (options.status)
            params.set("status", options.status);
        return this.request("GET", `?${params.toString()}`);
    }
    async get(runId) {
        try {
            return await this.request("GET", `/${encodeURIComponent(runId)}`);
        }
        catch (error) {
            if (error instanceof Error && /not found/i.test(error.message))
                return null;
            throw error;
        }
    }
    async update(runId, fields) {
        return this.request("PATCH", `/${encodeURIComponent(runId)}`, fields);
    }
    async request(method, suffix, body) {
        return controlPlaneRequest(`${this.cpUrl}/v1/agents/${encodeURIComponent(this.agentName)}/meta-runs${suffix}`, method, this.cpJwt, body);
    }
}
export class ProtocolSimulationsClient {
    cpUrl;
    cpJwt;
    agentName;
    constructor(ctx, options = {}) {
        const cpUrl = ctx.cpUrl();
        const cpJwt = ctx.cpJwt();
        if (!cpUrl || !cpJwt) {
            throw new Error("protocol simulation helpers require cp_url/cp_jwt; declare wants_cp_jwt=true on the agent");
        }
        this.cpUrl = cpUrl.replace(/\/+$/, "");
        this.cpJwt = cpJwt;
        this.agentName = safeMemorySegment(options.agentName ?? options.agent_name ?? ctx.agent ?? "agent");
    }
    async listScenarios() {
        return this.request("GET", "/scenarios");
    }
    async list_scenarios() {
        return this.listScenarios();
    }
    async listRegistry() {
        return this.request("GET", "/protocol-registry");
    }
    async list_registry() {
        return this.listRegistry();
    }
    async checkRuntimeReadiness(payload = {}) {
        return this.request("POST", "/runtime-readiness", {
            requested: true,
            simulation_passed: false,
            policy_reviewed: false,
            owner_approved: false,
            operator_enabled: false,
            registry_enabled: false,
            no_critical_findings: false,
            redaction_reviewed: false,
            ...payload
        });
    }
    async check_runtime_readiness(payload = {}) {
        return this.checkRuntimeReadiness(payload);
    }
    async recordScenarioRun(jobId, options = {}) {
        return this.request("POST", `/${encodeURIComponent(jobId)}/scenario-runs`, {
            scenario_ids: options.scenario_ids ?? options.scenarioIds ?? [],
            cost_cents: options.cost_cents ?? options.costCents ?? 0
        });
    }
    async record_scenario_run(jobId, options = {}) {
        return this.recordScenarioRun(jobId, options);
    }
    async request(method, suffix, body) {
        return controlPlaneRequest(`${this.cpUrl}/v1/agents/${encodeURIComponent(this.agentName)}/protocol-simulations${suffix}`, method, this.cpJwt, body);
    }
}
export class DiscoveryClient {
    cpUrl;
    cpJwt;
    constructor(ctx, options = {}) {
        const cpUrl = options.cpUrl ?? options.cp_url ?? ctx.cpUrl();
        if (!cpUrl)
            throw new Error("discovery requires cp_url");
        this.cpUrl = cpUrl.replace(/\/+$/, "");
        this.cpJwt = options.cpJwt ?? options.cp_jwt ?? ctx.cpJwt();
    }
    async findAgents(options = {}) {
        const params = new URLSearchParams({ limit: String(options.limit ?? 10) });
        for (const tag of options.tags ?? [])
            params.append("tag", tag);
        if (options.capability)
            params.set("capability", options.capability);
        if (options.skill)
            params.set("skill", options.skill);
        const rows = await this.request(`/v1/agents?${params.toString()}`);
        const out = [];
        for (const row of rows ?? []) {
            const name = typeof row.name === "string" ? row.name : "";
            const url = typeof row.url === "string" ? row.url : null;
            let card = {};
            try {
                if (url) {
                    const response = await fetch(`${url.replace(/\/+$/, "")}/.well-known/agent-card`);
                    card = await response.json();
                }
                else if (name) {
                    const detail = await this.request(`/v1/agents/${encodeURIComponent(name)}`);
                    card = isObject(detail.card) ? detail.card : {};
                }
            }
            catch {
                continue;
            }
            out.push({ name, url, card });
        }
        return out;
    }
    async find_agents(options = {}) {
        return this.findAgents(options);
    }
    async getAgent(name) {
        const detail = await this.request(`/v1/agents/${encodeURIComponent(name)}`);
        return {
            name: String(detail.name ?? name),
            url: typeof detail.url === "string" ? detail.url : null,
            card: isObject(detail.card) ? detail.card : {}
        };
    }
    async get_agent(name) {
        return this.getAgent(name);
    }
    async request(path) {
        const response = await fetch(`${this.cpUrl}${path}`, {
            headers: {
                accept: "application/json",
                ...(this.cpJwt ? { authorization: `bearer ${this.cpJwt}` } : {})
            }
        });
        const text = await response.text();
        if (response.status >= 400)
            throw new Error(`discovery request failed: ${response.status}${text ? `: ${text.slice(0, 500)}` : ""}`);
        return text ? JSON.parse(text) : null;
    }
}
export class SubAgentToolkit {
    ctx;
    constructor(ctx) {
        this.ctx = ctx;
    }
    async listSubagents(options = {}) {
        return (await this.ctx.discover.findAgents(options)).map(agentSummary);
    }
    async list_subagents(options = {}) {
        return this.listSubagents(options);
    }
    async getSubagent(name) {
        return agentSummary(await this.ctx.discover.getAgent(name));
    }
    async get_subagent(name) {
        return this.getSubagent(name);
    }
    async callSubagent(name, skill, options = {}) {
        const target = await this.ctx.discover.getAgent(name);
        const call = await this.ctx.call(target.url || target.name, skill, options.args ?? {}, {
            grant: options.grant,
            timeoutSeconds: options.timeout
        });
        return {
            ok: true,
            agent: target.name,
            skill,
            result: call.result,
            events: call.events,
            artifacts: call.artifacts,
            grant_id: call.grant_id
        };
    }
    async call_subagent(name, skill, options = {}) {
        return this.callSubagent(name, skill, options);
    }
}
export class MissingScopes extends Error {
    missing;
    constructor(missing) {
        super(`missing scopes: ${missing.sort().join(", ")}`);
        this.name = "MissingScopes";
        this.missing = [...missing].sort();
    }
}
export class RunContext {
    request;
    agent;
    skill;
    handler;
    arguments;
    auth;
    caller;
    grantIds;
    grant_ids;
    randomSeed;
    random_seed;
    llm;
    events = [];
    artifacts = [];
    workspaceClient;
    sandboxClient;
    randomState;
    constructor(request) {
        this.request = request;
        this.agent = request.agent;
        this.skill = request.skill;
        this.handler = request.handler;
        this.arguments = request.arguments;
        this.auth = request.auth ?? null;
        this.caller = request.caller ?? null;
        this.grantIds = request.grant_ids ?? [];
        this.grant_ids = this.grantIds;
        this.randomSeed = request.random_seed ?? null;
        this.random_seed = this.randomSeed;
        this.randomState = seedToUint32(this.randomSeed ?? `${this.taskId}:${this.agent}:${this.skill}`);
        this.llm = createLlmAccessor(() => this.resolveLlmCreds());
    }
    get emittedEvents() {
        return [...this.events];
    }
    get emittedArtifacts() {
        return [...this.artifacts];
    }
    get taskId() {
        if (this.request.task_id)
            return this.request.task_id;
        const runId = this.request.composition?.run_id;
        if (typeof runId === "string" && runId)
            return runId;
        const taskId = this.request.composition?.task_id;
        if (typeof taskId === "string" && taskId)
            return taskId;
        return "local";
    }
    get task_id() {
        return this.taskId;
    }
    get grant() {
        return this.request.grant ? decodeGrantPayload(this.request.grant) : null;
    }
    get workspace() {
        if (this.request.workspace_client) {
            return this.request.workspace_client;
        }
        if (!this.request.grant) {
            throw new Error("missing workspace grant");
        }
        const cpUrl = this.cpUrl();
        if (!cpUrl) {
            throw new Error("missing control plane URL for workspace grant");
        }
        this.workspaceClient ??= new WorkspaceClient({
            cpUrl,
            grantToken: this.request.grant,
            grant: this.grant,
            requestScope: (options) => this.requestScope(options)
        });
        return this.workspaceClient;
    }
    get sandbox() {
        if (this.request.sandbox_client)
            return this.request.sandbox_client;
        const baseUrl = process.env.A2A_SANDBOX_URL;
        if (!baseUrl)
            throw new SandboxUnavailable();
        this.sandboxClient ??= new HttpSandboxClient(baseUrl, {
            defaultWorkspace: this.grant?.bucket,
            timeoutSeconds: Number(process.env.A2A_SANDBOX_TIMEOUT_S || "1200"),
            authToken: process.env.A2A_SANDBOX_TOKEN,
            grantToken: this.request.grant
        });
        return this.sandboxClient;
    }
    get memory() {
        return new MemoryClient(this);
    }
    get metaRuns() {
        return new MetaAgentRunsClient(this);
    }
    get meta_runs() {
        return this.metaRuns;
    }
    get protocolSimulations() {
        return new ProtocolSimulationsClient(this);
    }
    get protocol_simulations() {
        return this.protocolSimulations;
    }
    get discover() {
        if (this.request.discovery_client)
            return this.request.discovery_client;
        return new DiscoveryClient(this);
    }
    get subagents() {
        return new SubAgentToolkit(this);
    }
    workspaceBackend(_options = {}) {
        const artifactsRoot = `/${(this.workspace.writePrefixes[0] ?? this.workspace.outputsPrefix ?? "outputs/").replace(/^\/+|\/+$/g, "")}/.a2a-artifacts`;
        const thisContext = this;
        return {
            workspace: this.workspace,
            get sandbox() {
                return thisContext.sandbox;
            },
            artifacts_root: artifactsRoot,
            run_shell: (script, options = {}) => this.workspaceShell(script, options),
            run_python: (code, options = {}) => this.workspacePython(code, options),
            tools: () => [
                { name: "memory", client: this.memory },
                { name: "subagents", client: this.subagents }
            ]
        };
    }
    workspace_backend(options = {}) {
        return this.workspaceBackend(options);
    }
    deepagentsBackend(options = {}) {
        return this.workspaceBackend(options);
    }
    deepagents_backend(options = {}) {
        return this.deepagentsBackend(options);
    }
    random() {
        this.randomState = (1664525 * this.randomState + 1013904223) >>> 0;
        return this.randomState / 0x100000000;
    }
    async emitEvent(event) {
        const normalized = normalizeEvent(event);
        this.events.push(normalized);
        await this.request.emit?.(normalized);
    }
    async emit_event(event) {
        return this.emitEvent(event);
    }
    async emitProgress(message, payload = {}) {
        await this.emitEvent({ kind: "progress", payload: { message, ...payload } });
    }
    async emit_progress(message, payload = {}) {
        return this.emitProgress(message, payload);
    }
    async emitTextDelta(text) {
        await this.emitEvent({ kind: "text_delta", payload: { text } });
    }
    async emit_text_delta(text) {
        return this.emitTextDelta(text);
    }
    async emitError(message, options = {}) {
        await this.emitEvent({ kind: "error", payload: { message, ...options } });
    }
    async emit_error(message, options = {}) {
        return this.emitError(message, options);
    }
    async emitArtifact(ref) {
        const artifact = { ...ref };
        this.artifacts.push(artifact);
        await this.emitEvent({ kind: "artifact", payload: artifact });
    }
    async emit_artifact(ref) {
        return this.emitArtifact(ref);
    }
    async writeArtifact(name, data, contentType = "application/octet-stream", metadata = {}) {
        const bytes = bytesFromArtifactData(data);
        const path = this.artifactPath(name);
        let ref;
        if (this.request.grant && this.cpUrl()) {
            await this.workspace.writeBytes(path, bytes);
            ref = {
                name,
                path,
                uri: this.workspace.grant?.bucket ? `s3://${this.workspace.grant.bucket}/${path}` : `workspace://${path}`,
                mime_type: contentType,
                content_type: contentType,
                bytes: bytes.byteLength,
                metadata
            };
        }
        else {
            ref = {
                name,
                path,
                uri: `memory://${this.taskId}/${safeArtifactName(name)}`,
                mime_type: contentType,
                content_type: contentType,
                bytes: bytes.byteLength,
                metadata
            };
        }
        await this.emitArtifact(ref);
        return ref;
    }
    async write_artifact(name, data, contentType = "application/octet-stream", metadata = {}) {
        return this.writeArtifact(name, data, contentType, metadata);
    }
    secret(name) {
        const requestSecret = this.request.secrets?.[name];
        if (requestSecret != null && requestSecret !== "")
            return requestSecret;
        const value = process.env[name];
        if (value == null || value === "") {
            throw new Error(`missing secret: ${name}`);
        }
        return value;
    }
    consumerConfig(name, defaultValue) {
        const value = this.request.consumer_config?.[name];
        if (value === undefined)
            return defaultValue;
        return value;
    }
    consumer_config(name, defaultValue) {
        return this.consumerConfig(name, defaultValue);
    }
    consumerSecret(name) {
        const value = this.request.consumer_secrets?.[name];
        if (value == null || value === "") {
            throw new ConsumerSetupMissing(`missing consumer setup secret: ${name}`);
        }
        return value;
    }
    consumer_secret(name) {
        return this.consumerSecret(name);
    }
    cpJwt() {
        return this.request.cp_jwt ?? null;
    }
    cp_jwt() {
        return this.cpJwt();
    }
    cpUrl() {
        return this.request.cp_url ?? process.env.A2A_CP_URL ?? null;
    }
    cp_url() {
        return this.cpUrl();
    }
    requireScopes(required) {
        const present = scopesFromAuth(this.auth);
        const missing = required.filter((scope) => !present.has(scope));
        if (missing.length > 0)
            throw new MissingScopes(missing);
    }
    require_scopes(required) {
        return this.requireScopes(required);
    }
    async checkCancelled() {
        if (this.request.cancelled) {
            throw new CancelledByCaller();
        }
    }
    async check_cancelled() {
        return this.checkCancelled();
    }
    async ask(prompt, options = {}) {
        const questionId = randomId("question");
        const promise = waitForCallback(pendingAnswers, questionId, options.timeoutSeconds ?? options.timeout_seconds ?? 3600, `timed out waiting for answer: ${questionId}`);
        await this.emitEvent({
            kind: "question",
            payload: {
                question_id: questionId,
                prompt,
                metadata: options.metadata ?? {}
            }
        });
        return promise;
    }
    async collect(schema, options = {}) {
        const requestId = randomId("input");
        const promise = waitForCallback(pendingInputs, requestId, options.timeoutSeconds ?? options.timeout_seconds ?? 3600, `timed out waiting for input request: ${requestId}`);
        await this.emitEvent({
            kind: "input_request",
            payload: {
                request_id: requestId,
                schema,
                title: options.title,
                reason: options.reason,
                ui_schema: options.uiSchema ?? options.ui_schema ?? {}
            }
        });
        const raw = await promise;
        validateJsonSchemaValue(schema, raw);
        return raw;
    }
    async requestScope(options) {
        if (this.request.scope_expansion_allowed === false) {
            throw new WorkspaceDenied(`skill ${this.skill} does not allow scope expansion`);
        }
        const requestId = randomId("scope");
        const readPrefixes = normalizeStringList(options.readPrefixes ?? options.read_prefixes ?? options.read);
        const writePrefixes = normalizeStringList(options.writePrefixes ?? options.write_prefixes ?? options.write);
        const approvalTimeout = options.approvalTimeoutSeconds ?? options.approval_timeout_seconds ?? 3600;
        const promise = waitForCallback(pendingScopeRequests, requestId, approvalTimeout, `timed out waiting for scope grant: ${requestId}`);
        await this.emitEvent({
            kind: "scope_request",
            payload: {
                request_id: requestId,
                reason: options.reason ?? "agent requested additional workspace access",
                read_prefixes: readPrefixes,
                write_prefixes: writePrefixes,
                mode: options.mode ?? (writePrefixes.length > 0 ? "read_write_overlay" : "read_only"),
                ttl_seconds: options.ttlSeconds ?? options.ttl_seconds ?? null,
                approval_timeout_seconds: approvalTimeout
            }
        });
        const resolution = await promise;
        const grantToken = resolution.grant_token ?? resolution.grant ?? resolution.token;
        if (grantToken) {
            this.request.grant = grantToken;
            this.workspaceClient = undefined;
            return this.grant;
        }
        return resolution.payload ?? null;
    }
    async request_scope(options) {
        return this.requestScope(options);
    }
    async ensureRead(path, reason) {
        if (this.workspaceCoversRead(path))
            return;
        await this.requestScope({ read: path, reason });
    }
    async ensure_read(path, reason) {
        return this.ensureRead(path, reason);
    }
    async ensureWrite(path, reason) {
        if (this.workspaceCoversWrite(path))
            return;
        await this.requestScope({ write: path, reason });
    }
    async ensure_write(path, reason) {
        return this.ensureWrite(path, reason);
    }
    async ensureWorkspace(options) {
        const reads = normalizeStringList(options.readPrefixes ?? options.read_prefixes ?? options.read);
        const writes = normalizeStringList(options.writePrefixes ?? options.write_prefixes ?? options.write);
        const missingRead = reads.some((path) => !this.workspaceCoversRead(path));
        const missingWrite = writes.some((path) => !this.workspaceCoversWrite(path));
        if (missingRead || missingWrite || !this.request.grant) {
            await this.requestScope(options);
        }
        return this.workspace;
    }
    async ensure_workspace(options) {
        return this.ensureWorkspace(options);
    }
    async workspaceShell(script, options = {}) {
        const name = `workspace-sh-${randomId("sandbox").replace(/^sandbox-/, "").slice(0, 8)}`;
        const sandbox = await this.sandbox.create({
            name,
            image: typeof options.image === "string" ? options.image : "python:3.11-slim",
            workspace: this.grant?.bucket,
            memory_mib: typeof options.memory_mib === "number" ? options.memory_mib : 512,
            cpus: typeof options.cpus === "number" ? options.cpus : 1,
            labels: { workspace_write_policy: "workspace" }
        });
        try {
            return await sandbox.shell(script, {
                timeout: typeof options.timeout_seconds === "number"
                    ? options.timeout_seconds
                    : typeof options.timeout === "number"
                        ? options.timeout
                        : undefined
            });
        }
        finally {
            await sandbox.stop().catch(() => undefined);
            await this.sandbox.remove(sandbox.name).catch(() => undefined);
        }
    }
    async workspace_shell(script, options = {}) {
        return this.workspaceShell(script, options);
    }
    async workspacePython(code, options = {}) {
        const name = `workspace-py-${randomId("sandbox").replace(/^sandbox-/, "").slice(0, 8)}`;
        const sandbox = await this.sandbox.create({
            name,
            image: typeof options.image === "string" ? options.image : "python:3.11-slim",
            workspace: this.grant?.bucket,
            memory_mib: typeof options.memory_mib === "number" ? options.memory_mib : 512,
            cpus: typeof options.cpus === "number" ? options.cpus : 1,
            labels: { workspace_write_policy: "workspace" }
        });
        try {
            return await sandbox.exec("python", ["-c", code], {
                timeout: typeof options.timeout_seconds === "number"
                    ? options.timeout_seconds
                    : typeof options.timeout === "number"
                        ? options.timeout
                        : undefined
            });
        }
        finally {
            await sandbox.stop().catch(() => undefined);
            await this.sandbox.remove(sandbox.name).catch(() => undefined);
        }
    }
    async workspace_python(code, options = {}) {
        return this.workspacePython(code, options);
    }
    async call(target, skill, args = {}, options = {}) {
        const timeoutSeconds = options.timeoutSeconds ?? options.timeout_seconds ?? 1800;
        const grant = options.grant ?? this.request.grant ?? null;
        const targetIdentity = target;
        const composition = {
            ...(this.request.composition ?? {}),
            llm_budget_usd: options.llmBudgetUsd ?? options.llm_budget_usd
        };
        await this.emitEvent({
            kind: "composition_call_started",
            payload: { target: targetIdentity, skill, budget: composition }
        });
        try {
            const client = this.request.a2a_client ?? new HttpA2AClient({ defaultTimeout: timeoutSeconds });
            const result = await client.call(target, skill, {
                args,
                grant,
                cp_jwt: this.cpJwt(),
                cp_url: this.cpUrl(),
                llm_creds: this.request.llm_creds ?? null,
                consumer_config: options.consumer_config ?? options.consumerConfig ?? null,
                consumer_secrets: options.consumer_secrets ?? options.consumerSecrets ?? null,
                timeoutSeconds,
                composition
            });
            await this.emitEvent({
                kind: "composition_call_complete",
                payload: { target: targetIdentity, skill, grant_id: result.grant_id, budget: composition }
            });
            return result;
        }
        catch (error) {
            await this.emitEvent({
                kind: "composition_call_error",
                payload: {
                    target: targetIdentity,
                    skill,
                    message: error instanceof Error ? error.message : String(error),
                    budget: composition
                }
            });
            throw error;
        }
    }
    async mintGiteaToken(options = {}) {
        const cpUrl = this.cpUrl();
        const cpJwt = this.cpJwt();
        if (!cpUrl || !cpJwt)
            throw new Error("missing control plane credentials for Gitea token mint");
        const response = await fetch(`${cpUrl.replace(/\/+$/, "")}/v1/gitea/tokens`, {
            method: "POST",
            headers: { "content-type": "application/json", authorization: `Bearer ${cpJwt}` },
            body: JSON.stringify(options)
        });
        const text = await response.text();
        if (response.status >= 400)
            throw new Error(`gitea token mint failed: ${response.status}${text ? `: ${text.slice(0, 500)}` : ""}`);
        return text ? JSON.parse(text) : {};
    }
    async mint_gitea_token(options = {}) {
        return this.mintGiteaToken(options);
    }
    async releaseGiteaToken(tokenId) {
        const cpUrl = this.cpUrl();
        const cpJwt = this.cpJwt();
        if (!cpUrl || !cpJwt)
            throw new Error("missing control plane credentials for Gitea token release");
        const response = await fetch(`${cpUrl.replace(/\/+$/, "")}/v1/gitea/tokens/${encodeURIComponent(tokenId)}`, {
            method: "DELETE",
            headers: { authorization: `Bearer ${cpJwt}` }
        });
        if (response.status >= 400) {
            const text = await response.text().catch(() => "");
            throw new Error(`gitea token release failed: ${response.status}${text ? `: ${text.slice(0, 500)}` : ""}`);
        }
    }
    async release_gitea_token(tokenId) {
        return this.releaseGiteaToken(tokenId);
    }
    finalizeResponse(response) {
        const finalized = { ...response };
        if (finalized.events === undefined && this.events.length > 0) {
            finalized.events = this.emittedEvents;
        }
        if (finalized.artifacts === undefined && this.artifacts.length > 0) {
            finalized.artifacts = this.emittedArtifacts;
        }
        return finalized;
    }
    static answer(questionId, answer) {
        return resolvePending(pendingAnswers, questionId, answer);
    }
    static submitInput(requestId, value) {
        return resolvePending(pendingInputs, requestId, value);
    }
    static submit_input(requestId, value) {
        return RunContext.submitInput(requestId, value);
    }
    static resolveScopeGrant(requestId, resolution) {
        return resolvePending(pendingScopeRequests, requestId, resolution);
    }
    static resolve_scope_grant(requestId, resolution) {
        return RunContext.resolveScopeGrant(requestId, resolution);
    }
    static denyScope(requestId, reason = "scope denied") {
        return rejectPending(pendingScopeRequests, requestId, new WorkspaceDenied(reason));
    }
    static deny_scope(requestId, reason = "scope denied") {
        return RunContext.denyScope(requestId, reason);
    }
    artifactPath(name) {
        const safeName = safeArtifactName(name);
        const prefix = this.workspaceClient?.outputsPrefix ?? this.grant?.outputs_prefix ?? "outputs/";
        const cleanPrefix = prefix ? `${prefix.replace(/^\/+/, "").replace(/\/+$/, "")}/` : "";
        return `${cleanPrefix}${safeArtifactName(this.taskId)}/${safeName}`;
    }
    resolveLlmCreds() {
        if (this.request.llm_creds) {
            const normalized = normalizeLlmCreds(this.request.llm_creds, {
                source: typeof this.request.llm_creds.source === "string"
                    ? this.request.llm_creds.source
                    : this.request.llm_creds.api_key === this.request.grant
                        ? "platform"
                        : "caller"
            });
            if (normalized)
                return normalized;
        }
        if (process.env.AGENT_LLM_KEY) {
            return {
                base_url: process.env.AGENT_LLM_URL ?? "https://api.openai.com/v1",
                api_key: process.env.AGENT_LLM_KEY,
                model: process.env.AGENT_LLM_MODEL ?? "gpt-4o",
                source: "agent_byok",
                temperature_mode: "default",
                temperature: null,
                extra_body: {},
                metadata: {}
            };
        }
        return {
            base_url: `${process.env.A2A_LITELLM_URL ?? "http://litellm.llm.svc.cluster.local:4000"}/v1`,
            api_key: process.env.A2A_LITELLM_KEY ?? "",
            model: process.env.A2A_LITELLM_MODEL ?? "gpt-5.5",
            source: "platform",
            temperature_mode: "default",
            temperature: null,
            extra_body: {},
            metadata: {}
        };
    }
    workspaceCoversRead(path) {
        const grant = this.grant;
        if (!grant)
            return false;
        if ((grant.deny_patterns ?? []).some((pattern) => patternMatchesPath(pattern, path)))
            return false;
        const allow = grant.allow_patterns ?? [];
        return allow.length === 0 || allow.some((pattern) => patternMatchesPath(pattern, path));
    }
    workspaceCoversWrite(path) {
        const grant = this.grant;
        if (!grant)
            return false;
        if ((grant.deny_patterns ?? []).some((pattern) => patternMatchesPath(pattern, path)))
            return false;
        if (grant.mode === "read_only")
            return false;
        const prefixes = grant.write_prefixes ?? [];
        if (prefixes.length > 0)
            return prefixes.some((prefix) => path.startsWith(prefix));
        const outputs = grant.outputs_prefix;
        if (outputs && path.startsWith(outputs))
            return true;
        return grant.mode === "read_write_direct";
    }
}
export class LocalRunContext extends RunContext {
    artifactBytes = new Map();
    cancelled = false;
    onEvent;
    constructor(options = {}) {
        const onEvent = options.onEvent ?? options.on_event;
        super({
            agent: options.agent ?? "local-agent",
            skill: options.skill ?? "local-skill",
            handler: options.handler ?? options.skill ?? "local-skill",
            arguments: options.arguments ?? options.args ?? {},
            task_id: options.task_id ?? options.taskId ?? "local-task",
            auth: options.auth ?? {},
            caller: typeof options.caller === "string" ? { sub: options.caller } : options.caller ?? null,
            grant_ids: options.grant_ids ?? options.grantIds ?? [],
            random_seed: options.random_seed ?? options.randomSeed ?? null,
            secrets: options.secrets ?? {},
            workspace_client: options.workspace ?? undefined,
            sandbox_client: options.sandbox ?? undefined,
            a2a_client: options.a2a ?? undefined,
            discovery_client: options.discover ?? undefined,
            consumer_config: options.consumer_config ?? options.consumerConfig ?? null,
            consumer_secrets: options.consumer_secrets ?? options.consumerSecrets ?? null,
            emit: onEvent
        });
        this.onEvent = onEvent;
    }
    cancel() {
        this.cancelled = true;
        this.request.cancelled = true;
    }
    async checkCancelled() {
        if (this.cancelled)
            throw new CancelledByCaller(this.taskId);
        return super.checkCancelled();
    }
    async emitEvent(event) {
        const normalized = normalizeEvent(event);
        this.events.push(normalized);
        await this.onEvent?.(normalized);
    }
    async writeArtifact(name, data, contentType = "application/octet-stream", metadata = {}) {
        const bytes = bytesFromArtifactData(data);
        this.artifactBytes.set(name, bytes);
        const ref = {
            name,
            uri: `memory://${this.taskId}/${safeArtifactName(name)}`,
            mime_type: contentType,
            content_type: contentType,
            bytes: bytes.byteLength,
            metadata
        };
        await this.emitArtifact(ref);
        return ref;
    }
}
export class ReplayDivergence extends Error {
    idx;
    recorded;
    observed;
    constructor(idx, recorded, observed, message) {
        super(message ?? `replay divergence at idx=${idx}: recorded=${JSON.stringify(recorded)} observed=${JSON.stringify(observed)}`);
        this.name = "ReplayDivergence";
        this.idx = idx;
        this.recorded = recorded;
        this.observed = observed;
    }
}
export class ReplayLLM {
    session;
    creds;
    calls;
    responses;
    callIndex = 0;
    responseIndex = 0;
    constructor(session, creds = null) {
        this.session = session;
        this.creds = creds;
        this.calls = filterEvents(session, "llm_call");
        this.responses = filterEvents(session, "llm_response");
    }
    complete(callSignature = {}) {
        const callEvent = nextReplayEvent(this.calls, this.callIndex++, callSignature);
        const responseEvent = nextReplayEvent(this.responses, this.responseIndex++, callSignature);
        assertReplaySignature(callEvent, callSignature);
        return responseEvent.payload.response;
    }
}
export class ReplayToolCaller {
    session;
    calls;
    responses;
    callIndex = 0;
    responseIndex = 0;
    constructor(session) {
        this.session = session;
        this.calls = filterEvents(session, "tool_call");
        this.responses = filterEvents(session, "tool_response");
    }
    call(name, callSignature = {}) {
        const observed = { name, ...callSignature };
        const callEvent = nextReplayEvent(this.calls, this.callIndex++, observed);
        const responseEvent = nextReplayEvent(this.responses, this.responseIndex++, observed);
        if (callEvent.payload.name !== name) {
            throw new ReplayDivergence(callEvent.idx, callEvent.payload, observed);
        }
        assertReplaySignature(callEvent, callSignature, ["name"]);
        return responseEvent.payload.response;
    }
}
export class ReplayWorkspaceClient {
    session;
    reads;
    writes;
    readIndex = 0;
    writeIndex = 0;
    constructor(session) {
        this.session = session;
        this.reads = filterEvents(session, "workspace_read");
        this.writes = filterEvents(session, "workspace_write");
    }
    read(path) {
        const event = nextReplayEvent(this.reads, this.readIndex++, { path });
        if (event.payload.path !== path)
            throw new ReplayDivergence(event.idx, { path: event.payload.path }, { path });
        const data = event.payload.data ?? "";
        return typeof data === "string" ? Buffer.from(data, "utf8") : Buffer.from(data);
    }
    readBytes(path) {
        return this.read(path);
    }
    read_bytes(path) {
        return this.read(path);
    }
    readText(path) {
        return new TextDecoder().decode(this.read(path));
    }
    read_text(path) {
        return this.readText(path);
    }
    write(path, _data) {
        const event = nextReplayEvent(this.writes, this.writeIndex++, { path });
        if (event.payload.path !== path)
            throw new ReplayDivergence(event.idx, { path: event.payload.path }, { path });
    }
    writeBytes(path, data) {
        this.write(path, data);
    }
    write_bytes(path, data) {
        this.write(path, data);
    }
    writeText(path, data) {
        this.write(path, data);
    }
    write_text(path, data) {
        this.write(path, data);
    }
}
export async function replaySession(agent, session) {
    const args = extractRecordedArgs(session);
    return agent.localInvoke(session.skill_name, args, {
        task_id: session.task_id || "replay",
        caller: session.caller ? { sub: session.caller } : null,
        random_seed: session.random_seed,
        workspace: new ReplayWorkspaceClient(session)
    });
}
export const replay_session = replaySession;
export function publicAuth() {
    return {
        model: "a2a_pack.auth.NoAuth",
        strategy: "public",
        principal_schema: { title: "NoAuth", type: "object", properties: {}, additionalProperties: false },
        required: false
    };
}
export function apiKeyAuth(options = {}) {
    return {
        model: "a2a_pack.auth.APIKeyAuth",
        strategy: "api_key",
        resolver: options.resolver ?? null,
        principal_schema: {
            title: "APIKeyAuth",
            type: "object",
            properties: {
                api_key_id: { title: "Api Key Id", type: "string" },
                scopes: {
                    title: "Scopes",
                    type: "array",
                    items: { type: "string" },
                    default: []
                }
            },
            required: ["api_key_id"],
            additionalProperties: false
        },
        required: true
    };
}
export function jwtAuth(options = {}) {
    return {
        model: "a2a_pack.auth.JWTAuth",
        strategy: "jwt",
        resolver: options.resolver ?? null,
        principal_schema: {
            title: "JWTAuth",
            type: "object",
            properties: {
                sub: { title: "Sub", type: "string" },
                org_id: { anyOf: [{ type: "string" }, { type: "null" }], default: null, title: "Org Id" },
                email: { anyOf: [{ type: "string" }, { type: "null" }], default: null, title: "Email" },
                scopes: {
                    title: "Scopes",
                    type: "array",
                    items: { type: "string" },
                    default: []
                }
            },
            required: ["sub"],
            additionalProperties: false
        },
        required: true
    };
}
export function platformUserAuth(options = {}) {
    return {
        model: "a2a_pack.auth.PlatformUserAuth",
        strategy: "platform_user",
        resolver: options.resolver ?? null,
        principal_schema: {
            title: "PlatformUserAuth",
            type: "object",
            properties: {
                sub: { title: "Sub", type: "string" },
                user_id: { anyOf: [{ type: "integer" }, { type: "null" }], default: null, title: "User Id" },
                email: { anyOf: [{ type: "string" }, { type: "null" }], default: null, title: "Email" },
                org_id: { anyOf: [{ type: "string" }, { type: "null" }], default: null, title: "Org Id" },
                org_slug: { anyOf: [{ type: "string" }, { type: "null" }], default: null, title: "Org Slug" },
                scopes: {
                    title: "Scopes",
                    type: "array",
                    items: { type: "string" },
                    default: []
                }
            },
            required: ["sub"],
            additionalProperties: false
        },
        required: true
    };
}
export function customAuth(options) {
    return {
        model: options.model,
        strategy: "custom",
        resolver: options.resolver ?? null,
        principal_schema: options.principal_schema,
        required: options.required ?? true
    };
}
export function skill(declaration) {
    validateSkill(declaration);
    return declaration;
}
// Documented name (parity with the Python SDK's @a2a.tool): typed callables
// are tools; the agent card still publishes them under `skills` (A2A spec
// vocabulary), and `skill` stays a permanent alias.
export const tool = skill;
function normalizeRuntime(runtime) {
    const { webhooks, ...rest } = runtime;
    const endpoints = runtime.endpoints ?? webhooks;
    const normalized = endpoints ? { ...rest, endpoints } : rest;
    const access = normalized.account_access;
    const calls = access?.platform_skill_calls ?? 0;
    if (calls < 0 || !Number.isInteger(calls)) {
        throw new Error("runtime.account_access.platform_skill_calls must be a non-negative integer");
    }
    if (calls > 0 && access?.required !== true) {
        throw new Error("runtime.account_access.required must be true when platform_skill_calls is non-zero");
    }
    if (calls > 0
        && normalized.llm_provisioning !== undefined
        && !["platform", "platform_or_caller_provided"].includes(normalized.llm_provisioning)) {
        throw new Error("runtime.account_access.platform_skill_calls requires platform-capable llm_provisioning");
    }
    return normalized;
}
export function compileAgent(agentClass, options = {}) {
    const identity = agentClass.agent;
    if (!identity?.name)
        throw new Error("agent.agent.name is required");
    const skills = agentClass.skills.map(normalizeSkill);
    if (skills.length === 0)
        throw new Error("agent must declare at least one skill");
    const names = new Set();
    for (const item of skills) {
        if (names.has(item.name))
            throw new Error(`duplicate skill: ${item.name}`);
        names.add(item.name);
    }
    return {
        schema_version: "2026-06-04",
        language: options.language ?? "typescript",
        name: identity.name,
        description: identity.description ?? "",
        version: identity.version ?? "0.1.0",
        entrypoint: options.entrypoint ?? {},
        skills,
        capabilities: agentClass.capabilities,
        input_modes: agentClass.inputModes,
        output_modes: agentClass.outputModes,
        required_secrets: agentClass.requiredSecrets,
        required_env: agentClass.requiredEnv,
        consumer_setup: normalizeConsumerSetup(agentClass.consumerSetup),
        runtime: normalizeRuntime({ sandbox: "microsandbox", ...agentClass.runtime }),
        template_lineage: agentClass.templateLineage,
        meta_agent_manifest: agentClass.metaAgentManifest,
        state_schema: agentClass.stateSchema,
        workspace_access: agentClass.workspaceAccess,
        config_schema: agentClass.configSchema,
        auth: agentClass.auth,
        metadata: options.metadata ?? {}
    };
}
export async function writeAgentDsl(agentClass, path = ".a2a/agent.dsl.json", options = {}) {
    const dsl = compileAgent(agentClass, options);
    await mkdir(dirname(path), { recursive: true });
    await writeFile(path, `${JSON.stringify(dsl, null, 2)}\n`, "utf8");
    return dsl;
}
export function serveWorker(agentClass, handlers, options = {}) {
    const dsl = compileAgent(agentClass);
    const handlerNames = new Set(dsl.skills.map((item) => item.handler));
    const skillsByHandler = new Map(dsl.skills.map((item) => [item.handler, item]));
    const server = createServer(async (req, res) => {
        try {
            if (await handleCallbackRequest(req, res))
                return;
            if (req.method === "POST" && req.url?.startsWith("/_a2a/invoke-stream/")) {
                await handleStreamingWorkerInvoke(req, res, handlerNames, handlers, skillsByHandler);
                return;
            }
            if (req.method !== "POST" || !req.url?.startsWith("/_a2a/invoke/")) {
                writeJson(res, 404, { error: "not found" });
                return;
            }
            const handlerName = decodeURIComponent(req.url.slice("/_a2a/invoke/".length));
            if (!handlerNames.has(handlerName) || typeof handlers[handlerName] !== "function") {
                writeJson(res, 404, { error: `unknown handler: ${handlerName}` });
                return;
            }
            const body = await readJson(req);
            const request = body;
            const spec = skillsByHandler.get(handlerName);
            if (spec)
                validateJsonSchemaValue(spec.input_schema, request.arguments, "input");
            const raw = await handlers[handlerName](request);
            const response = normalizeWorkerResponse(raw);
            if (spec)
                validateJsonSchemaValue(spec.output_schema, response.result, "output");
            writeJson(res, 200, response);
        }
        catch (error) {
            writeJson(res, 500, { error: error instanceof Error ? error.message : String(error) });
        }
    });
    disableLongRunningWorkerTimeouts(server);
    server.listen(options.port ?? 9001, options.host ?? "127.0.0.1");
    return server;
}
export function serveAgent(agentClass, options = {}) {
    const dsl = compileAgent(agentClass);
    const agent = new agentClass();
    const handlerNames = new Set(dsl.skills.map((item) => item.handler));
    let startupPromise = null;
    const lifecycleContext = () => new RunContext({
        agent: dsl.name,
        skill: "__lifecycle__",
        handler: "__lifecycle__",
        arguments: {}
    });
    const ensureStarted = async () => {
        startupPromise ??= agent.startup(lifecycleContext());
        await startupPromise;
    };
    const server = createServer(async (req, res) => {
        try {
            if (await handleCallbackRequest(req, res))
                return;
            if (req.method === "GET" && req.url?.split("?", 1)[0] === "/_a2a/health") {
                await ensureStarted();
                const ok = await agent.health();
                writeJson(res, ok ? 200 : 503, { ok });
                return;
            }
            if (req.method === "POST" && req.url?.startsWith("/_a2a/invoke-stream/")) {
                await ensureStarted();
                await handleStreamingAgentInvoke(req, res, handlerNames, agent);
                return;
            }
            if (req.method !== "POST" || !req.url?.startsWith("/_a2a/invoke/")) {
                writeJson(res, 404, { error: "not found" });
                return;
            }
            const handlerName = decodeURIComponent(req.url.slice("/_a2a/invoke/".length));
            if (!handlerNames.has(handlerName)) {
                writeJson(res, 404, { error: `unknown handler: ${handlerName}` });
                return;
            }
            await ensureStarted();
            const body = await readJson(req);
            const request = { ...body, handler: handlerName };
            const response = await agent.dispatch(request);
            writeJson(res, 200, response);
        }
        catch (error) {
            writeJson(res, 500, { error: error instanceof Error ? error.message : String(error) });
        }
    });
    disableLongRunningWorkerTimeouts(server);
    const close = server.close.bind(server);
    server.close = ((callback) => {
        void agent.shutdown(lifecycleContext()).finally(() => close(callback));
        return server;
    });
    server.listen(options.port ?? 9001, options.host ?? "127.0.0.1");
    return server;
}
async function handleCallbackRequest(req, res) {
    if (req.method !== "POST" || !req.url)
        return false;
    const path = req.url.split("?", 1)[0];
    const answerPrefix = "/_a2a/answers/";
    const inputPrefix = "/_a2a/input-requests/";
    const scopeGrantPrefix = "/_a2a/scope-grants/";
    const scopeDenyPrefix = "/_a2a/scope-denials/";
    if (path.startsWith(answerPrefix)) {
        const body = await readJson(req);
        const id = decodeURIComponent(path.slice(answerPrefix.length));
        const ok = RunContext.answer(id, callbackValue(body, ["answer", "value", "result"]));
        writeJson(res, ok ? 200 : 404, { ok });
        return true;
    }
    if (path.startsWith(inputPrefix)) {
        const body = await readJson(req);
        const id = decodeURIComponent(path.slice(inputPrefix.length));
        const ok = RunContext.submitInput(id, callbackValue(body, ["input", "value", "result", "data"]));
        writeJson(res, ok ? 200 : 404, { ok });
        return true;
    }
    if (path.startsWith(scopeGrantPrefix)) {
        const body = await readJson(req);
        const id = decodeURIComponent(path.slice(scopeGrantPrefix.length));
        const ok = RunContext.resolveScopeGrant(id, body);
        writeJson(res, ok ? 200 : 404, { ok });
        return true;
    }
    if (path.startsWith(scopeDenyPrefix)) {
        const body = await readJson(req);
        const id = decodeURIComponent(path.slice(scopeDenyPrefix.length));
        const reason = isObject(body) && typeof body.reason === "string" ? body.reason : "scope denied";
        const ok = RunContext.denyScope(id, reason);
        writeJson(res, ok ? 200 : 404, { ok });
        return true;
    }
    return false;
}
async function handleStreamingWorkerInvoke(req, res, handlerNames, handlers, skillsByHandler) {
    const handlerName = decodeURIComponent(req.url.slice("/_a2a/invoke-stream/".length));
    if (!handlerNames.has(handlerName) || typeof handlers[handlerName] !== "function") {
        writeSseError(res, 404, `unknown handler: ${handlerName}`);
        return;
    }
    await streamDispatch(res, async (emit) => {
        const body = await readJson(req);
        const request = { ...body, handler: handlerName, emit };
        const spec = skillsByHandler.get(handlerName);
        if (spec)
            validateJsonSchemaValue(spec.input_schema, request.arguments, "input");
        const response = normalizeWorkerResponse(await handlers[handlerName](request));
        if (spec)
            validateJsonSchemaValue(spec.output_schema, response.result, "output");
        return response;
    });
}
async function handleStreamingAgentInvoke(req, res, handlerNames, agent) {
    const handlerName = decodeURIComponent(req.url.slice("/_a2a/invoke-stream/".length));
    if (!handlerNames.has(handlerName)) {
        writeSseError(res, 404, `unknown handler: ${handlerName}`);
        return;
    }
    await streamDispatch(res, async (emit) => {
        const body = await readJson(req);
        const request = { ...body, handler: handlerName, emit };
        return agent.dispatch(request);
    });
}
async function streamDispatch(res, dispatch) {
    const emittedEvents = [];
    writeSseHead(res);
    writeSse(res, { type: "started" });
    try {
        const response = await dispatch((event) => {
            emittedEvents.push(event);
            writeSse(res, toEventFrame(event));
        });
        writeSse(res, {
            type: "result",
            result: response.result,
            events: response.events ?? emittedEvents,
            artifacts: response.artifacts ?? []
        });
    }
    catch (error) {
        writeSse(res, {
            type: "error",
            status: 500,
            detail: error instanceof Error ? error.message : String(error)
        });
    }
    finally {
        res.write("data: [DONE]\n\n");
        res.end();
    }
}
function toEventFrame(event) {
    const kind = typeof event.kind === "string"
        ? event.kind
        : typeof event.type === "string"
            ? event.type
            : "progress";
    const { kind: _kind, type: _type, payload: rawPayload, ...rest } = event;
    const payload = isObject(rawPayload) ? rawPayload : rest;
    return { type: "event", kind, payload };
}
function disableLongRunningWorkerTimeouts(server) {
    server.timeout = 0;
    server.requestTimeout = 0;
    server.headersTimeout = 0;
    server.keepAliveTimeout = 0;
}
function normalizeSkill(declaration) {
    validateSkill(declaration);
    return {
        name: declaration.name,
        description: declaration.description ?? "",
        handler: declaration.handler ?? declaration.name,
        tags: declaration.tags ?? [],
        scopes: declaration.scopes ?? [],
        stream: declaration.stream ?? false,
        policy: {
            timeout_seconds: declaration.policy?.timeout_seconds ?? null,
            idempotent: declaration.policy?.idempotent ?? false,
            max_retries: declaration.policy?.max_retries ?? 0,
            cost_class: declaration.policy?.cost_class ?? null,
            allow_scope_expansion: declaration.policy?.allow_scope_expansion ?? false,
            grant_mode: declaration.policy?.grant_mode ?? null,
            grant_allow_patterns: declaration.policy?.grant_allow_patterns ?? [],
            grant_deny_patterns: declaration.policy?.grant_deny_patterns ?? [],
            grant_outputs_prefix: declaration.policy?.grant_outputs_prefix ?? null,
            grant_write_prefixes: declaration.policy?.grant_write_prefixes ?? [],
            grant_ttl_seconds: declaration.policy?.grant_ttl_seconds ?? null,
            grant_run_timeout_seconds: declaration.policy?.grant_run_timeout_seconds ?? null,
            grant_approval_timeout_seconds: declaration.policy?.grant_approval_timeout_seconds ?? null,
            grant_scope_approval_timeout_seconds: declaration.policy?.grant_scope_approval_timeout_seconds ?? null
        },
        input_schema: declaration.input_schema,
        output_schema: declaration.output_schema
    };
}
function validateSkill(declaration) {
    if (!declaration.name)
        throw new Error("skill.name is required");
    if (declaration.input_schema?.type !== "object") {
        throw new Error(`skill ${declaration.name}: input_schema must be an object schema`);
    }
    if (typeof declaration.input_schema.properties !== "object" || declaration.input_schema.properties === null) {
        throw new Error(`skill ${declaration.name}: input_schema.properties is required`);
    }
    if (!Array.isArray(declaration.input_schema.required)) {
        throw new Error(`skill ${declaration.name}: input_schema.required must be an array`);
    }
    for (const key of declaration.input_schema.required) {
        if (typeof key !== "string")
            throw new Error(`skill ${declaration.name}: required values must be strings`);
        if (!(key in declaration.input_schema.properties)) {
            throw new Error(`skill ${declaration.name}: required field ${key} missing from properties`);
        }
    }
    if (!declaration.output_schema || Object.keys(declaration.output_schema).length === 0) {
        throw new Error(`skill ${declaration.name}: output_schema is required`);
    }
}
function skillForName(agentClass, skillName) {
    return compileAgent(agentClass).skills.find((item) => item.name === skillName) ?? null;
}
function skillForHandler(agentClass, handlerName) {
    return compileAgent(agentClass).skills.find((item) => item.handler === handlerName) ?? null;
}
function normalizeWorkerResponse(value) {
    if (isObject(value) && "result" in value) {
        return value;
    }
    return { result: value };
}
function isObject(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}
function normalizeEvent(event) {
    const raw = event;
    if (typeof raw.kind === "string") {
        return {
            kind: raw.kind,
            payload: isObject(raw.payload) ? raw.payload : {}
        };
    }
    if (typeof raw.type === "string") {
        const { type, payload, ...rest } = raw;
        return {
            kind: type,
            payload: isObject(payload) ? payload : rest
        };
    }
    return { kind: "event", payload: raw };
}
function decodeGrantPayload(token) {
    const rawPayload = token.split(".", 1)[0];
    if (!rawPayload)
        return null;
    try {
        const padded = rawPayload.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(rawPayload.length / 4) * 4, "=");
        const parsed = JSON.parse(Buffer.from(padded, "base64").toString("utf8"));
        if (!isObject(parsed))
            return null;
        return parsed;
    }
    catch {
        return null;
    }
}
function grantTokenFromPayload(payload) {
    return `${Buffer.from(JSON.stringify(payload)).toString("base64url")}.signature`;
}
function grantIdFromToken(token) {
    if (!token)
        return null;
    return decodeGrantPayload(token)?.grant_id ?? null;
}
function bytesFromArtifactData(data) {
    if (typeof data === "string")
        return Buffer.from(data, "utf8");
    if (Buffer.isBuffer(data))
        return data;
    return data;
}
function safeArtifactName(name) {
    const clean = name.replace(/\\/g, "/").split("/").filter(Boolean).join("/");
    return clean.replace(/[^A-Za-z0-9._/-]/g, "_").replace(/\.\.(\/|$)/g, "");
}
function sandboxHeaders(authToken, grantToken) {
    const headers = {};
    if (authToken)
        headers.authorization = `Bearer ${authToken}`;
    if (grantToken)
        headers["x-a2a-grant"] = grantToken;
    return headers;
}
async function sandboxFetch(url, options) {
    const { timeoutSeconds, ...request } = options;
    const method = request.method ?? "GET";
    let response;
    try {
        response = await fetch(url, {
            ...request,
            signal: AbortSignal.timeout(Math.max(1, timeoutSeconds) * 1000)
        });
    }
    catch (error) {
        throw new SandboxRuntimeError(`sandbox request failed: ${method} ${url}`, {
            method,
            url,
            timeoutSeconds,
            causeType: error instanceof Error ? error.name : typeof error,
            causeMessage: error instanceof Error ? error.message : String(error)
        });
    }
    if (response.status >= 400) {
        const body = await response.text().catch(() => "");
        throw new SandboxRuntimeError(`sandbox request failed: ${method} ${url} status=${response.status}`, {
            statusCode: response.status,
            responseBody: body || undefined,
            method,
            url,
            timeoutSeconds
        });
    }
    return response;
}
function normalizeExecResult(value) {
    const data = isObject(value) ? value : {};
    return {
        stdout: typeof data.stdout === "string" ? data.stdout : "",
        stderr: typeof data.stderr === "string" ? data.stderr : "",
        exit_code: typeof data.exit_code === "number" ? data.exit_code : 0,
        truncated: typeof data.truncated === "boolean" ? data.truncated : false,
        files: Array.isArray(data.files)
            ? data.files.filter(isObject).map((item) => ({ ...item }))
            : []
    };
}
function normalizeLlmCreds(value, options) {
    const apiKey = typeof value.api_key === "string" ? value.api_key : "";
    const baseUrl = typeof value.base_url === "string" ? value.base_url : "";
    const model = typeof value.model === "string" ? value.model : "";
    if (!apiKey || !baseUrl || !model)
        return null;
    const rawTemperature = value.temperature;
    const parsedTemperature = rawTemperature === null || rawTemperature === undefined
        ? null
        : Number(rawTemperature);
    const extraBody = isObject(value.extra_body) ? value.extra_body : {};
    const metadata = isObject(value.metadata) ? value.metadata : {};
    return {
        base_url: baseUrl,
        api_key: apiKey,
        model,
        source: typeof value.source === "string" ? value.source : options.source,
        temperature_mode: typeof value.temperature_mode === "string" ? value.temperature_mode : "default",
        temperature: Number.isFinite(parsedTemperature) ? parsedTemperature : null,
        extra_body: { ...extraBody },
        metadata: { ...metadata }
    };
}
function createLlmAccessor(resolve) {
    const accessor = (() => resolve());
    for (const key of ["base_url", "api_key", "model", "source", "temperature_mode", "temperature", "extra_body", "metadata"]) {
        Object.defineProperty(accessor, key, {
            enumerable: true,
            get: () => resolve()[key]
        });
    }
    return accessor;
}
function validateJsonSchemaValue(schema, value, path = "input") {
    const anyOf = schema.anyOf;
    if (Array.isArray(anyOf)) {
        const errors = [];
        for (const option of anyOf) {
            if (!isObject(option))
                continue;
            try {
                validateJsonSchemaValue(option, value, path);
                return;
            }
            catch (error) {
                errors.push(error instanceof Error ? error.message : String(error));
            }
        }
        throw new Error(`${path} does not match any allowed schema${errors.length ? `: ${errors[0]}` : ""}`);
    }
    const oneOf = schema.oneOf;
    if (Array.isArray(oneOf)) {
        let matches = 0;
        let lastError = "";
        for (const option of oneOf) {
            if (!isObject(option))
                continue;
            try {
                validateJsonSchemaValue(option, value, path);
                matches += 1;
            }
            catch (error) {
                lastError = error instanceof Error ? error.message : String(error);
            }
        }
        if (matches !== 1)
            throw new Error(matches === 0 ? `${path} does not match any allowed schema: ${lastError}` : `${path} matches multiple schemas`);
        return;
    }
    const type = schema.type;
    const allowedTypes = Array.isArray(type) ? type : typeof type === "string" ? [type] : [];
    if (allowedTypes.length > 0 && !allowedTypes.some((item) => jsonTypeMatches(item, value))) {
        throw new Error(`${path} does not match schema type ${allowedTypes.join("|")}`);
    }
    const enumValues = schema.enum;
    if (Array.isArray(enumValues) && !enumValues.some((item) => Object.is(item, value))) {
        throw new Error(`${path} must be one of ${enumValues.map(String).join(", ")}`);
    }
    if ("const" in schema && !Object.is(schema.const, value)) {
        throw new Error(`${path} must equal ${String(schema.const)}`);
    }
    if (typeof value === "string") {
        if (typeof schema.minLength === "number" && value.length < schema.minLength) {
            throw new Error(`${path} must have at least ${schema.minLength} characters`);
        }
        if (typeof schema.maxLength === "number" && value.length > schema.maxLength) {
            throw new Error(`${path} must have at most ${schema.maxLength} characters`);
        }
        if (typeof schema.pattern === "string" && !(new RegExp(schema.pattern).test(value))) {
            throw new Error(`${path} does not match pattern ${schema.pattern}`);
        }
    }
    if (typeof value === "number" && Number.isFinite(value)) {
        if (typeof schema.minimum === "number" && value < schema.minimum) {
            throw new Error(`${path} must be >= ${schema.minimum}`);
        }
        if (typeof schema.exclusiveMinimum === "number" && value <= schema.exclusiveMinimum) {
            throw new Error(`${path} must be > ${schema.exclusiveMinimum}`);
        }
        if (typeof schema.maximum === "number" && value > schema.maximum) {
            throw new Error(`${path} must be <= ${schema.maximum}`);
        }
        if (typeof schema.exclusiveMaximum === "number" && value >= schema.exclusiveMaximum) {
            throw new Error(`${path} must be < ${schema.exclusiveMaximum}`);
        }
        if (typeof schema.multipleOf === "number" && schema.multipleOf !== 0 && !Number.isInteger(value / schema.multipleOf)) {
            throw new Error(`${path} must be a multiple of ${schema.multipleOf}`);
        }
    }
    if ((allowedTypes.includes("object") || isObject(schema.properties)) && isObject(value)) {
        const properties = isObject(schema.properties) ? schema.properties : {};
        const required = Array.isArray(schema.required) ? schema.required.filter((item) => typeof item === "string") : [];
        for (const key of required) {
            if (!(key in value))
                throw new Error(`${path}.${key} is required`);
        }
        if (schema.additionalProperties === false) {
            const known = new Set(Object.keys(properties));
            const unknown = Object.keys(value).filter((key) => !known.has(key));
            if (unknown.length > 0)
                throw new Error(`${path} has unknown properties: ${unknown.sort().join(", ")}`);
        }
        if (typeof schema.minProperties === "number" && Object.keys(value).length < schema.minProperties) {
            throw new Error(`${path} must have at least ${schema.minProperties} properties`);
        }
        if (typeof schema.maxProperties === "number" && Object.keys(value).length > schema.maxProperties) {
            throw new Error(`${path} must have at most ${schema.maxProperties} properties`);
        }
        for (const [key, child] of Object.entries(properties)) {
            if (key in value && isObject(child))
                validateJsonSchemaValue(child, value[key], `${path}.${key}`);
        }
    }
    if (allowedTypes.includes("array") && Array.isArray(value)) {
        if (typeof schema.minItems === "number" && value.length < schema.minItems) {
            throw new Error(`${path} must have at least ${schema.minItems} items`);
        }
        if (typeof schema.maxItems === "number" && value.length > schema.maxItems) {
            throw new Error(`${path} must have at most ${schema.maxItems} items`);
        }
        if (schema.uniqueItems === true) {
            const seen = new Set();
            for (const item of value) {
                const key = stableJson(item);
                if (seen.has(key))
                    throw new Error(`${path} must contain unique items`);
                seen.add(key);
            }
        }
        if (isObject(schema.items)) {
            value.forEach((item, index) => validateJsonSchemaValue(schema.items, item, `${path}[${index}]`));
        }
    }
}
function applyJsonSchemaDefaults(schema, value) {
    if (!isObject(value))
        throw new Error("config must be an object");
    const out = { ...value };
    const properties = isObject(schema.properties) ? schema.properties : {};
    for (const [key, child] of Object.entries(properties)) {
        if (!isObject(child))
            continue;
        if (!(key in out) && "default" in child) {
            out[key] = cloneJsonValue(child.default);
        }
        else if (key in out && isObject(out[key])) {
            out[key] = applyJsonSchemaDefaults(child, out[key]);
        }
    }
    return out;
}
function cloneJsonValue(value) {
    if (value === undefined)
        return undefined;
    return JSON.parse(JSON.stringify(value));
}
function jsonTypeMatches(type, value) {
    if (type === "null")
        return value === null;
    if (type === "array")
        return Array.isArray(value);
    if (type === "object")
        return isObject(value);
    if (type === "integer")
        return Number.isInteger(value);
    if (type === "number")
        return typeof value === "number" && Number.isFinite(value);
    if (type === "boolean")
        return typeof value === "boolean";
    if (type === "string")
        return typeof value === "string";
    return true;
}
function randomId(prefix) {
    return `${prefix}-${randomBytes(8).toString("hex")}`;
}
function waitForCallback(map, id, timeoutSeconds, timeoutMessage) {
    return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
            map.delete(id);
            reject(new Error(timeoutMessage));
        }, Math.max(1, timeoutSeconds) * 1000);
        map.set(id, {
            resolve,
            reject,
            timer
        });
    });
}
function resolvePending(map, id, value) {
    const pending = map.get(id);
    if (!pending)
        return false;
    clearTimeout(pending.timer);
    map.delete(id);
    pending.resolve(value);
    return true;
}
function rejectPending(map, id, error) {
    const pending = map.get(id);
    if (!pending)
        return false;
    clearTimeout(pending.timer);
    map.delete(id);
    pending.reject(error);
    return true;
}
function callbackValue(body, keys) {
    if (!isObject(body))
        return body;
    for (const key of keys) {
        if (key in body)
            return body[key];
    }
    return body;
}
function normalizeStringList(value) {
    if (typeof value === "string")
        return value ? [value] : [];
    return Array.isArray(value) ? value.filter((item) => typeof item === "string" && item.length > 0) : [];
}
function seedToUint32(value) {
    if (typeof value === "number" && Number.isFinite(value))
        return value >>> 0;
    let hash = 2166136261;
    for (const char of String(value)) {
        hash ^= char.charCodeAt(0);
        hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
}
function patternMatchesPath(pattern, path) {
    if (pattern === "*" || pattern === "**")
        return true;
    const normalized = pattern.replace(/\\/g, "/");
    if (normalized.endsWith("/**"))
        return path.startsWith(normalized.slice(0, -3));
    if (normalized.endsWith("*"))
        return path.startsWith(normalized.slice(0, -1));
    return path === normalized || path.startsWith(`${normalized.replace(/\/+$/, "")}/`);
}
function detectFileType(path) {
    const lower = path.toLowerCase();
    if (lower.endsWith(".py"))
        return "python";
    if (lower.endsWith(".ts") || lower.endsWith(".tsx"))
        return "typescript";
    if (lower.endsWith(".js") || lower.endsWith(".jsx"))
        return "javascript";
    if (lower.endsWith(".yaml") || lower.endsWith(".yml"))
        return "yaml";
    if (lower.endsWith(".json"))
        return "json";
    if (lower.endsWith(".toml"))
        return "toml";
    if (lower.endsWith(".md"))
        return "markdown";
    if (lower.endsWith(".sql"))
        return "sql";
    if (lower.endsWith(".sh"))
        return "shell";
    return "other";
}
function countOccurrences(haystack, needle) {
    if (!needle)
        return 0;
    let count = 0;
    let index = haystack.indexOf(needle);
    while (index !== -1) {
        count += 1;
        index = haystack.indexOf(needle, index + needle.length);
    }
    return count;
}
function callerNamespace(caller) {
    if (!caller)
        return "anonymous";
    for (const key of ["sub", "user", "user_id", "agent", "name"]) {
        const value = caller[key];
        if (typeof value === "string" && value)
            return value;
    }
    return "anonymous";
}
function safeMemorySegment(value) {
    const clean = String(value || "default").replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^-+|-+$/g, "");
    return clean || "default";
}
function safeMemoryKey(value) {
    return String(value || "note")
        .replace(/\\/g, "/")
        .split("/")
        .filter(Boolean)
        .map(safeMemorySegment)
        .join("/") || "note";
}
function agentSummary(agent) {
    const skills = Array.isArray(agent.card.skills) ? agent.card.skills.filter(isObject) : [];
    const capabilities = isObject(agent.card.capabilities) ? Object.keys(agent.card.capabilities).sort() : [];
    return {
        name: agent.name,
        url: agent.url ?? null,
        description: typeof agent.card.description === "string" ? agent.card.description : "",
        version: typeof agent.card.version === "string" ? agent.card.version : "",
        capabilities,
        skills: skills.map((skill) => ({
            name: skill.name,
            description: skill.description,
            tags: Array.isArray(skill.tags) ? skill.tags : [],
            input_schema: isObject(skill.input_schema) ? skill.input_schema : {}
        }))
    };
}
async function emitReceiptAndReplay(ctx, options) {
    if (!process.env.A2A_RECEIPT_SIGNING_KEY && !process.env.A2A_REPLAY_SIGNING_KEY)
        return;
    let receiptId = "";
    if (process.env.A2A_RECEIPT_SIGNING_KEY) {
        try {
            const [receipt, token] = sealReceipt({
                agent_name: options.agentName,
                agent_version: options.agentVersion,
                skill_name: options.skillName,
                started_at: options.startedAt,
                caller: callerPrincipal(ctx.caller),
                task_id: ctx.taskId,
                inputs: options.inputs,
                result: options.result,
                status: options.status,
                error_type: options.errorType,
                grant_ids: ctx.grantIds,
                artifacts: ctx.emittedArtifacts
            });
            receiptId = receipt.receipt_id;
            await ctx.emitEvent({ kind: "receipt_sealed", payload: { token, receipt_id: receipt.receipt_id } });
        }
        catch (error) {
            await ctx.emitEvent({
                kind: "receipt_error",
                payload: { message: error instanceof Error ? error.message : String(error), type: error instanceof Error ? error.name : typeof error }
            }).catch(() => undefined);
        }
    }
    if (process.env.A2A_REPLAY_SIGNING_KEY) {
        try {
            const session = options.recorder.buildSession({ receipt_id: receiptId });
            const [, token] = sealReplaySession(session);
            await ctx.emitEvent({
                kind: "replay_sealed",
                payload: { token, session_id: session.session_id, receipt_id: receiptId }
            });
        }
        catch (error) {
            await ctx.emitEvent({
                kind: "receipt_error",
                payload: { message: error instanceof Error ? error.message : String(error), type: error instanceof Error ? error.name : typeof error }
            }).catch(() => undefined);
        }
    }
}
function callerPrincipal(caller) {
    if (!caller)
        return "";
    for (const key of ["agent", "sub", "user", "user_id", "name"]) {
        const value = caller[key];
        if (typeof value === "string" && value)
            return value;
        if (typeof value === "number")
            return String(value);
    }
    return "";
}
function stableJson(value) {
    return JSON.stringify(sortJsonValue(value));
}
function sortJsonValue(value) {
    if (Array.isArray(value))
        return value.map(sortJsonValue);
    if (!isObject(value))
        return value;
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, sortJsonValue(value[key])]));
}
function previewJson(value, limit = 240) {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return text.length <= limit ? text : `${text.slice(0, Math.max(0, limit - 1))}…`;
}
function splitSignedToken(token, ErrorClass) {
    if (!token || !token.includes("."))
        throw new ErrorClass("malformed signed token");
    const [payload, signature] = token.split(".");
    if (!payload || !signature || token.split(".").length !== 2)
        throw new ErrorClass("malformed signed token");
    return [payload, signature];
}
function signingKeyFromEnv(value) {
    if (value.includes("BEGIN"))
        return value;
    const raw = decodeKeyMaterial(value);
    if (raw.length !== 32)
        return value;
    return createPrivateKey({
        key: Buffer.concat([
            Buffer.from("302e020100300506032b657004220420", "hex"),
            raw
        ]),
        format: "der",
        type: "pkcs8"
    });
}
function verifyingKeyFromEnv(value) {
    if (value.includes("BEGIN"))
        return value;
    const raw = decodeKeyMaterial(value);
    if (raw.length !== 32)
        return createPublicKey(value);
    return createPublicKey({
        key: Buffer.concat([
            Buffer.from("302a300506032b6570032100", "hex"),
            raw
        ]),
        format: "der",
        type: "spki"
    });
}
function decodeKeyMaterial(value) {
    const clean = value.trim().startsWith("base64:")
        ? value.trim().slice("base64:".length).trim()
        : value.trim();
    try {
        return Buffer.from(clean, "base64");
    }
    catch {
        return Buffer.from(clean, "base64url");
    }
}
function normalizeWritePrefixes(outputsPrefix, writePrefixes) {
    const out = [];
    const add = (prefix) => {
        if (!prefix)
            return;
        const clean = prefix.replace(/^\/+/, "").replace(/\/+$/, "");
        if (clean && !out.includes(`${clean}/`))
            out.push(`${clean}/`);
    };
    add(outputsPrefix);
    for (const prefix of writePrefixes)
        add(prefix);
    return out;
}
function modeRank(mode) {
    if (mode === "read_write_direct")
        return 2;
    if (mode === "read_write_overlay")
        return 1;
    return 0;
}
function patternCovered(pattern, parentPatterns) {
    return parentPatterns.some((parent) => (parent === "**" ||
        parent === pattern ||
        (parent.endsWith("/**") && pattern.startsWith(parent.slice(0, -3))) ||
        (parent.endsWith("*") && pattern.startsWith(parent.slice(0, -1)))));
}
function mergePatterns(inherited, requested) {
    const out = [];
    for (const pattern of [...inherited, ...requested]) {
        if (pattern && !out.includes(pattern))
            out.push(pattern);
    }
    return out;
}
function assertWriteScopeWithinParent(parent, childWritePrefixes) {
    if (childWritePrefixes.length === 0)
        return;
    if ((parent.mode ?? "read_only") === "read_only") {
        throw new GrantDelegationDenied("read-only parent grant cannot delegate writes");
    }
    const parentPrefixes = normalizeWritePrefixes(parent.outputs_prefix ?? null, parent.write_prefixes ?? []);
    if (parentPrefixes.length > 0) {
        for (const prefix of childWritePrefixes) {
            if (!parentPrefixes.some((parentPrefix) => prefix.startsWith(parentPrefix))) {
                throw new GrantDelegationDenied(`child write prefix ${prefix} is outside parent grant`);
            }
        }
        return;
    }
    for (const prefix of childWritePrefixes) {
        const probe = `${prefix.replace(/\/+$/, "")}/__a2a_probe__`;
        if (!patternCovered(probe, parent.allow_patterns ?? [])) {
            throw new GrantDelegationDenied(`child write prefix ${prefix} is outside parent grant`);
        }
    }
}
function nextReplayEvent(events, index, observed) {
    const event = events[index];
    if (!event)
        throw new ReplayDivergence(-1, null, observed);
    return event;
}
function assertReplaySignature(event, observed, ignoreKeys = []) {
    for (const [key, value] of Object.entries(event.payload)) {
        if (ignoreKeys.includes(key) || !(key in observed))
            continue;
        if (!Object.is(observed[key], value)) {
            throw new ReplayDivergence(event.idx, event.payload, observed);
        }
    }
}
function extractRecordedArgs(session) {
    const start = iterEvents(session).find((event) => event.kind === "skill_start");
    return isObject(start?.payload.args) ? { ...start.payload.args } : {};
}
async function controlPlaneRequest(url, method, cpJwt, body) {
    const response = await fetch(url, {
        method,
        headers: {
            authorization: `Bearer ${cpJwt}`,
            ...(body === undefined ? {} : { "content-type": "application/json" })
        },
        body: body === undefined ? undefined : JSON.stringify(body)
    });
    const text = await response.text();
    if (response.status === 404)
        throw new Error("control-plane resource not found");
    if (response.status >= 400) {
        throw new Error(`control-plane request failed: ${response.status}${text ? `: ${text.slice(0, 500)}` : ""}`);
    }
    return text ? JSON.parse(text) : null;
}
function normalizeConsumerSetup(setup) {
    if (setup instanceof ConsumerSetup)
        return setup.toJSON();
    return new ConsumerSetup(setup.fields ?? []).toJSON();
}
function isConsumerSetupInputType(value) {
    return [
        "text",
        "password",
        "url",
        "email",
        "textarea",
        "number",
        "boolean",
        "select"
    ].includes(value);
}
function scopesFromAuth(auth) {
    if (auth === null)
        return new Set();
    const scopes = new Set();
    const list = auth.scopes;
    if (Array.isArray(list)) {
        for (const item of list) {
            if (typeof item === "string")
                scopes.add(item);
        }
    }
    const scope = auth.scope;
    if (typeof scope === "string") {
        for (const item of scope.split(/\s+/)) {
            if (item)
                scopes.add(item);
        }
    }
    return scopes;
}
async function readJson(req) {
    const chunks = [];
    for await (const chunk of req)
        chunks.push(Buffer.from(chunk));
    const raw = Buffer.concat(chunks).toString("utf8");
    return raw ? JSON.parse(raw) : {};
}
function writeJson(res, status, body) {
    res.statusCode = status;
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify(body));
}
function writeSseHead(res) {
    if (res.headersSent)
        return;
    res.statusCode = 200;
    res.setHeader("content-type", "text/event-stream");
    res.setHeader("cache-control", "no-cache");
    res.setHeader("connection", "keep-alive");
    res.setHeader("x-accel-buffering", "no");
}
function writeSse(res, body) {
    writeSseHead(res);
    res.write(`data: ${JSON.stringify(body)}\n\n`);
}
function writeSseError(res, status, detail) {
    writeSse(res, { type: "error", status, detail });
    res.write("data: [DONE]\n\n");
    res.end();
}
//# sourceMappingURL=index.js.map