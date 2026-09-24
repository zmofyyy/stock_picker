/**
 * HTTP API 客户端。
 *
 * 所有页面统一通过这里与后端交互；前端不做任何本地计算，
 * 保证「回测 / 选股 / 追踪」的结果与 CLI 完全一致。
 */
import axios, { AxiosInstance } from 'axios'
import type {
  AlertRow,
  ApiResponse,
  BacktestResult,
  BasicsItem,
  CacheInfo,
  DashboardData,
  DataStatus,
  HistoryRow,
  LoadResult,
  QualityData,
  ReportData,
  ScreenResult,
  SettingsData,
  SignalRow,
  StockRow,
  StrategyMeta,
  TrackingState,
  UpdateResult,
  WatchItem,
} from './types'

/** 统一解包后端响应 */
function unwrap<T>(resp: { data: ApiResponse<T> }): T {
  const body = resp.data
  if (body && typeof body === 'object' && 'ok' in body) {
    if (!body.ok) {
      throw new Error(body.message || '请求失败')
    }
    return body.data
  }
  return body as unknown as T
}

/** 把后端错误信息转成可读文本 */
function toError(error: unknown): never {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    const message = error.response?.data?.message
    throw new Error(
      (typeof detail === 'string' && detail) ||
        (typeof message === 'string' && message) ||
        error.message,
    )
  }
  throw error
}

export class StockSelectorApi {
  private http: AxiosInstance

  constructor(baseURL = '') {
    this.http = axios.create({
      baseURL,
      timeout: 600_000, // 回测/回放可能较慢
      headers: { 'Content-Type': 'application/json' },
    })
  }

  /** 下载文本（CSV / Markdown / HTML） */
  async downloadText(path: string): Promise<string> {
    try {
      const resp = await this.http.get(path, { responseType: 'text' })
      return resp.data as string
    } catch (error) {
      toError(error)
    }
  }

  // ---------------- 系统 ----------------
  async health() {
    return unwrap<{ version: string }>(await this.http.get('/api/health'))
  }

  // ---------------- 数据 ----------------
  async dataStatus(): Promise<DataStatus> {
    return unwrap<DataStatus>(await this.http.get('/api/data/status'))
  }

  async listStocks(params: {
    include_index?: boolean
    limit?: number
    keyword?: string
  } = {}): Promise<{
    count: number
    returned: number
    stocks: StockRow[]
    markets: Record<string, number>
  }> {
    return unwrap(await this.http.get('/api/data/stocks', { params }))
  }

  async loadData(payload: {
    codes?: string[]
    freq?: string
    start?: string
    end?: string
    limit?: number
    force?: boolean
    tdx_dir?: string
  }): Promise<LoadResult> {
    return unwrap<LoadResult>(await this.http.post('/api/data/load', payload))
  }

  async preview(code: string, params: { freq?: string; limit?: number } = {}) {
    return unwrap<{
      code: string
      name: string
      count: number
      rows: Array<Record<string, unknown>>
    }>(await this.http.get(`/api/data/preview/${code}`, { params }))
  }

  async qualityCheck(payload: { codes?: string[]; max_codes?: number }): Promise<QualityData> {
    return unwrap<QualityData>(await this.http.post('/api/data/quality', payload))
  }

  async cacheInfo(limit = 200): Promise<CacheInfo> {
    return unwrap<CacheInfo>(await this.http.get('/api/data/cache', { params: { limit } }))
  }

  async clearCache(code?: string) {
    return unwrap<{ removed: number }>(
      await this.http.delete('/api/data/cache', { params: { code } }),
    )
  }

  async checkTdxDir(directory: string) {
    return unwrap<{ input: string; vipdoc: string; valid: boolean; current: string }>(
      await this.http.get('/api/data/tdx-dir', { params: { directory } }),
    )
  }

  async setTdxDir(tdx_dir: string, persist = true): Promise<DataStatus> {
    return unwrap<DataStatus>(
      await this.http.post('/api/data/tdx-dir', { tdx_dir, persist }),
    )
  }

  async createNamesTemplate() {
    return unwrap<{ path: string }>(await this.http.post('/api/data/names/template'))
  }

  async updateNames(names: Record<string, string>) {
    return unwrap<{ count: number; file: string }>(
      await this.http.post('/api/data/names', { names }),
    )
  }

  // ---------------- 股票基础信息（流通股本） ----------------
  /** 查询已配置的流通股本等基础信息 */
  async listBasics(params: { keyword?: string; limit?: number } = {}): Promise<{
    count: number
    returned: number
    file: string
    items: BasicsItem[]
  }> {
    return unwrap(await this.http.get('/api/data/basics', { params }))
  }

