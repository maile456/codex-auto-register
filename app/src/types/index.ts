export interface AccountRecord {
  id: string
  email: string
  chatgptPassword: string
  totpSecret: string
  emailAccessUrl: string
  createdAt: string
  accountType: AccountType
  phoneBound: boolean | null
  promotionEligible: boolean | null
  accessTokenConfigured: boolean
  accessTokenExpiresAt: string | null
  accessTokenUpdatedAt: string | null
  planCheckStatus?: 'running' | 'success' | 'failed' | null
  planCheckedAt?: string | null
  planCheckErrorCode?: string | null
  planCheckHttpStatus?: number | null
  planAccountId?: string | null
  subscriptionPlan?: string | null
  hasActiveSubscription?: boolean | null
  planExpiresAt?: string | null
  planRenewsAt?: string | null
  promotionCampaignId?: string | null
  checkoutType?: 'oaics' | 'cs' | null
  checkoutTypeCheckedAt?: string | null
  registrationCountry?: string | null
}

export interface AccountPlanCheckItem {
  id: string
  status: 'success' | 'failed' | 'skipped'
  errorCode: string | null
}

export interface AccountPlanCheckResult {
  requested: number
  succeeded: number
  failed: number
  skipped: number
  items: AccountPlanCheckItem[]
}

export type AccountType = 'plus' | 'free'

export interface EmailRecord {
  id: string
  email: string
  accessUrl: string
  importedAt: string
  sourceType?: 'manual' | 'mailcom_alias'
  parentEmail?: string | null
}

export type EmailSource = 'all' | 'standard' | 'mailcom_alias'

export type ProxyStatus = 'available' | 'unknown' | 'quarantined'
export type ProxyScheme = 'http' | 'https' | 'socks5' | 'socks5h'

export interface ProxyRecord {
  id: string
  host: string
  port: number
  username: string
  password: string
  enabled: boolean
  status: ProxyStatus
  latencyMs: number | null
  lastCheckedAt: string | null
  country: string
  group: string
  scheme: ProxyScheme
}

export interface ProxyCountrySummary {
  country: string
  total: number
  enabled: number
}

export interface ProxyGroupSummary {
  country: string
  group: string
  total: number
  enabled: number
  available: number
  quarantined: number
  schemes: ProxyScheme[]
}

export interface ParsedEmail {
  email: string
  accessUrl: string
}

export interface ParsedProxy {
  host: string
  port: number
  username: string
  password: string
  scheme: ProxyScheme
}

export interface ImportIssue {
  line: number
  reason: string
  preview: string
}

export interface ImportPreview<T> {
  total: number
  accepted: T[]
  duplicates: ImportIssue[]
  errors: ImportIssue[]
}

export interface ImportResult {
  total: number
  imported: number
  duplicateCount: number
  errorCount: number
}

export interface ExecutionSettings {
  schemaVersion: 2
  browserExecutablePath: string
  roxyApiKey: string
  roxyApiPort: number
  headless: boolean
  proxyRetryCount: number
  concurrency: number
  taskTimeoutSeconds: number
  updatedAt: string | null
}

export interface ExecutionSettingsInput {
  browserExecutablePath: string
  roxyApiKey: string
  roxyApiPort: number
  headless: boolean
  proxyRetryCount: number
  concurrency: number
  taskTimeoutSeconds: number
}

export type ExportFormat = 'credentials' | 'mail-links' | 'access-tokens'
export type ExportScope = 'single' | 'selected' | 'all'

export interface AccountExport {
  content: string
  filename: string
  count: number
  format: ExportFormat
  skippedMissingCount: number
  skippedExpiredCount: number
}

export interface EmailExport {
  content: string
  filename: string
  count: number
}

export interface PageResponse<T> {
  items: T[]
  total: number
  page: number
  pageSize: 10 | 20 | 50 | 100
}

export interface ResourceQuery {
  page: number
  pageSize: 10 | 20 | 50 | 100
  q?: string
  country?: string
  promotion?: '' | 'untried_plus' | 'ineligible' | 'unchecked'
  source?: EmailSource
}

