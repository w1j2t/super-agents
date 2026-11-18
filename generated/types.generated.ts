// 本文件由 scripts/gen-ts-types.py 自动生成，请勿手工修改。
// 重新生成：python scripts/gen-ts-types.py
// 类型来源：src/**/*.rs 的 pub struct / pub enum（字段名与枚举取值均按源码推导）

// ===== 源: src/api/handlers/auth.rs =====
export interface RefreshInput {
  refresh_token: string;
}

export interface ChangePasswordInput {
  current_password: string;
  new_password: string;
}

export interface RevokedView {
  revoked_sessions: number;
}

// ===== 源: src/api/handlers/catalog.rs =====
export interface AgentView {
  id: string;
  kind: string;
  name: string;
  domain: string;
  summary: string;
  vertical_knowledge: string[];
  doc_refs: string[];
  skills: string[];
  permissions: string[];
  sandbox_profile: string;
  default_model: string;
  task_types: string[];
  max_concurrency: number;
}

export interface CapabilityView {
  id: string;
  name: string;
  category: string;
  trigger: string;
  owner_agents: string[];
  permission_boundary: string;
  observability_signals: string[];
  degradation_or_abort: string;
}

// ===== 源: src/api/handlers/memory.rs =====
export interface UpsertInput {
  scope: string;
  key: string;
  content: string;
  tags: string[];
  confidence: number | null;
}

export interface UpdateInput {
  content: string;
  tags: string[] | null;
  confidence: number | null;
}

export interface MemoryListQuery {
  scope: string | null;
  offset: number | null;
}

export interface MemoryList {
  total: number;
  count: number;
  limit: number;
  offset: number;
  has_more: boolean;
  items: MemoryEntry[];
}

export interface UpsertResult {
  created: boolean;
  entry: MemoryEntry;
}

// ===== 源: src/api/handlers/observability.rs =====
export interface AuditQuery {
  actor: string | null;
  limit: number | null;
  offset: number | null;
}

export interface AuditList {
  total: number;
  count: number;
  limit: number;
  offset: number;
  has_more: boolean;
  denied: number;
  items: AuditEvent[];
}

export interface Overview {
  version: string;
  env: string;
  storage: string;
  storage_durable: boolean;
  uptime_secs: number;
  agents_total: number;
  capabilities_total: number;
  audit_denied: number;
  audit_backend: string;
  runner_backend: string;
  limiter_backend: string;
  limiter_enforcing: boolean;
  metrics_export: string;
  store: Record<string, number>;
}

export interface ComponentList {
  count: number;
  restart_required_for_change: boolean;
  items: ComponentInfo[];
}

export interface AlertList {
  count: number;
  active: Alert[];
  evaluated: EvaluatedNow;
  stats: AlertStats;
  auto_action: AutoAction;
  policy: AlertPolicyView;
  rules: RuleView[];
}

export interface EvaluatedNow {
  fired: number;
  deduped: string[];
  aggregated: number;
}

export interface AlertPolicyView {
  dedup_window_secs: number;
  storm_threshold: number;
  http_5xx_ratio: number;
  min_http_requests: number;
  task_failure_ratio: number;
  min_tasks: number;
  rate_limited_hits: number;
  authz_denied_hits: number;
  circuit_open_hits: number;
}

export interface RuleView {
  id: string;
  title: string;
  severity: string;
  observed_from: string;
}

// ===== 源: src/api/handlers/onboarding.rs =====
export interface StepView {
  key: string;
  order: number;
  title: string;
  summary: string;
  action: string;
  required: boolean;
  est_seconds: number;
  completed: boolean;
  completed_at: string | null;
}

export interface OnboardingView {
  status: string;
  total_required: number;
  completed_required: number;
  skipped: boolean;
  next_step: string | null;
  next_action: string | null;
  steps: StepView[];
}

export interface StepChangeView {
  changed: boolean;
  onboarding: OnboardingView;
}

// ===== 源: src/api/handlers/task.rs =====
export interface CreateTaskInput {
  goal: string;
  task_type: string | null;
  model_profile: string | null;
  budget_tokens: number | null;
}

export interface TaskListQuery {
  limit: number | null;
  offset: number | null;
}

export interface TaskDetail {
  task: TaskView;
  trace: TraceEntry[];
  memory_refs: string[];
}