  /** 生成 stock_basic.csv 模板 */
  async createBasicsTemplate() {
    return unwrap<{ path: string; exists: boolean }>(
      await this.http.post('/api/data/basics/template'),
    )
  }

  /** 批量写入流通股本（单位：股，也支持 "65.7亿" 这类写法） */
  async updateBasics(basics: Record<string, unknown>) {
    return unwrap<{ count: number; file: string }>(
      await this.http.post('/api/data/basics', { basics }),
    )
  }

  // ---------------- 选股 ----------------
  async screenMeta(): Promise<{
    strategies: StrategyMeta[]
    default_strategy: string
    defaults: { min_bars: number; max_results: number }
    last_result: Record<string, unknown> | null
  }> {
    return unwrap(await this.http.get('/api/screen'))
  }

  async runScreen(payload: {
    strategy?: string
    params?: Record<string, unknown>
    date?: string
    start?: string
    end?: string
    scan_all?: boolean
    universe?: string[]
    min_bars?: number
    max_results?: number
    only_buy?: boolean
    limit_universe?: number
  }): Promise<ScreenResult> {
    return unwrap<ScreenResult>(await this.http.post('/api/screen/run', payload))
  }

  async screenToWatchlist(payload: {
    codes?: string[]
    group?: string
    strategy?: string
    note?: string
  }) {
    return unwrap<{ added: number; failed: unknown[] }>(
      await this.http.post('/api/screen/to-watchlist', payload),
    )
  }

  // ---------------- 回测 ----------------
  async backtestConfig(): Promise<{
    defaults: Record<string, number | string | boolean>
    strategies: StrategyMeta[]
    benchmarks: string[]
    recent: Array<Record<string, unknown>>
  }> {
    return unwrap(await this.http.get('/api/backtest/config'))
  }

  async runBacktest(payload: Record<string, unknown>): Promise<BacktestResult> {
    return unwrap<BacktestResult>(await this.http.post('/api/backtest/run', payload))
  }

  async backtestToWatchlist(payload: { group?: string; only_open?: boolean } = {}) {
    return unwrap<{ added: number }>(
      await this.http.post('/api/backtest/to-watchlist', payload),
    )
  }

  // ---------------- 追踪 ----------------
  async getWatchlist(params: {
    group?: string
    enabled_only?: boolean
    keyword?: string
    with_state?: boolean
  } = {}): Promise<{ items: WatchItem[]; count: number; groups: Array<{ group: string; count: number }> }> {
    return unwrap(await this.http.get('/api/tracker/watchlist', { params }))
  }

  async addWatch(payload: Record<string, unknown>): Promise<WatchItem> {
    return unwrap<WatchItem>(await this.http.post('/api/tracker/watchlist', payload))
  }

  async addWatchBatch(payload: {
    items: Array<Record<string, unknown>>
    group?: string
    source?: string
    overwrite?: boolean
  }) {
    return unwrap<{ added: number; failed: unknown[] }>(
      await this.http.post('/api/tracker/watchlist/batch', payload),
    )
  }

  async updateWatch(code: string, payload: Record<string, unknown>): Promise<WatchItem> {
    return unwrap<WatchItem>(await this.http.patch(`/api/tracker/watchlist/${code}`, payload))
  }

  async removeWatch(code: string, keepState = false) {
    return unwrap<{ code: string }>(
      await this.http.delete(`/api/tracker/watchlist/${code}`, {
        params: { keep_state: keepState },
      }),
    )
  }

  async pauseWatch(code: string, paused = true): Promise<WatchItem> {
    return unwrap<WatchItem>(
      await this.http.post(`/api/tracker/watchlist/${code}/pause`, null, {
        params: { paused },
      }),
    )
  }

  async updateTracking(payload: {
    codes?: string[]
    date?: string
    group?: string
    strategy?: string
    params?: Record<string, unknown>
    notify?: boolean
  }): Promise<UpdateResult> {
    return unwrap<UpdateResult>(await this.http.post('/api/tracker/update', payload))
  }

  async replayTracking(payload: {
    codes?: string[]
    start?: string
    end?: string
    group?: string
    strategy?: string
    reset?: boolean
    notify?: boolean
  }): Promise<UpdateResult> {
    return unwrap<UpdateResult>(await this.http.post('/api/tracker/replay', payload))
  }

  async listStates(params: {
    state?: string
    group?: string
    risk_level?: string
    keyword?: string
  } = {}): Promise<{ items: TrackingState[]; count: number }> {
    return unwrap(await this.http.get('/api/tracker/states', { params }))
  }

