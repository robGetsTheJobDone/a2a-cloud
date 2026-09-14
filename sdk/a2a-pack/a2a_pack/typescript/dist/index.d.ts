import { type Server } from "node:http";
export type JsonSchema = Record<string, unknown>;
export type AgentDslLanguage = "typescript" | "javascript";
export type AuthStrategy = "public" | "api_key" | "platform_user" | "jwt" | "custom";
export type ConsumerSetupKind = "config" | "secret";
export type ConsumerSetupInputType = "text" | "password" | "url" | "email" | "textarea" | "number" | "boolean" | "select";
export type ConsumerSetupFieldWire = {
    name: string;
    kind: ConsumerSetupKind;
    label?: string | null;
    description: string;
    required: boolean;
    input_type: ConsumerSetupInputType;
    options: string[];
};
export type ConsumerSetupWire = {
    fields: ConsumerSetupFieldWire[];
};
export type AgentIdentity = {
    name: string;
    description: string;
    version?: string;
};
export type AgentDslAuth = {
    model: string;
    strategy: AuthStrategy;
    principal_schema: JsonSchema;
    resolver?: string | null;
    required: boolean;
};
export type SkillPolicy = {
    timeout_seconds?: number | null;
    idempotent?: boolean;
    max_retries?: number;
    cost_class?: string | null;
    allow_scope_expansion?: boolean;
    grant_mode?: string | null;
    grant_allow_patterns?: string[];
    grant_deny_patterns?: string[];
    grant_outputs_prefix?: string | null;
    grant_write_prefixes?: string[];
    grant_ttl_seconds?: number | null;
    grant_run_timeout_seconds?: number | null;
    grant_approval_timeout_seconds?: number | null;
    grant_scope_approval_timeout_seconds?: number | null;
};
export type EndpointMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD" | "OPTIONS";
export type AgentEndpoint = {
    name?: string | null;
    path: string;
    methods?: EndpointMethod[];
    method?: EndpointMethod | EndpointMethod[];
    skill: string;
    body_arg?: string;
    headers_arg?: string | null;
    query_arg?: string | null;
};
export type SkillDeclaration = {
    name: string;
    description?: string;
    handler?: string;
    tags?: string[];
    scopes?: string[];
    stream?: boolean;
    policy?: SkillPolicy;
    input_schema: JsonSchema;
    output_schema: JsonSchema;
};
export type UploadedFile = {
    path: string;
    filename: string;
    media_type: string;
    size_bytes: number;
};
export type FileUploadOptions = {
    accept?: string[];
    maxBytes?: number;
    max_bytes?: number;
    multiple?: boolean;
    description?: string;
};
export declare function uploadedFileSchema(options?: FileUploadOptions): JsonSchema;
export declare const fileUploadSchema: typeof uploadedFileSchema;
export type AgentRuntime = {
    lifecycle?: "ephemeral" | "session" | "warm";
    availability?: "on_demand" | "always_on";
    state?: "none" | "session" | "durable";
    sandbox?: "microsandbox";
    resources?: {
        cpu?: string;
        memory?: string;
        gpu?: number;
        max_runtime_seconds?: number;
    };
    concurrency?: number;
    egress?: {
        allow_hosts?: string[];
        allow_internal_services?: string[];
        deny_internet_by_default?: boolean;
    };
    tools_used?: string[];
    llm_provisioning?: "platform" | "platform_or_caller_provided" | "caller_provided" | "agent_byok";
    account_access?: {
        required?: boolean;
        platform_skill_calls?: number;
        after_trial?: "byok";
    };
    platform_resources?: {
        memory?: {
            tiers?: Array<"files" | "kv" | "vector">;
            namespace?: string;
            scope?: "agent" | "user" | "thread";
            retention?: "ephemeral" | "durable";
        };
        databases?: Array<{
            name: string;
            engine?: "postgres";
            provider?: "neon";
            scope?: "user" | "org";
            branch?: string;
            access_mode?: "read_only" | "read_write" | "owner";
            role?: "read_only" | "read_write" | "owner";
            env?: {
                url?: string;
            };
            migrations?: {
                path?: string;
            };
            migrations_path?: string;
            scale_to_zero?: boolean;
        }>;
    };
    wants_cp_jwt?: boolean;
    endpoints?: AgentEndpoint[];
    webhooks?: AgentEndpoint[];
};
export type AgentDslEntrypoint = {
    module?: string | null;
    class_name?: string | null;
    function?: string | null;
    command?: string[];
};
export type AgentDslSkill = Required<Pick<SkillDeclaration, "name" | "input_schema" | "output_schema">> & {
    description: string;
    handler: string;
    tags: string[];
    scopes: string[];
    stream: boolean;
    policy: Required<SkillPolicy>;
};
export type AgentDsl = {
    schema_version: "2026-06-04";
    language: AgentDslLanguage;
    name: string;
    description: string;
    version: string;
    entrypoint: AgentDslEntrypoint;
    skills: AgentDslSkill[];
    capabilities: Record<string, unknown>;
    input_modes: string[];
    output_modes: string[];
    required_secrets: string[];
    required_env: string[];
    consumer_setup: ConsumerSetupWire;
    runtime: AgentRuntime;
    template_lineage?: unknown | null;
    meta_agent_manifest?: unknown | null;
    state_schema?: JsonSchema | null;
    workspace_access: Record<string, unknown>;
    config_schema?: JsonSchema | null;
    auth: AgentDslAuth;
    metadata: Record<string, unknown>;
};
export type AgentClass<T extends A2AAgent = A2AAgent> = {
    new (config?: Record<string, unknown>): T;
    agent: AgentIdentity;
    auth: AgentDslAuth;
    skills: SkillDeclaration[];
    capabilities: Record<string, unknown>;
    inputModes: string[];
    outputModes: string[];
    requiredSecrets: string[];
    requiredEnv: string[];
    consumerSetup: ConsumerSetup | ConsumerSetupWire;
    runtime: AgentRuntime;
    workspaceAccess: Record<string, unknown>;
    configSchema: JsonSchema | null;
    stateSchema: JsonSchema | null;
    templateLineage: unknown | null;
    metaAgentManifest: unknown | null;
};
export declare abstract class A2AAgent {
    static agent: AgentIdentity;
    static auth: AgentDslAuth;
    static skills: SkillDeclaration[];
    static capabilities: Record<string, unknown>;
    static inputModes: string[];
    static outputModes: string[];
    static requiredSecrets: string[];
    static requiredEnv: string[];
    static consumerSetup: ConsumerSetup | ConsumerSetupWire;
    static runtime: AgentRuntime;
    static workspaceAccess: Record<string, unknown>;
    static configSchema: JsonSchema | null;
    static stateSchema: JsonSchema | null;
    static templateLineage: unknown | null;
    static metaAgentManifest: unknown | null;
    readonly config: Record<string, unknown>;
    constructor(config?: Record<string, unknown>);
    startup(_ctx: RunContext): Promise<void>;
    shutdown(_ctx: RunContext): Promise<void>;
    health(): Promise<boolean>;
    dispatch(request: WorkerRequest): Promise<WorkerResponse>;
    localInvoke(skillName: string, args?: Record<string, unknown>, options?: {
        auth?: Record<string, unknown> | null;
        task_id?: string;
        taskId?: string;
        grant?: string | null;
        cp_url?: string | null;
        cpUrl?: string | null;
        cp_jwt?: string | null;
        cpJwt?: string | null;
        llm_creds?: Record<string, unknown> | null;
        llmCreds?: Record<string, unknown> | null;
        consumer_config?: Record<string, unknown> | null;
        consumerConfig?: Record<string, unknown> | null;
        consumer_secrets?: Record<string, string> | null;
        consumerSecrets?: Record<string, string> | null;
        caller?: Record<string, unknown> | null;
        grant_ids?: string[] | null;
        grantIds?: string[] | null;
        random_seed?: number | string | null;
        randomSeed?: number | string | null;
        emit?: (event: Record<string, unknown>) => void | Promise<void>;
        a2a?: A2AClient;
        a2a_client?: A2AClient;
        workspace?: WorkspaceClientLike;
        workspace_client?: WorkspaceClientLike;
        sandbox?: HttpSandboxClient;
        sandbox_client?: HttpSandboxClient;
        discover?: DiscoveryClient;
        discovery_client?: DiscoveryClient;
        secrets?: Record<string, string>;
    }): Promise<unknown>;
    local_invoke(skillName: string, args?: Record<string, unknown>, options?: Parameters<A2AAgent["localInvoke"]>[2]): Promise<unknown>;
}
export declare class ConsumerSetupField {
    readonly name: string;
    readonly kind: ConsumerSetupKind;
    readonly label: string | null;
    readonly description: string;
    readonly required: boolean;
    readonly input_type: ConsumerSetupInputType;
    readonly options: string[];
    constructor(options: {
        name: string;
        kind?: ConsumerSetupKind;
        label?: string | null;
        description?: string;
        required?: boolean;
        input_type?: ConsumerSetupInputType;
        options?: string[];
    });
    static config(name: string, options?: {
        label?: string | null;
        description?: string;
        required?: boolean;
        input_type?: ConsumerSetupInputType;
        options?: string[];
    }): ConsumerSetupField;
    static secret(name: string, options?: {
        label?: string | null;
        description?: string;
        required?: boolean;
        input_type?: ConsumerSetupInputType;
    }): ConsumerSetupField;
    toJSON(): ConsumerSetupFieldWire;
}
export declare class ConsumerSetup {
    readonly fields: ConsumerSetupField[];
    constructor(fields?: (ConsumerSetupField | ConsumerSetupFieldWire)[]);
    static none(): ConsumerSetup;
    static fromFields(...fields: (ConsumerSetupField | ConsumerSetupFieldWire)[]): ConsumerSetup;
    static from_fields(...fields: (ConsumerSetupField | ConsumerSetupFieldWire)[]): ConsumerSetup;
    get requiredNames(): string[];
    get required_names(): string[];
    toJSON(): ConsumerSetupWire;
}
export type WorkerRequest = {
    agent: string;
    skill: string;
    handler: string;
    arguments: Record<string, unknown>;
    task_id?: string | null;
    caller?: Record<string, unknown> | null;
    grant_ids?: string[] | null;
    random_seed?: number | string | null;
    grant?: string | null;
    llm_creds?: Record<string, unknown> | null;
    composition?: Record<string, unknown> | null;
    consumer_config?: Record<string, unknown> | null;
    consumer_secrets?: Record<string, string> | null;
    cp_jwt?: string | null;
    cp_url?: string | null;
    auth?: Record<string, unknown> | null;
    scope_expansion_allowed?: boolean | null;
    emit?: (event: Record<string, unknown>) => void | Promise<void>;
    context?: RunContext;
    cancelled?: boolean;
    a2a_client?: A2AClient;
    workspace_client?: WorkspaceClientLike;
    sandbox_client?: HttpSandboxClient;
    discovery_client?: DiscoveryClient;
    secrets?: Record<string, string>;
};
export type WorkerResponse = {
    result: unknown;
    events?: Record<string, unknown>[];
    artifacts?: Record<string, unknown>[];
};
export type Handler = (request: WorkerRequest) => WorkerResponse | Promise<WorkerResponse>;
export type ContextHandler = (context: RunContext, input: Record<string, unknown>) => unknown | Promise<unknown>;
export type AgentEvent = {
    kind: string;
    payload?: Record<string, unknown>;
};
export type ExecutionReceipt = {
    receipt_id: string;
    schema_version: number;
    agent_name: string;
    agent_version: string;
    caller: string;
    task_id: string;
    skill_name: string;
    input_hash: string;
    input_preview: string;
    grant_ids: string[];
    file_ops: Record<string, unknown>;
    tool_calls: Record<string, unknown>[];
    artifacts: Record<string, unknown>[];
    handoffs: Record<string, unknown>[];
    status: string;
    error_type: string;
    result_preview: string;
    eval_score: number | null;
    reviewer: string;
    started_at: number;
    ended_at: number;
    elapsed_ms: number;
    nonce: string;
};
export type ReplayEvent = {
    idx: number;
    kind: string;
    ts_ms: number;
    payload: Record<string, unknown>;
};
export type ReplaySession = {
    session_id: string;
    schema_version: number;
    agent_name: string;
    agent_version: string;
    caller: string;
    task_id: string;
    skill_name: string;
    random_seed: string;
    input_hash: string;
    started_at: number;
    ended_at: number;
    events: ReplayEvent[];
    receipt_id: string;
    nonce: string;
};
export type ArtifactRef = {
    name?: string;
    path?: string;
    uri?: string;
    mime_type?: string;
    bytes?: number;
    content_type?: string;
    metadata?: Record<string, unknown>;
};
export type ArtifactData = string | Uint8Array | Buffer;
export type LLMCreds = {
    base_url: string;
    api_key: string;
    model: string;
    source: "caller" | "agent_byok" | "platform" | string;
    temperature_mode: string;
    temperature: number | null;
    extra_body: Record<string, unknown>;
    metadata: Record<string, unknown>;
};
export type LLMCredsAccessor = (() => LLMCreds) & LLMCreds;
export type WorkspaceMode = "read_only" | "read_write_overlay" | "read_write_direct";
export type WorkspaceGrantPayload = {
    grant_id?: string;
    issuer?: string;
    audience?: string;
    bucket?: string;
    mode?: WorkspaceMode;
    allow_patterns?: string[];
    deny_patterns?: string[];
    outputs_prefix?: string | null;
    write_prefixes?: string[];
    llm_models?: string[];
    llm_max_budget_usd?: number | null;
    llm_rpm_limit?: number | null;
    llm_tpm_limit?: number | null;
    source_grants?: Record<string, unknown>[];
    parent_grant_id?: string | null;
    delegation_depth?: number;
    max_delegation_depth?: number;
    expires_at?: number;
    issued_at?: number;
    nonce?: string;
};
export type CallResult = {
    result: unknown;
    events: Record<string, unknown>[];
    artifacts: Record<string, unknown>[];
    grant_id: string | null;
};
export type A2ACallOptions = {
    args?: Record<string, unknown>;
    grant?: string | null;
    cp_jwt?: string | null;
    cpJwt?: string | null;
    cp_url?: string | null;
    cpUrl?: string | null;
    llm_creds?: Record<string, unknown> | null;
    llmCreds?: Record<string, unknown> | null;
    consumer_config?: Record<string, unknown> | null;
    consumerConfig?: Record<string, unknown> | null;
    consumer_secrets?: Record<string, string> | null;
    consumerSecrets?: Record<string, string> | null;
    timeout?: number | null;
    timeoutSeconds?: number | null;
    composition?: Record<string, unknown> | null;
};
export type WorkspaceFileInfo = {
    path: string;
    size_bytes?: number;
    file_type?: string;
};
export type WorkspaceSearchOptions = {
    limit?: number;
    prefix?: string;
    fileType?: string;
    file_type?: string;
    regex?: boolean;
};
export type WorkspaceView = {
    path: string;
    content: string;
    truncated: boolean;
    bytes: number;
};
export type WorkspaceClientLike = {
    exists?: (path: string) => Promise<boolean> | boolean;
    read?: (path: string) => Promise<Uint8Array | Buffer | string> | Uint8Array | Buffer | string;
    readBytes?: (path: string) => Promise<Uint8Array | Buffer> | Uint8Array | Buffer;
    read_bytes?: (path: string) => Promise<Uint8Array | Buffer> | Uint8Array | Buffer;
    readText?: (path: string) => Promise<string> | string;
    read_text?: (path: string) => Promise<string> | string;
    write?: (path: string, data: Uint8Array | Buffer | string) => Promise<void> | void;
    writeBytes?: (path: string, data: Uint8Array | Buffer | string, contentType?: string) => Promise<void> | void;
    write_bytes?: (path: string, data: Uint8Array | Buffer | string, contentType?: string) => Promise<void> | void;
    writeText?: (path: string, data: string, contentType?: string) => Promise<void> | void;
    write_text?: (path: string, data: string, contentType?: string) => Promise<void> | void;
    list?: () => Promise<WorkspaceFileInfo[]> | WorkspaceFileInfo[];
    iterPaths?: () => Promise<string[]> | string[];
    iter_paths?: () => Promise<string[]> | string[];
    search?: (query: string, options?: WorkspaceSearchOptions) => Promise<WorkspaceFileInfo[]> | WorkspaceFileInfo[];
    view?: (path: string, options?: {
        maxBytes?: number;
        max_bytes?: number;
    }) => Promise<WorkspaceView> | WorkspaceView;
    delegate?: (options: {
        audience: string;
        allow_patterns?: string[];
        allowPatterns?: string[];
        deny_patterns?: string[];
        denyPatterns?: string[];
        mode?: WorkspaceMode;
        outputs_prefix?: string | null;
        outputsPrefix?: string | null;
        write_prefixes?: string[];
        writePrefixes?: string[];
        ttl_seconds?: number;
        ttlSeconds?: number;
    }) => Promise<string> | string;
    bucket?: string | null;
    mode?: WorkspaceMode | null;
    outputsPrefix?: string | null;
    outputs_prefix?: string | null;
    writePrefixes?: string[];
    write_prefixes?: string[];
};
export type MemoryRecord = {
    key: string;
    namespace: string;
    value: unknown;
    metadata: Record<string, unknown>;
    created_at: string;
    updated_at: string;
};
export type MemoryLogEntry = {
    log: string;
    value: unknown;
    metadata: Record<string, unknown>;
    created_at: string;
};
export type MetaRunPlanNode = Record<string, unknown> & {
    id: string;
    agent: string;
    skill: string;
};
export type MetaRunPlan = Record<string, unknown>;
export type MetaAgentRunRecord = Record<string, unknown>;
export type ProtocolScenarioRecord = Record<string, unknown>;
export type ProtocolRegistryRecord = Record<string, unknown>;
export type ProtocolRuntimeReadinessRecord = Record<string, unknown>;
export type ProtocolSimulationRecord = Record<string, unknown>;
export type DiscoveredAgent = {
    name: string;
    url?: string | null;
    card: Record<string, unknown>;
};
export type WorkspaceBackend = {
    workspace: WorkspaceClient;
    sandbox: HttpSandboxClient;
    artifacts_root: string;
    run_shell: (script: string, options?: Record<string, unknown>) => Promise<ExecResult>;
    run_python: (code: string, options?: Record<string, unknown>) => Promise<ExecResult>;
    tools: () => Record<string, unknown>[];
};
export type ExecResult = {
    stdout: string;
    stderr: string;
    exit_code: number;
    truncated?: boolean;
    files?: Record<string, unknown>[];
};
export type SandboxSpec = {
    name: string;
    image?: string;
    memory_mib?: number;
    cpus?: number;
    workspace?: string | null;
    secrets?: string[];
    egress?: string[];
    labels?: Record<string, string>;
};
export type ScopeRequest = {
    read?: string | string[];
    write?: string | string[];
    readPrefixes?: string[];
    writePrefixes?: string[];
    read_prefixes?: string[];
    write_prefixes?: string[];
    reason?: string;
    mode?: WorkspaceMode;
    ttlSeconds?: number;
    ttl_seconds?: number;
    approvalTimeoutSeconds?: number;
    approval_timeout_seconds?: number;
};
export type ScopeGrantResolution = {
    grant?: string;
    grant_token?: string;
    token?: string;
    payload?: WorkspaceGrantPayload;
};
export declare class WorkspaceDenied extends Error {
    constructor(message: string);
}
export declare class ConsumerSetupMissing extends Error {
    constructor(message: string);
}
export declare class CancelledByCaller extends Error {
    constructor(message?: string);
}
export declare class SandboxUnavailable extends Error {
    constructor(message?: string);
}
export declare class SandboxRuntimeError extends Error {
    readonly statusCode?: number;
    readonly responseBody?: string;
    readonly operation?: string;
    readonly method?: string;
    readonly url?: string;
    readonly timeoutSeconds?: number;
    readonly causeType?: string;
    readonly causeMessage?: string;
    constructor(message: string, options?: {
        statusCode?: number;
        responseBody?: string;
        operation?: string;
        method?: string;
        url?: string;
        timeoutSeconds?: number;
        causeType?: string;
        causeMessage?: string;
    });
    toErrorPayload(): Record<string, unknown>;
}
export declare class SkillNotFound extends Error {
    constructor(skill: string);
}
export declare class SkillInputError extends Error {
    constructor(message: string);
}
export declare class SkillOutputError extends Error {
    constructor(message: string);
}
export declare class ReceiptInvalid extends Error {
    constructor(message: string);
}
export declare class ReplayInvalid extends Error {
    constructor(message: string);
}
export declare class GrantInvalid extends Error {
    constructor(message: string);
}
export declare class GrantDelegationDenied extends Error {
    constructor(message: string);
}
export declare const EVENT_KINDS: readonly ["skill_start", "llm_call", "llm_response", "tool_call", "tool_response", "workspace_read", "workspace_write", "scope_request", "scope_approve", "handoff_start", "handoff_end", "artifact_write", "eval", "error", "skill_end"];
export declare class EventRecorder {
    readonly agent_name: string;
    readonly agent_version: string;
    readonly skill_name: string;
    readonly caller: string;
    readonly task_id: string;
    readonly random_seed: string;
    readonly input_hash: string;
    readonly started_at: number;
    private readonly items;
    constructor(options: {
        agent_name: string;
        skill_name: string;
        agent_version?: string;
        caller?: string;
        task_id?: string;
        random_seed?: string;
        input_hash?: string;
        started_at?: number;
    });
    record(kind: string, payload?: Record<string, unknown>): ReplayEvent;
    get events(): ReplayEvent[];
    buildSession(options?: {
        session_id?: string;
        receipt_id?: string;
    }): ReplaySession;
    build_session(options?: {
        session_id?: string;
        receipt_id?: string;
    }): ReplaySession;
}
export declare function hashInput(payload: unknown): string;
export declare const hash_input: typeof hashInput;
export declare function sealReceipt(options: {
    agent_name: string;
    skill_name: string;
    started_at: number;
    ended_at?: number;
    agent_version?: string;
    caller?: string;
    task_id?: string;
    inputs?: unknown;
    result?: unknown;
    status?: string;
    error_type?: string;
    grant_ids?: string[];
    artifacts?: Record<string, unknown>[];
}): [ExecutionReceipt, string];
export declare const seal_receipt: typeof sealReceipt;
export declare function signReceipt(receipt: ExecutionReceipt): string;
export declare const sign_receipt: typeof signReceipt;
export declare function verifyReceipt(token: string): ExecutionReceipt;
export declare const verify_receipt: typeof verifyReceipt;
export declare function sealReplaySession(session: ReplaySession): [ReplaySession, string];
export declare const seal_replay_session: typeof sealReplaySession;
export declare function signReplaySession(session: ReplaySession): string;
export declare const sign_replay_session: typeof signReplaySession;
export declare function verifyReplaySession(token: string): ReplaySession;
export declare const verify_replay_session: typeof verifyReplaySession;
export declare function iterEvents(session: ReplaySession): ReplayEvent[];
export declare const iter_events: typeof iterEvents;
export declare function filterEvents(session: ReplaySession, kind: string | string[]): ReplayEvent[];
export declare const filter_events: typeof filterEvents;
export declare function mintGrant(options: {
    issuer: string;
    audience: string;
    bucket: string;
    mode?: WorkspaceMode;
    allow_patterns?: string[];
    deny_patterns?: string[];
    outputs_prefix?: string | null;
    write_prefixes?: string[];
    ttl_seconds?: number;
    parent_grant_id?: string | null;
    delegation_depth?: number;
    max_delegation_depth?: number;
    llm_models?: string[];
    llm_max_budget_usd?: number | null;
    llm_rpm_limit?: number | null;
    llm_tpm_limit?: number | null;
    source_grants?: Record<string, unknown>[];
}): [WorkspaceGrantPayload, string];
export declare const mint_grant: typeof mintGrant;
export declare function signGrant(grant: WorkspaceGrantPayload): string;
export declare const sign_grant: typeof signGrant;
export declare function verifyGrant(token: string): WorkspaceGrantPayload;
export declare const verify_grant: typeof verifyGrant;
export declare function delegateGrant(parent: WorkspaceGrantPayload, options: {
    issuer: string;
    audience: string;
    bucket?: string;
    mode?: WorkspaceMode;
    allow_patterns?: string[];
    deny_patterns?: string[];
    outputs_prefix?: string | null;
    write_prefixes?: string[];
    ttl_seconds?: number;
}): [WorkspaceGrantPayload, string];
export declare const delegate_grant: typeof delegateGrant;
export declare abstract class A2AClient {
    abstract call(target: string, skill: string, options?: A2ACallOptions): Promise<CallResult>;
}
export declare class InMemoryA2AClient extends A2AClient {
    readonly agents: Record<string, A2AAgent>;
    readonly ctxFactory?: (agent: A2AAgent, grant: string | null) => Partial<WorkerRequest> | null | undefined;
    constructor(options: {
        agents: Record<string, A2AAgent>;
        ctxFactory?: (agent: A2AAgent, grant: string | null) => Partial<WorkerRequest> | null | undefined;
        ctx_factory?: (agent: A2AAgent, grant: string | null) => Partial<WorkerRequest> | null | undefined;
    });
    call(target: string, skillName: string, options?: A2ACallOptions): Promise<CallResult>;
}
export declare class HttpA2AClient extends A2AClient {
    readonly defaultTimeout: number;
    readonly default_timeout: number;
    readonly discovery?: {
        getAgent?: (name: string) => Promise<DiscoveredAgent>;
        get_agent?: (name: string) => Promise<DiscoveredAgent>;
    } | null;
    private readonly resolvedUrls;
    constructor(options?: {
        defaultTimeout?: number;
        default_timeout?: number;
        discovery?: {
            getAgent?: (name: string) => Promise<DiscoveredAgent>;
            get_agent?: (name: string) => Promise<DiscoveredAgent>;
        } | null;
    });
    call(target: string, skillName: string, options?: A2ACallOptions): Promise<CallResult>;
    private resolveTarget;
}
export declare class HttpSandboxHandle {
    readonly baseUrl: string;
    readonly name: string;
    readonly timeoutSeconds: number;
    readonly authToken?: string;
    readonly grantToken?: string;
    constructor(options: {
        baseUrl: string;
        name: string;
        timeoutSeconds?: number;
        authToken?: string | null;
        grantToken?: string | null;
    });
    exec(cmd: string, args?: string[], options?: {
        timeout?: number;
    }): Promise<ExecResult>;
    shell(script: string, options?: {
        timeout?: number;
    }): Promise<ExecResult>;
    stop(): Promise<void>;
    kill(): Promise<void>;
    logs(_options?: {
        tail?: number;
    }): Promise<string>;
    private postExec;
}
export declare class HttpSandboxClient {
    readonly baseUrl: string;
    readonly defaultWorkspace?: string;
    readonly timeoutSeconds: number;
    readonly authToken?: string;
    readonly grantToken?: string;
    constructor(baseUrl: string, options?: {
        defaultWorkspace?: string | null;
        timeoutSeconds?: number;
        authToken?: string | null;
        grantToken?: string | null;
    });
    create(spec: SandboxSpec): Promise<HttpSandboxHandle>;
    get(name: string): Promise<HttpSandboxHandle>;
    list(): Promise<string[]>;
    remove(name: string): Promise<void>;
    runPython(code: string, options?: Record<string, unknown>): Promise<ExecResult>;
    run_python(code: string, options?: Record<string, unknown>): Promise<ExecResult>;
    runShell(script: string, options?: Record<string, unknown>): Promise<ExecResult>;
    run_shell(script: string, options?: Record<string, unknown>): Promise<ExecResult>;
    private runOneShot;
}
export declare class WorkspaceClient {
    readonly grantToken: string;
    readonly grant: WorkspaceGrantPayload | null;
    private readonly baseUrl;
    private readonly scopeRequester?;
    constructor(options: {
        cpUrl: string;
        grantToken: string;
        grant?: WorkspaceGrantPayload | null;
        requestScope?: (options: ScopeRequest) => Promise<WorkspaceGrantPayload | null>;
    });
    get outputsPrefix(): string | null;
    get outputs_prefix(): string | null;
    get bucket(): string | null;
    get mode(): WorkspaceMode | null;
    get allowPatterns(): string[];
    get allow_patterns(): string[];
    get denyPatterns(): string[];
    get deny_patterns(): string[];
    get writePrefixes(): string[];
    get write_prefixes(): string[];
    get currentGrantId(): string | null;
    get current_grant_id(): string | null;
    get currentGrant(): WorkspaceGrantPayload | null;
    get current_grant(): WorkspaceGrantPayload | null;
    isWritableOutput(path: string): boolean;
    is_writable_output(path: string): boolean;
    delegate(options: {
        audience: string;
        allow_patterns?: string[];
        allowPatterns?: string[];
        deny_patterns?: string[];
        denyPatterns?: string[];
        mode?: WorkspaceMode;
        outputs_prefix?: string | null;
        outputsPrefix?: string | null;
        write_prefixes?: string[];
        writePrefixes?: string[];
        ttl_seconds?: number;
        ttlSeconds?: number;
    }): Promise<string>;
    exists(path: string): Promise<boolean>;
    readBytes(path: string): Promise<Uint8Array>;
    read_bytes(path: string): Promise<Uint8Array>;
    readText(path: string): Promise<string>;
    read_text(path: string): Promise<string>;
    writeBytes(path: string, content: Uint8Array | Buffer, contentType?: string): Promise<void>;
    write_bytes(path: string, content: Uint8Array | Buffer, contentType?: string): Promise<void>;
    writeText(path: string, content: string, contentType?: string): Promise<void>;
    write_text(path: string, content: string, contentType?: string): Promise<void>;
    delete(path: string): Promise<void>;
    delete_path(path: string): Promise<void>;
    list(): Promise<WorkspaceFileInfo[]>;
    iterPaths(): Promise<string[]>;
    iter_paths(): Promise<string[]>;
    search(query: string, options?: WorkspaceSearchOptions): Promise<WorkspaceFileInfo[]>;
    view(path: string, options?: {
        maxBytes?: number;
        max_bytes?: number;
    }): Promise<WorkspaceView>;
    requestAccess(options: ScopeRequest): Promise<WorkspaceGrantPayload | null>;
    request_access(options: ScopeRequest): Promise<WorkspaceGrantPayload | null>;
    private headers;
    private url;
}
export declare class LocalWorkspaceClient implements WorkspaceClientLike {
    private readonly files;
    readonly bucket: string;
    readonly issuer: string;
    mode: WorkspaceMode | null;
    allowPatterns: string[];
    allow_patterns: string[];
    denyPatterns: string[];
    deny_patterns: string[];
    outputsPrefix: string | null;
    outputs_prefix: string | null;
    writePrefixes: string[];
    write_prefixes: string[];
    currentGrantId: string | null;
    current_grant_id: string | null;
    currentGrant: WorkspaceGrantPayload | null;
    current_grant: WorkspaceGrantPayload | null;
    constructor(files?: Record<string, string | Uint8Array | Buffer>, options?: {
        bucket?: string;
        issuer?: string;
        mode?: WorkspaceMode | null;
        allow_patterns?: string[];
        allowPatterns?: string[];
        deny_patterns?: string[];
        denyPatterns?: string[];
        outputs_prefix?: string | null;
        outputsPrefix?: string | null;
        write_prefixes?: string[];
        writePrefixes?: string[];
    });
    installGrant(grant: WorkspaceGrantPayload): void;
    install_grant(grant: WorkspaceGrantPayload): void;
    isWritableOutput(path: string): boolean;
    is_writable_output(path: string): boolean;
    exists(path: string): boolean;
    read(path: string): Uint8Array;
    readBytes(path: string): Uint8Array;
    read_bytes(path: string): Uint8Array;
    readText(path: string): string;
    read_text(path: string): string;
    write(path: string, data: Uint8Array | Buffer | string): void;
    writeBytes(path: string, data: Uint8Array | Buffer | string): void;
    write_bytes(path: string, data: Uint8Array | Buffer | string): void;
    writeText(path: string, data: string): void;
    write_text(path: string, data: string): void;
    delete(path: string): void;
    delete_path(path: string): void;
    list(): WorkspaceFileInfo[];
    iterPaths(): string[];
    iter_paths(): string[];
    search(query: string, options?: WorkspaceSearchOptions): WorkspaceFileInfo[];
    view(path: string, options?: {
        maxBytes?: number;
        max_bytes?: number;
    }): WorkspaceView;
    delegate(options: Parameters<WorkspaceClient["delegate"]>[0]): Promise<string>;
    private isDenied;
}
export declare const FileSystemWorkspaceClient: typeof LocalWorkspaceClient;
export declare class MemoryClient {
    private readonly ctx;
    private readonly root;
    readonly agentNamespace: string;
    readonly agent_namespace: string;
    readonly userNamespace: string;
    readonly user_namespace: string;
    constructor(ctx: RunContext, options?: {
        rootPrefix?: string;
        root_prefix?: string;
        agentNamespace?: string;
        agent_namespace?: string;
        userNamespace?: string;
        user_namespace?: string;
    });
    get rootPrefix(): string;
    get root_prefix(): string;
    forScope(options?: {
        agentNamespace?: string;
        agent_namespace?: string;
        userNamespace?: string;
        user_namespace?: string;
    }): MemoryClient;
    for_scope(options?: {
        agentNamespace?: string;
        agent_namespace?: string;
        userNamespace?: string;
        user_namespace?: string;
    }): MemoryClient;
    putNote(key: string, value: unknown, options?: {
        namespace?: string;
        metadata?: Record<string, unknown>;
    }): Promise<MemoryRecord>;
    put_note(key: string, value: unknown, options?: {
        namespace?: string;
        metadata?: Record<string, unknown>;
    }): Promise<MemoryRecord>;
    remember(key: string, value: unknown, options?: {
        namespace?: string;
        metadata?: Record<string, unknown>;
    }): Promise<MemoryRecord>;
    getNote(key: string, options?: {
        namespace?: string;
    }): Promise<MemoryRecord | null>;
    get_note(key: string, options?: {
        namespace?: string;
    }): Promise<MemoryRecord | null>;
    recall(key: string, options?: {
        namespace?: string;
    }): Promise<MemoryRecord | null>;
    listNotes(options?: {
        namespace?: string;
        limit?: number;
    }): Promise<MemoryRecord[]>;
    list_notes(options?: {
        namespace?: string;
        limit?: number;
    }): Promise<MemoryRecord[]>;
    searchNotes(query: string, options?: {
        namespace?: string;
        limit?: number;
    }): Promise<MemoryRecord[]>;
    search_notes(query: string, options?: {
        namespace?: string;
        limit?: number;
    }): Promise<MemoryRecord[]>;
    search(query: string, options?: {
        namespace?: string;
        limit?: number;
    }): Promise<MemoryRecord[]>;
    appendLog(name: string, value: unknown, options?: {
        metadata?: Record<string, unknown>;
    }): Promise<MemoryLogEntry>;
    append_log(name: string, value: unknown, options?: {
        metadata?: Record<string, unknown>;
    }): Promise<MemoryLogEntry>;
    readLog(name: string, options?: {
        limit?: number;
    }): Promise<MemoryLogEntry[]>;
    read_log(name: string, options?: {
        limit?: number;
    }): Promise<MemoryLogEntry[]>;
    private namespacePrefix;
    private notePath;
    private logPath;
}
export declare class MetaAgentRunsClient {
    private readonly cpUrl;
    private readonly cpJwt;
    private readonly agentName;
    constructor(ctx: RunContext, options?: {
        agentName?: string;
        agent_name?: string;
    });
    create(payload: {
        goal: string;
        success_criteria?: string[];
        thread_id?: string | null;
        run_id?: string | null;
        status?: string;
        current_plan?: MetaRunPlan;
        progress?: Record<string, unknown>[];
        state?: Record<string, unknown>;
        summary?: string | null;
    }): Promise<MetaAgentRunRecord>;
    list(options?: {
        thread_id?: string;
        status?: string;
        limit?: number;
    }): Promise<MetaAgentRunRecord[]>;
    get(runId: string): Promise<MetaAgentRunRecord | null>;
    update(runId: string, fields: Record<string, unknown>): Promise<MetaAgentRunRecord>;
    private request;
}
export declare class ProtocolSimulationsClient {
    private readonly cpUrl;
    private readonly cpJwt;
    private readonly agentName;
    constructor(ctx: RunContext, options?: {
        agentName?: string;
        agent_name?: string;
    });
    listScenarios(): Promise<ProtocolScenarioRecord[]>;
    list_scenarios(): Promise<ProtocolScenarioRecord[]>;
    listRegistry(): Promise<ProtocolRegistryRecord[]>;
    list_registry(): Promise<ProtocolRegistryRecord[]>;
    checkRuntimeReadiness(payload?: Record<string, unknown>): Promise<ProtocolRuntimeReadinessRecord>;
    check_runtime_readiness(payload?: Record<string, unknown>): Promise<ProtocolRuntimeReadinessRecord>;
    recordScenarioRun(jobId: string, options?: {
        scenario_ids?: string[];
        scenarioIds?: string[];
        cost_cents?: number;
        costCents?: number;
    }): Promise<ProtocolSimulationRecord>;
    record_scenario_run(jobId: string, options?: {
        scenario_ids?: string[];
        cost_cents?: number;
    }): Promise<ProtocolSimulationRecord>;
    private request;
}
export declare class DiscoveryClient {
    private readonly cpUrl;
    private readonly cpJwt;
    constructor(ctx: RunContext, options?: {
        cpUrl?: string;
        cp_url?: string;
        cpJwt?: string | null;
        cp_jwt?: string | null;
    });
    findAgents(options?: {
        tags?: string[];
        capability?: string;
        skill?: string;
        limit?: number;
    }): Promise<DiscoveredAgent[]>;
    find_agents(options?: {
        tags?: string[];
        capability?: string;
        skill?: string;
        limit?: number;
    }): Promise<DiscoveredAgent[]>;
    getAgent(name: string): Promise<DiscoveredAgent>;
    get_agent(name: string): Promise<DiscoveredAgent>;
    private request;
}
export declare class SubAgentToolkit {
    private readonly ctx;
    constructor(ctx: RunContext);
    listSubagents(options?: {
        tags?: string[];
        capability?: string;
        skill?: string;
        limit?: number;
    }): Promise<Record<string, unknown>[]>;
    list_subagents(options?: {
        tags?: string[];
        capability?: string;
        skill?: string;
        limit?: number;
    }): Promise<Record<string, unknown>[]>;
    getSubagent(name: string): Promise<Record<string, unknown>>;
    get_subagent(name: string): Promise<Record<string, unknown>>;
    callSubagent(name: string, skill: string, options?: {
        args?: Record<string, unknown>;
        grant?: string | null;
        timeout?: number;
    }): Promise<Record<string, unknown>>;
    call_subagent(name: string, skill: string, options?: {
        args?: Record<string, unknown>;
        grant?: string | null;
        timeout?: number;
    }): Promise<Record<string, unknown>>;
}
export declare class MissingScopes extends Error {
    missing: string[];
    constructor(missing: string[]);
}
export declare class RunContext {
    readonly request: WorkerRequest;
    readonly agent: string;
    readonly skill: string;
    readonly handler: string;
    readonly arguments: Record<string, unknown>;
    readonly auth: Record<string, unknown> | null;
    readonly caller: Record<string, unknown> | null;
    readonly grantIds: string[];
    readonly grant_ids: string[];
    readonly randomSeed: number | string | null;
    readonly random_seed: number | string | null;
    readonly llm: LLMCredsAccessor;
    readonly events: Record<string, unknown>[];
    readonly artifacts: Record<string, unknown>[];
    private workspaceClient?;
    private sandboxClient?;
    private randomState;
    constructor(request: WorkerRequest);
    get emittedEvents(): Record<string, unknown>[];
    get emittedArtifacts(): Record<string, unknown>[];
    get taskId(): string;
    get task_id(): string;
    get grant(): WorkspaceGrantPayload | null;
    get workspace(): WorkspaceClient;
    get sandbox(): HttpSandboxClient;
    get memory(): MemoryClient;
    get metaRuns(): MetaAgentRunsClient;
    get meta_runs(): MetaAgentRunsClient;
    get protocolSimulations(): ProtocolSimulationsClient;
    get protocol_simulations(): ProtocolSimulationsClient;
    get discover(): DiscoveryClient;
    get subagents(): SubAgentToolkit;
    workspaceBackend(_options?: {
        image?: string;
    }): WorkspaceBackend;
    workspace_backend(options?: {
        image?: string;
    }): WorkspaceBackend;
    deepagentsBackend(options?: {
        image?: string;
    }): WorkspaceBackend;
    deepagents_backend(options?: {
        image?: string;
    }): WorkspaceBackend;
    random(): number;
    emitEvent(event: AgentEvent | Record<string, unknown>): Promise<void>;
    emit_event(event: AgentEvent | Record<string, unknown>): Promise<void>;
    emitProgress(message: string, payload?: Record<string, unknown>): Promise<void>;
    emit_progress(message: string, payload?: Record<string, unknown>): Promise<void>;
    emitTextDelta(text: string): Promise<void>;
    emit_text_delta(text: string): Promise<void>;
    emitError(message: string, options?: {
        code?: string;
    }): Promise<void>;
    emit_error(message: string, options?: {
        code?: string;
    }): Promise<void>;
    emitArtifact(ref: ArtifactRef): Promise<void>;
    emit_artifact(ref: ArtifactRef): Promise<void>;
    writeArtifact(name: string, data: ArtifactData, contentType?: string, metadata?: Record<string, unknown>): Promise<ArtifactRef>;
    write_artifact(name: string, data: ArtifactData, contentType?: string, metadata?: Record<string, unknown>): Promise<ArtifactRef>;
    secret(name: string): string;
    consumerConfig<T = unknown>(name: string, defaultValue?: T): T;
    consumer_config<T = unknown>(name: string, defaultValue?: T): T;
    consumerSecret(name: string): string;
    consumer_secret(name: string): string;
    cpJwt(): string | null;
    cp_jwt(): string | null;
    cpUrl(): string | null;
    cp_url(): string | null;
    requireScopes(required: string[]): void;
    require_scopes(required: string[]): void;
    checkCancelled(): Promise<void>;
    check_cancelled(): Promise<void>;
    ask<T = unknown>(prompt: string, options?: {
        timeoutSeconds?: number;
        timeout_seconds?: number;
        metadata?: Record<string, unknown>;
    }): Promise<T>;
    collect<T = unknown>(schema: JsonSchema, options?: {
        title?: string;
        reason?: string;
        uiSchema?: JsonSchema;
        ui_schema?: JsonSchema;
        timeoutSeconds?: number;
        timeout_seconds?: number;
    }): Promise<T>;
    requestScope(options: ScopeRequest): Promise<WorkspaceGrantPayload | null>;
    request_scope(options: ScopeRequest): Promise<WorkspaceGrantPayload | null>;
    ensureRead(path: string, reason?: string): Promise<void>;
    ensure_read(path: string, reason?: string): Promise<void>;
    ensureWrite(path: string, reason?: string): Promise<void>;
    ensure_write(path: string, reason?: string): Promise<void>;
    ensureWorkspace(options: ScopeRequest): Promise<WorkspaceClient>;
    ensure_workspace(options: ScopeRequest): Promise<WorkspaceClient>;
    workspaceShell(script: string, options?: Record<string, unknown>): Promise<ExecResult>;
    workspace_shell(script: string, options?: Record<string, unknown>): Promise<ExecResult>;
    workspacePython(code: string, options?: Record<string, unknown>): Promise<ExecResult>;
    workspace_python(code: string, options?: Record<string, unknown>): Promise<ExecResult>;
    call(target: string, skill: string, args?: Record<string, unknown>, options?: {
        grant?: string | null;
        timeoutSeconds?: number;
        timeout_seconds?: number;
        llmBudgetUsd?: number;
        llm_budget_usd?: number;
        consumer_config?: Record<string, unknown> | null;
        consumerConfig?: Record<string, unknown> | null;
        consumer_secrets?: Record<string, string> | null;
        consumerSecrets?: Record<string, string> | null;
    }): Promise<CallResult>;
    mintGiteaToken(options?: Record<string, unknown>): Promise<Record<string, unknown>>;
    mint_gitea_token(options?: Record<string, unknown>): Promise<Record<string, unknown>>;
    releaseGiteaToken(tokenId: string): Promise<void>;
    release_gitea_token(tokenId: string): Promise<void>;
    finalizeResponse(response: WorkerResponse): WorkerResponse;
    static answer(questionId: string, answer: unknown): boolean;
    static submitInput(requestId: string, value: unknown): boolean;
    static submit_input(requestId: string, value: unknown): boolean;
    static resolveScopeGrant(requestId: string, resolution: ScopeGrantResolution): boolean;
    static resolve_scope_grant(requestId: string, resolution: ScopeGrantResolution): boolean;
    static denyScope(requestId: string, reason?: string): boolean;
    static deny_scope(requestId: string, reason?: string): boolean;
    private artifactPath;
    private resolveLlmCreds;
    private workspaceCoversRead;
    private workspaceCoversWrite;
}
export declare class LocalRunContext extends RunContext {
    readonly artifactBytes: Map<string, Uint8Array<ArrayBufferLike>>;
    private cancelled;
    private readonly onEvent?;
    constructor(options?: {
        agent?: string;
        skill?: string;
        handler?: string;
        args?: Record<string, unknown>;
        arguments?: Record<string, unknown>;
        auth?: Record<string, unknown> | null;
        task_id?: string;
        taskId?: string;
        secrets?: Record<string, string>;
        workspace?: WorkspaceClientLike | null;
        sandbox?: HttpSandboxClient | null;
        a2a?: A2AClient | null;
        discover?: DiscoveryClient | null;
        consumer_config?: Record<string, unknown> | null;
        consumerConfig?: Record<string, unknown> | null;
        consumer_secrets?: Record<string, string> | null;
        consumerSecrets?: Record<string, string> | null;
        caller?: Record<string, unknown> | string | null;
        grant_ids?: string[];
        grantIds?: string[];
        random_seed?: number | string | null;
        randomSeed?: number | string | null;
        on_event?: (event: Record<string, unknown>) => void | Promise<void>;
        onEvent?: (event: Record<string, unknown>) => void | Promise<void>;
    });
    cancel(): void;
    checkCancelled(): Promise<void>;
    emitEvent(event: AgentEvent | Record<string, unknown>): Promise<void>;
    writeArtifact(name: string, data: ArtifactData, contentType?: string, metadata?: Record<string, unknown>): Promise<ArtifactRef>;
}
export declare class ReplayDivergence extends Error {
    readonly idx: number;
    readonly recorded: unknown;
    readonly observed: unknown;
    constructor(idx: number, recorded: unknown, observed: unknown, message?: string);
}
export declare class ReplayLLM {
    readonly session: ReplaySession;
    readonly creds: LLMCreds | null;
    private readonly calls;
    private readonly responses;
    private callIndex;
    private responseIndex;
    constructor(session: ReplaySession, creds?: LLMCreds | null);
    complete(callSignature?: Record<string, unknown>): unknown;
}
export declare class ReplayToolCaller {
    readonly session: ReplaySession;
    private readonly calls;
    private readonly responses;
    private callIndex;
    private responseIndex;
    constructor(session: ReplaySession);
    call(name: string, callSignature?: Record<string, unknown>): unknown;
}
export declare class ReplayWorkspaceClient implements WorkspaceClientLike {
    readonly session: ReplaySession;
    private readonly reads;
    private readonly writes;
    private readIndex;
    private writeIndex;
    constructor(session: ReplaySession);
    read(path: string): Uint8Array;
    readBytes(path: string): Uint8Array;
    read_bytes(path: string): Uint8Array;
    readText(path: string): string;
    read_text(path: string): string;
    write(path: string, _data: Uint8Array | Buffer | string): void;
    writeBytes(path: string, data: Uint8Array | Buffer | string): void;
    write_bytes(path: string, data: Uint8Array | Buffer | string): void;
    writeText(path: string, data: string): void;
    write_text(path: string, data: string): void;
}
export declare function replaySession(agent: A2AAgent, session: ReplaySession): Promise<unknown>;
export declare const replay_session: typeof replaySession;
export declare function publicAuth(): AgentDslAuth;
export declare function apiKeyAuth(options?: {
    resolver?: string | null;
}): AgentDslAuth;
export declare function jwtAuth(options?: {
    resolver?: string | null;
}): AgentDslAuth;
export declare function platformUserAuth(options?: {
    resolver?: string | null;
}): AgentDslAuth;
export declare function customAuth(options: {
    model: string;
    principal_schema: JsonSchema;
    resolver?: string | null;
    required?: boolean;
}): AgentDslAuth;
export declare function skill(declaration: SkillDeclaration): SkillDeclaration;
export declare const tool: typeof skill;
export declare function compileAgent(agentClass: AgentClass, options?: {
    language?: AgentDslLanguage;
    entrypoint?: AgentDslEntrypoint;
    metadata?: Record<string, unknown>;
}): AgentDsl;
export declare function writeAgentDsl(agentClass: AgentClass, path?: string, options?: Parameters<typeof compileAgent>[1]): Promise<AgentDsl>;
export declare function serveWorker(agentClass: AgentClass, handlers: Record<string, Handler>, options?: {
    port?: number;
    host?: string;
}): Server;
export declare function serveAgent(agentClass: AgentClass, options?: {
    port?: number;
    host?: string;
}): Server;
//# sourceMappingURL=index.d.ts.map