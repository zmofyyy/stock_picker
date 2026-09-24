/**
 * 后端 API 类型定义。
 *
 * 与 backend/app/models/schemas.py 及服务层返回结构保持一致。
 */

/** 统一响应包装 */
export interface ApiResponse<T> {
  ok: boolean
  message: string
  data: T
}

/** 数据源状态 */
export interface DataStatus {
  tdx_dir: string
  vipdoc: string
  ready: boolean
  markets: Record<string, number>
  total_files: number
  adjust: string
  adjust_warning?: string
  start_date?: string
  last_data_date?: string | null
  pytdx: {
    available: boolean
    daily_reader: boolean
    min_reader: boolean
    error?: string | null
  }
  cache: { dir: string; format: string }
  names: { file: string; available: boolean; count: number }
  basics?: BasicsInfo
}

/** 股票池条目 */
export interface StockRow {
  code: string
  symbol: string
  market: string
  market_name: string
  is_index: boolean
  name?: string
  file: string
  size: number
  mtime: string
}

/** 数据加载结果 */
export interface LoadResult {
  total: number
  loaded: number
  failed: number
  rows: number
  freq?: string
  start?: string | null
  end?: string | null
  message: string
  details: Array<Record<string, unknown>>
}

/** 策略参数元信息 */
export interface ParamSchema {
  type?: 'int' | 'float' | 'bool' | 'str'
  label?: string
  min?: number
  max?: number
  step?: number
  help?: string
  /** type 为 str 时，渲染为下拉框的可选项 */
  options?: Array<{ value: string; label: string }>
}

/** 策略元信息 */
export interface StrategyMeta {
  name: string
  display_name: string
  description: string
  default_params: Record<string, unknown>
  param_schema: Record<string, ParamSchema>
  factor_columns: string[]
  min_bars: number
  /** 是否需要流通股本等股票基础信息（如 volume_surge 小盘放量） */
  requires_basics?: boolean
  config_params?: Record<string, unknown>
  effective_params?: Record<string, unknown>
}

/** 股票基础信息（流通股本）条目 */
export interface BasicsItem {
  code: string
  name: string
  /** 流通股本（股） */
  float_shares: number | null
  /** 流通股本（亿股） */
  float_shares_yi: number | null
  total_shares: number | null
  industry: string
}

/** 股票基础信息概览 */
export interface BasicsInfo {
  file: string
  available: boolean
  count: number
  strategies_requiring_basics: string[]
  warning?: string
}

/** 单条选股结果 */
export interface ScreenItem {
  code: string
  name: string
  date: string | null
  data_date: string | null
  strategy: string
  signal: number
  signal_text: string
  action: string
  is_new: boolean
  price: number | null
  prev_close: number | null
  pct_change: number | null
  reasons: string[]
  reason_text: string
  conditions: Record<string, boolean>
  factors: Record<string, number | null>
}

/** 选股结果集 */
export interface ScreenResult {
  strategy: string
  strategy_name: string
  params: Record<string, unknown>
  conditions: string[]
  start?: string | null
  end?: string | null
  total_scanned: number
  matched: number
  elapsed: number
  results: ScreenItem[]
  rows: Array<Record<string, unknown>>
  warnings: string[]
  markdown: string
}

/** 绩效指标 */
export type Metrics = Record<string, number | null>

/** 曲线点 */
export interface CurvePoint {
  date: string
  equity?: number
  net_value?: number
  return_pct?: number
  drawdown?: number
  drawdown_pct?: number
  strategy?: number
  benchmark?: number
}

/** 成交明细 */
export interface TradeRow {
  code: string
  name: string
  direction: string
  direction_text: string
  date: string
  price: number
  shares: number
  amount: number
  commission: number
  stamp_tax: number
  slippage_cost: number
  total_cost: number
  pnl: number
  pnl_pct: number
  hold_days: number
  reason: string
}

/** 持仓快照 */
export interface PositionRow {
  date: string
  code: string
  name: string
  shares: number
  avg_cost: number
  price: number
  market_value: number
  unrealized_pnl: number
  unrealized_pct: number
  weight: number
}