export interface TaskList {
  total: number;
  count: number;
  limit: number;
  offset: number;
  has_more: boolean;
  items: TaskView[];
}

// ===== 源: src/domain/agent.rs =====
export type AgentKind = 'super' | 'sub';

export interface AgentSpec {
  id: string;
  kind: AgentKind;
  name: string;
  domain: string;
  summary: string;
  vertical_knowledge: string[];
  doc_refs: string[];
  skills: string[];
  permissions: Permission[];
  sandbox_profile: string;
  default_model: ModelProfile;
  task_types: TaskType[];
  keywords: string[];
  max_concurrency: number;
}

// ===== 源: src/domain/capability.rs =====
export interface CapabilitySpec {
  id: string;
  name: string;
  category: string;
  trigger: string;
  owner_agents: string[];
  permission_boundary: string;
  observability_signals: string[];
  degradation_or_abort: string;
}

// ===== 源: src/domain/cluster.rs =====
export interface ClusterNode {
  node_id: string;
  role: string;
  started_at: string;
  uptime_seconds: number;
  step_concurrency: number;
  async_running: number;
  async_capacity: number;
  sse_subscribers: number;
  events_dropped: number;
  agents_total: number;
  tasks: ClusterTasks;
  source: string;
  updated_at: string;
  stale: boolean;
}

export interface ClusterTasks {
  total: number;
  queued: number;
  running: number;
  succeeded: number;
  partially_succeeded: number;
  failed: number;
  cancelled: number;
  aborted_budget: number;
}

export interface ClusterTotals {
  nodes: number;
  offline_nodes: number;
  agents_total: number;
  async_running: number;
  async_capacity: number;
  sse_subscribers: number;
  tasks: ClusterTasks;
}

export interface ClusterView {
  nodes: ClusterNode[];
  totals: ClusterTotals;
}

export interface NodeSnapshot {
  node_id: string;
  role: string;
  started_at: string;
  uptime_seconds: number;
  step_concurrency: number;
  async_running: number;
  async_capacity: number;
  sse_subscribers: number;
  events_dropped: number;
  agents_total: number;
  tasks: ClusterTasks;
  updated_at: string;
}

// ===== 源: src/domain/event.rs =====
export interface TaskEvent {
  seq: number;
  task_id: string;
  status: TaskStatus;
  at: string;
  finished_at: string | null;
  summary: string;
}

// ===== 源: src/domain/memory.rs =====
export type MemoryScope = 'private' | 'shared' | 'system';

export interface MemoryEntry {
  id: string;
  scope: MemoryScope;
  owner_id: string;
  key: string;
  content: string;
  tags: string[];
  version: number;
  written_by: string;
  created_at: string;
  updated_at: string;
  access_count: number;
  confidence: number;
}

export interface MemoryWrite {
  scope: MemoryScope;
  key: string;
  content: string;
  tags: string[];
  confidence: number;
}

// ===== 源: src/domain/onboarding.rs =====
export interface StepDef {
  key: string;
  order: number;
  title: string;
  summary: string;
  action: string;
  required: boolean;
  est_seconds: number;
}

export interface OnboardingProgress {
  user_id: string;
  completed: string[];
  skipped: boolean;
  started_at: string;
  updated_at: string;
  step_completed_at: [string, string][];
}

// ===== 源: src/domain/task.rs =====
export type TaskType = 'coding' | 'writing' | 'research' | 'planning' | 'ideation' | 'collaboration';

export type ModelProfile = 'fast' | 'standard' | 'deep';

export type TaskStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'partially_succeeded' | 'aborted_budget' | 'cancelled';

export interface PlanStep {
  index: number;
  name: string;
  agent_id: string;
  model_profile: ModelProfile;
  permission: Permission;
  est_tokens: number;
  sandbox: boolean;
}

export interface TraceEntry {
  at: string;
  step_index: number;
  agent_id: string;
  phase: string;
  outcome: string;
  detail: string;
  attempt: number;
  tokens: number;
  duration_ms: number | null;
}

export interface StepResult {
  index: number;
  agent_id: string;
  name: string;
  success: boolean;
  model_profile: ModelProfile;
  tokens: number;
  attempts: number;
  output: string;
  error: string | null;
}

export interface TaskRecord {
  id: string;
  owner_id: string;
  goal: string;
  task_type: TaskType;
  requested_model: ModelProfile | null;
  status: TaskStatus;
  plan: PlanStep[];
  results: StepResult[];
  trace: TraceEntry[];
  result_summary: string;
  tokens_used: number;
  budget_tokens: number;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  memory_refs: string[];
  agreement_score: number;
}