export interface OverviewStats {
  accounts: {
    total: number
    today: number
    totpComplete: number
    plus: { total: number; bound: number; unbound: number }
    free: { total: number; eligible: number; ineligible: number }
  }
  emails: { available: number; aliases: number }
  proxies: { total: number; enabled: number; available: number; quarantined: number }
}

export interface MongoHealth {
  status: 'online' | 'offline' | 'reconnecting'
  database: string
  error: string | null
  nextRetrySeconds: number | null
}

export interface HealthResponse {
  status: 'ok' | 'degraded'
  mode: 'local'
  mongodb: MongoHealth
}

export type RunStatus =
  | 'idle'
  | 'queued'
  | 'running'
  | 'waiting_for_database'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted'
export type RunKind = 'mock' | 'browser_probe'
export type WorkerStatus =
  | 'queued'
  | 'running'
  | 'success'
  | 'partial_success'
  | 'failed'
  | 'cancelled'
export type WorkerStage =
  | 'queued'
  | 'roxy_starting'
  | 'proxy_check'
  | 'login'
  | 'email'
  | 'verification'
  | 'profile'
  | 'access_token'
  | 'cleanup'
  | 'success'
  | 'partial_success'
  | 'failed'
  | 'cancelled'
export type RunLogLevel = 'info' | 'success' | 'warning' | 'error'
export type RunLogDetailValue = string | number | boolean | null

export interface RunLogEntry {
  schemaVersion: 1
  runId: string
  timestamp: string
  level: RunLogLevel
  event: string
  message: string
  email?: string | null
  sequence?: number | null
  details?: Record<string, RunLogDetailValue>
}

export interface RunLogSummary {
  runId: string
  filename: string
  startedAt: string
  updatedAt: string
  entryCount: number
  lastEvent: string
}

export interface RunLogFile extends RunLogSummary {
  entries: RunLogEntry[]
}

export interface RunExecutionState {
  status: RunStatus
  runId: string | null
  kind: RunKind
  requested: number
  pending: number
  processed: number
  succeeded: number
  failed: number
  workerCount: number
  activeWorkers: number
  startedAt: string | null
  updatedAt: string | null
  finishedAt: string | null
  logPersisted: boolean
  cancelRequested: boolean
  terminalReasonCode?: string | null
  registrationCountry?: string | null
  registrationProxyGroup?: string | null
  emailSource?: EmailSource
}

export interface ExtractedAccessToken {
  token: string
  preview: string
  email: string | null
  accountId: string | null
  planType: string | null
  expiresAt: string | null
  expired: boolean | null
}

export interface AccessTokenExtractResult {
  count: number
  items: ExtractedAccessToken[]
}

export type PaymentExtractorTaskStatus =
  | 'queued'
  | 'running'
  | 'cancel_requested'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export interface PaymentExtractorOption {
  value: string
  label: string
  currency?: string | null
}

export interface PaymentExtractorDefaults {
  country: string
  forceCountry?: string | null
  paymentMethod: string
  checkoutProxy: string
  updateProxy: string
  applyCheckoutUpdate: boolean
  concurrency: number
  maxConcurrency: number
  countries: PaymentExtractorOption[]
  paymentMethods: PaymentExtractorOption[]
}

export interface PaymentExtractorConcurrency {
  concurrency: number
  maxConcurrency: number
}

export interface PaymentExtractorTaskInput {
  accessToken: string
  checkoutProxy: string
  updateProxy: string
  stripeHcaptchaToken: string
  country: string
  paymentMethod: string
  applyCheckoutUpdate: boolean
}

export interface PaymentExtractorRetryInput {
  checkoutProxy?: string
  updateProxy?: string
}

export interface PaymentExtractorBilling {
  name?: string
  email?: string
  phone?: string
  country?: string
  line1?: string
  city?: string
  state?: string
  postalCode?: string
}