/** 回测结果 */
export interface BacktestResult {
  strategy: string
  strategy_name: string
  params: Record<string, unknown>
  start: string
  end: string
  universe_size: number
  elapsed: number
  metrics: Metrics
  metric_labels: Record<string, string>
  trade_stats: Record<string, number | null>
  trades: TradeRow[]
  positions: PositionRow[]
  rejected: Array<{ date: string; code: string; direction: string; reason: string }>
  account: Record<string, number>
  warnings: string[]
  benchmark_code: string
  equity_curve: CurvePoint[]
  drawdown_curve: CurvePoint[]
  benchmark_curve: CurvePoint[]
  monthly_returns: Array<Record<string, number | string | null>>
  markdown: string
  result_id?: string
}

/** 关注项 */
export interface WatchItem {
  id?: number
  code: string
  name: string
  group: string
  tags: string[]
  note: string
  strategy: string
  strategy_params: Record<string, unknown>
  cost_price: number | null
  shares: number | null
  target_price: number | null
  stop_price: number | null
  enabled: boolean
  source: string
  created_at?: string
  updated_at?: string
  state_info?: TrackingState | null
}

/** 追踪状态 */
export interface TrackingState {
  code: string
  name: string
  group: string
  state: string
  prev_state: string | null
  state_changed_at: string | null
  state_reason: string
  strategy: string
  signal: number
  signal_text: string
  signal_reason: string
  is_new_signal: boolean
  data_date: string | null
  price: number | null
  prev_close: number | null
  pct_change: number | null
  volume: number | null
  amount: number | null
  risk_level: string
  risk_flags: string[]
  cost_price: number | null
  shares: number | null
  unrealized_pnl: number | null
  unrealized_pct: number | null
  hold_days: number | null
  max_drawdown: number | null
  factors: Record<string, number | null>
  extra: Record<string, unknown>
  updated_at: string
}

/** 状态历史 */
export interface HistoryRow {
  id: number
  code: string
  name: string
  old_state: string
  new_state: string
  changed_at: string
  trade_date: string | null
  reason: string
  strategy: string
  trigger_type: string
  factors: Record<string, unknown>
  price: number | null
  note: string
}

/** 信号记录 */
export interface SignalRow {
  id: number
  code: string
  name: string
  strategy: string
  signal: number
  signal_text: string
  trade_date: string
  price: number | null
  pct_change: number | null
  reason: string
  is_new: boolean
  factors: Record<string, unknown>
  created_at: string
}

/** 提醒记录 */
export interface AlertRow {
  id: number
  code: string
  name: string
  level: string
  category: string
  title: string
  message: string
  strategy: string
  trade_date: string | null
  trigger_value: number | null
  delivered: Record<string, unknown>
  acked: boolean
  created_at: string
}

/** 追踪更新结果 */
export interface UpdateResult {
  trade_date: string | null
  trigger_type: string
  processed: number
  changed: number
  skipped: number
  new_signals: number
  alerts: number
  errors: Array<{ code: string; error: string }>
  state_changes: HistoryRow[]
  signals: SignalRow[]
  alert_list: AlertRow[]
  elapsed: number
}

/** 仪表盘数据 */
export interface DashboardData {
  date: string
  watch_count: number
  active_count: number
  tracked_count: number
  state_distribution: Record<string, number>
  risk_distribution: Record<string, number>
  today_signals: SignalRow[]
  today_state_changes: HistoryRow[]
  today_alerts: AlertRow[]
  holdings: TrackingState[]
  holding_count: number
  total_unrealized_pnl: number
  storage: Record<string, number>
  data_status?: DataStatus
  last_backtest?: {
    id: string
    strategy_name: string
    start: string
    end: string
    total_return: number | null
    max_drawdown: number | null
    sharpe: number | null
  } | null
}

/** 报告 */
export interface ReportData {
  kind: string
  title: string
  markdown: string
  html: string
  generated_at: string
  period: Record<string, unknown>
  summary: Record<string, unknown>
  rows: Array<Record<string, unknown>>
}

/** 配置 */
export type AppConfig = Record<string, any>

export interface SettingsData {
  config: AppConfig
  path: string
  storage: Record<string, number>
  notify: Record<string, unknown>
  scheduler: Record<string, unknown>
}

/** 数据质量 */
export interface QualityData {
  total: number
  ok: number
  problem_count: number
  issues: Array<{ code: string; level: string; message: string }>
  pytdx: Record<string, unknown>
  adjust: string
}

/** 缓存信息 */
export interface CacheInfo {
  cache_dir: string
  format: string
  count: number
  total_size: number
  total_size_mb: number
  entries: Array<Record<string, unknown>>
}