  async getState(code: string): Promise<{
    state: TrackingState | null
    watch?: WatchItem | null
    message?: string
  }> {
    return unwrap(await this.http.get(`/api/tracker/state/${code}`))
  }

  async setState(code: string, state: string, reason = '', note = '') {
    return unwrap<HistoryRow>(
      await this.http.post(`/api/tracker/state/${code}`, { state, reason, note }),
    )
  }

  async getHistory(
    code: string,
    params: { start?: string; end?: string; limit?: number } = {},
  ): Promise<{ code: string; items: HistoryRow[]; count: number }> {
    return unwrap(await this.http.get(`/api/tracker/history/${code}`, { params }))
  }

  async getTimeline(code: string, limit = 100) {
    return unwrap<{
      code: string
      name: string
      events: Array<{
        type: string
        date: string
        title: string
        detail: string
        level: string
        raw: Record<string, unknown>
      }>
    }>(await this.http.get(`/api/tracker/timeline/${code}`, { params: { limit } }))
  }

  async getSignals(params: Record<string, unknown> = {}): Promise<{
    items: SignalRow[]
    count: number
  }> {
    return unwrap(await this.http.get('/api/tracker/signals', { params }))
  }

  async getAlerts(params: Record<string, unknown> = {}): Promise<{
    items: AlertRow[]
    count: number
  }> {
    return unwrap(await this.http.get('/api/tracker/alerts', { params }))
  }

  async ackAlert(id: number) {
    return unwrap<{ id: number }>(await this.http.post(`/api/tracker/alerts/${id}/ack`))
  }

  async dashboard(date?: string): Promise<DashboardData> {
    return unwrap<DashboardData>(await this.http.get('/api/tracker/dashboard', { params: { date } }))
  }

  async storageStats() {
    return unwrap<Record<string, number>>(await this.http.get('/api/tracker/storage'))
  }

  async notifyStatus() {
    return unwrap<Record<string, unknown>>(await this.http.get('/api/tracker/notify'))
  }

  async testNotify() {
    return unwrap<Record<string, unknown>>(await this.http.post('/api/tracker/notify/test'))
  }

  async addNote(code: string, content: string) {
    return unwrap<Record<string, unknown>>(
      await this.http.post(`/api/tracker/notes/${code}`, { content }),
    )
  }

  async listNotes(code: string) {
    return unwrap<Array<Record<string, unknown>>>(
      await this.http.get(`/api/tracker/notes/${code}`),
    )
  }

  // ---------------- 报告 ----------------
  async getReport(params: {
    kind?: 'daily' | 'range' | 'summary'
    date?: string
    code?: string
    start?: string
    end?: string
    group?: string
  }): Promise<ReportData> {
    return unwrap<ReportData>(await this.http.get('/api/report', { params }))
  }

  // ---------------- 设置 ----------------
  async getSettings(): Promise<SettingsData> {
    return unwrap<SettingsData>(await this.http.get('/api/settings'))
  }

  async updateSettings(patch: Record<string, unknown>, persist = true): Promise<{ config: Record<string, unknown>; path: string }> {
    return unwrap(
      await this.http.post('/api/settings', { patch, persist }),
    )
  }

  async getStrategySettings() {
    return unwrap<{ default: string; strategies: StrategyMeta[] }>(
      await this.http.get('/api/settings/strategies'),
    )
  }

  async saveStrategySettings(name: string, params: Record<string, unknown>) {
    return unwrap<Record<string, unknown>>(
      await this.http.post('/api/settings/strategies', { name, params }),
    )
  }

  async schedulerStatus() {
    return unwrap<Record<string, unknown>>(await this.http.get('/api/settings/scheduler'))
  }

  async schedulerStart() {
    return unwrap<Record<string, unknown>>(await this.http.post('/api/settings/scheduler/start'))
  }

  async schedulerStop() {
    return unwrap<Record<string, unknown>>(await this.http.post('/api/settings/scheduler/stop'))
  }

  async schedulerRun(notify = true) {
    return unwrap<Record<string, unknown>>(
      await this.http.post('/api/settings/scheduler/run', null, { params: { notify } }),
    )
  }

  async resetDb(tables?: string) {
    return unwrap<Record<string, number>>(
      await this.http.post('/api/settings/reset-db', null, {
        params: { tables, confirm: true },
      }),
    )
  }
}

/** 全局单例（开发模式下走 Vite 代理，因此 baseURL 为空） */
export const api = new StockSelectorApi()

/** 触发浏览器下载 */
export function downloadText(filename: string, content: string, mime = 'text/plain') {
  const blob = new Blob([content], { type: `${mime};charset=utf-8` })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}