export interface PaymentExtractorResult {
  checkoutSessionId?: string
  sessionKind?: string
  paymentMethod?: string
  billingCountry?: string
  currency?: string
  amountDue?: number
  amountDueMinor?: number
  billing?: PaymentExtractorBilling
  accountEmail?: string
  paymentMethodId?: string
  stripeRedirectUrl?: string
  providerUrl?: string
  paypalUrl?: string
  gopayUrl?: string
  gcashUrl?: string
  [key: string]: unknown
}

export interface PaymentExtractorTask {
  taskId: string
  status: PaymentExtractorTaskStatus
  stage: string
  progress: number
  createdAt: string
  startedAt?: string | null
  finishedAt?: string | null
  accountEmail?: string | null
  paymentMethod: string
  billingCountry: string
  sessionKind?: string | null
  retryOf?: string | null
  result?: PaymentExtractorResult | null
  error?: string | null
  networkError?: boolean
  message?: string | null
  logs?: string[]
}

export interface PaymentExtractorTaskList {
  tasks: PaymentExtractorTask[]
}

export interface PaymentExtractorProxyTestResult {
  ok: boolean
  ip?: string
  country?: string
  countryCode?: string
  region?: string
  regionCode?: string
  city?: string
}

export interface PaymentExtractorProxySourceResult {
  ok: boolean
  proxies: string[]
  count: number
  uniqueCount: number
}

export interface PaymentExtractorDeleteResult {
  ok: boolean
  taskId?: string
  status?: string
}

export interface PaymentExtractorBulkDeleteResult {
  ok: boolean
  deletedCount: number
  taskIds: string[]
}

export type PaypalAgreementServiceStatus = 'online' | 'starting' | 'stopped' | 'failed' | 'conflict'

export interface PaypalAgreementServiceState {
  ok: boolean
  status: PaypalAgreementServiceStatus
  service: string
  sourceCommit: string
  host: string
  port: number
  uiPath: string
  uiUrl: string
  managed: boolean
  pid?: number | null
  error?: string
}

export type PipelineStage =
  | 'eligible'
  | 'extracting'
  | 'extraction_failed'
  | 'payment_ready'
  | 'paying'
  | 'payment_waiting_otp'
  | 'payment_waiting_manual'
  | 'payment_failed'
  | 'paid'

export interface PipelineError {
  code: string
  message: string
}

export interface PipelineItem {
  id: string
  accountId: string
  email: string
  chatgptPassword: string
  totpSecret: string
  emailAccessUrl: string
  accountCreatedAt: string | null
  accountType: AccountType
  promotionEligible: boolean
  promotionCampaignId?: string | null
  planCheckedAt?: string | null
  accessTokenConfigured: boolean
  accessTokenExpiresAt: string | null
  stage: PipelineStage
  extractionStatus: string
  extractionRetryCount: number
  extractorTaskId?: string | null
  extractionError?: PipelineError | null
  billingCountry?: string | null
  extractedAt?: string | null
  checkoutType?: 'oaics' | 'cs' | null
  checkoutTypeCheckedAt?: string | null
  paymentLink?: string | null
  paymentLinkConfigured: boolean
  paymentStatus: string
  paymentRetryCount: number
  paymentJobId?: string | null
  paymentError?: PipelineError | null
  paymentPhonePreview?: string | null
  heroSmsManaged: boolean
  heroSmsStatus?: string | null
  heroSmsAttempt: number
  heroSmsPrice?: number | null
  heroSmsWaitDeadline?: string | null
  heroSmsError?: PipelineError | null
  paidAt?: string | null
  paymentSummary?: {
    status?: string | null
    settlementStatus?: string | null
    billingCountry?: string | null
  } | null
  exportCount: number
  firstExportedAt?: string | null
  lastExportedAt?: string | null
  mailConfirmationStatus: 'unchecked' | 'waiting' | 'confirmed' | 'not_found' | 'review' | 'failed'
  mailConfirmationSubject?: string | null
  mailConfirmationReceivedAt?: string | null
  mailConfirmationCheckedAt?: string | null
  mailConfirmationError?: string | null
  mailConfirmationOrderId?: string | null
  mailConfirmationAttempt?: number
  mailConfirmationStartedAt?: string | null
  mailConfirmationDeadline?: string | null
  mailConfirmationNextCheckAt?: string | null
  smsReceiverState?: 'idle' | 'queued' | 'running' | 'retry_wait' | 'paused' | 'completed' | 'ready' | 'failed' | 'stopped'
  smsReceiverCredentialReady?: boolean
  smsReceiverPhoneVerified?: boolean
  smsReceiverTaskId?: string | null
  smsReceiverSubmittedAt?: string | null
  smsReceiverUpdatedAt?: string | null
  smsReceiverError?: string | null
  createdAt: string
  updatedAt: string
}