export interface TaskView {
  id: string;
  goal: string;
  task_type: TaskType;
  status: TaskStatus;
  plan: PlanStep[];
  results: StepResult[];
  result_summary: string;
  tokens_used: number;
  budget_tokens: number;
  model_profile: ModelProfile;
  agreement_score: number;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

// ===== 源: src/domain/user.rs =====
export type UserStatus = 'active' | 'disabled';

export interface User {
  id: string;
  username: string;
  username_lower: string;
  email: string;
  email_lower: string;
  display_name: string;
  password_hash: string;
  role: Role;
  status: UserStatus;
  locale: string;
  timezone: string;
  created_at: string;
  updated_at: string;
  last_login_at: string | null;
  failed_login_count: number;
  locked_until: string | null;
}

export interface UserView {
  id: string;
  username: string;
  email: string;
  display_name: string;
  role: Role;
  status: UserStatus;
  locale: string;
  timezone: string;
  created_at: string;
  last_login_at: string | null;
}

export interface Session {
  id: string;
  user_id: string;
  refresh_fingerprint: string;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  user_agent: string;
  client_ip: string;
}

// ===== 源: src/obs/alert.rs =====
export type Severity = 'info' | 'warning' | 'critical';

export interface AlertRule {
  id: string;
  title: string;
  severity: Severity;
  observed_from: string;
}

export interface AlertPolicy {
  dedup_secs: number;
  storm_threshold: number;
  http_5xx_ratio: number;
  min_http_requests: number;
  task_failure_ratio: number;
  min_tasks: number;
  rate_limited_hits: number;
  authz_denied_hits: number;
  circuit_open_hits: number;
}

export interface Alert {
  rule: string;
  title: string;
  severity: Severity;
  observed: number;
  threshold: number;
  detail: string;
  hits: number;
  first_fired_at: string;
  last_seen_at: string;
}

export interface Evaluation {
  alerts: Alert[];
  deduped: string[];
  aggregated: number;
}

export interface AutoAction {
  kind: string;
  detail: string;
}

export interface Alerter {
  policy: AlertPolicy;
  inner: Inner;
}

export interface AlertStats {
  fired_total: number;
  suppressed_total: number;
  aggregated_total: number;
  active_total: number;
  dedup_window_secs: number;
  storm_threshold: number;
  rules_total: number;
}

// ===== 源: src/obs/audit.rs =====
export interface AuditEvent {
  id: string;
  at: string;
  actor: string;
  action: string;
  target: string;
  decision: string;
  reason: string;
  request_id: string;
}

// ===== 源: src/obs/metrics.rs =====
export type MetricsKind = 'prometheus' | 'json' | 'noop';

export interface JsonExporter {
  inner: Metrics;
}

export interface NoopExporter {
  started: Instant;
}

// ===== 源: src/security/jwt.rs =====
export type JwtError = 'malformed' | 'bad_signature' | 'expired' | 'wrong_type' | 'wrong_issuer' | 'wrong_audience' | 'bad_algorithm' | 'encode';

// ===== 源: src/security/ratelimit.rs =====
export type LimiterKind = 'memory' | 'noop';

export interface Quota {
  capacity: number;
  refill_per_sec: number;
}

export interface LimitOutcome {
  allowed: boolean;
  retry_after_secs: number;
  remaining: number;
  limit: number;
}

// ===== 源: src/security/rbac.rs =====
export type Role = 'owner' | 'admin' | 'member';

export type Permission = 'read' | 'write' | 'sandbox_exec' | 'memory_write_shared' | 'memory_write_system' | 'cross_agent' | 'admin';

// ===== 源: src/services/auth.rs =====
export interface RegisterInput {
  username: string;
  email: string;
  password: string;
  display_name: string | null;
  locale: string | null;
  timezone: string | null;
}

export interface LoginInput {
  password: string;
}

export interface AuthTokens {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface AuthResult {
  user: UserView;
  tokens: AuthTokens;
  onboarding_status: string;
  onboarding_next_step: string | null;
}

// ===== 源: src/services/cluster_registry.rs =====
export interface ClusterRegistry {
  dir: PathBuf;
  node_id: string;
  ttl: Duration;
  enabled: boolean;
}