export interface PipelineList {
  items: PipelineItem[]
  total: number
  page: number
  pageSize: number
  counts: Record<string, number>
}

export interface PipelinePaidStats {
  total: number
  today: number
  last7Days: number
  terminalTotal: number
  failed: number
  successRate: number
  averageHeroSmsPrice: number | null
  exported: number
  unexported: number
  mailConfirmed: number
  daily: Array<{ date: string; count: number }>
}

export interface PipelinePaidExport {
  content: string
  filename: string
  count: number
  skippedMissingUrlCount: number
}

export interface PipelinePaidMailCheck {
  requested: number
  checked: number
  confirmed: number
  waiting?: number
  review?: number
  notFound: number
  failed: number
  items: Array<{ id: string; status: string }>
}

export interface SmsReceiverSettings {
  enabled: boolean
  autoSubmit: boolean
  baseUrl: string
  mailboxPublicBaseUrl: string
  updatedAt: string | null
}

export interface SmsReceiverBatchResult {
  requested: number
  processed: number
  submitted?: number
  ready?: number
  failed: number
  items: Array<{ id: string; ok: boolean; state: string; error?: string }>
}

export interface PipelineSettings {
  enabled: boolean
  extractionConcurrency: number
  paymentConcurrency: number
  extractionFailureRetries: number
  paymentFailureRetries: number
  country: string
  checkoutProxy: string
  updateProxy: string
  protocolProxy: string
  checkoutProxyCountry: string
  updateProxyCountry: string
  protocolProxyCountry: string
  checkoutProxyGroup: string
  updateProxyGroup: string
  protocolProxyGroup: string
  applyCheckoutUpdate: boolean
  heroSmsEnabled: boolean
  autoPaymentEnabled: boolean
  heroSmsMaxPrice: number
  heroSmsChangeNumberRetries: number
  heroSmsNumberWaitSeconds: number
  heroSmsCountryId: number
  agreementAutoSmsEnabled: boolean
  heroSmsApiKeyConfigured: boolean
  updatedAt?: string | null
}

export interface HeroSmsCountry {
  id: number
  name: string
}

export interface HeroSmsSettings {
  enabled: boolean
  countryId: number
  maxPrice: number
  changeNumberRetries: number
  numberWaitSeconds: number
  agreementAutoSmsEnabled: boolean
  pipelineAutoPaymentEnabled: boolean
  apiKeyConfigured: boolean
  updatedAt?: string | null
}

export interface HeroSmsTestResult {
  ok: boolean
  configured: boolean
  countryId: number
  service: 'PayPal'
  balance: number
}

export interface RunWorkerSnapshot {
  workerId: string
  sequence: number
  status: WorkerStatus
  stage: WorkerStage
  stageElapsedMs: number
  email: string
  egressIp: string | null
  errorCode: string | null
  errorStage?: string | null
  errorOperation?: string | null
  errorKind?: string | null
  errorHttpStatus?: number | null
  errorApiCode?: number | null
  errorRetryCount?: number | null
  errorElapsedMs?: number | null
  startedAt: string | null
  updatedAt: string
  finishedAt: string | null
}
