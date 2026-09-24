/**
 * 回测页面。
 *
 * 展示净值曲线、回撤曲线、持仓、交易明细、绩效指标，并支持导出 CSV / HTML / Markdown。
 */
import { useEffect, useMemo, useState } from 'react'
import {
  Button,
  Card,
  Col,
  DatePicker,
  Divider,
  Empty,
  InputNumber,
  Row,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import { DownloadOutlined, PlayCircleOutlined, StarOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import * as echarts from 'echarts'
import dayjs, { Dayjs } from 'dayjs'
import { api, downloadText } from '@/api'
import type { BacktestResult, PositionRow, TradeRow } from '@/api'
import EChart from '@/components/EChart'
import MarkdownView from '@/components/MarkdownView'
import MetricCard from '@/components/MetricCard'
import StrategyParamsForm from '@/components/StrategyParamsForm'

const { Title, Text } = Typography

export default function Backtest() {
  const [strategies, setStrategies] = useState<Array<Record<string, any>>>([])
  const [strategy, setStrategy] = useState('ma_cross')
  const [params, setParams] = useState<Record<string, unknown>>({})
  const [benchmarks, setBenchmarks] = useState<string[]>(['000300.SH', '000001.SH'])
  const [range, setRange] = useState<[Dayjs, Dayjs]>([dayjs('2022-01-01'), dayjs('2024-01-01')])
  const [universeText, setUniverseText] = useState('')
  const [maxUniverse, setMaxUniverse] = useState<number | null>(null)

  const [cash, setCash] = useState(1_000_000)
  const [commission, setCommission] = useState(0.0003)
  const [stampTax, setStampTax] = useState(0.001)
  const [slippage, setSlippage] = useState(0.0002)
  const [maxPositions, setMaxPositions] = useState(10)
  const [sizing, setSizing] = useState<'equal' | 'fixed'>('equal')
  const [fixedAmount, setFixedAmount] = useState(100_000)
  const [execPrice, setExecPrice] = useState<'next_open' | 'next_close' | 'close'>('next_open')
  const [benchmark, setBenchmark] = useState('000300.SH')

  const [result, setResult] = useState<BacktestResult | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    ;(async () => {
      try {
        const cfg = await api.backtestConfig()
        setStrategies(cfg.strategies as unknown as Array<Record<string, any>>)
        setBenchmarks(cfg.benchmarks)
        const d = cfg.defaults as Record<string, any>
        setCash(Number(d.initial_cash ?? 1_000_000))
        setCommission(Number(d.commission ?? 0.0003))
        setStampTax(Number(d.stamp_tax ?? 0.001))
        setSlippage(Number(d.slippage ?? 0.0002))
        setMaxPositions(Number(d.max_positions ?? 10))
        setSizing((d.position_sizing as 'equal' | 'fixed') ?? 'equal')
        setFixedAmount(Number(d.fixed_amount ?? 100_000))
        setExecPrice((d.exec_price as any) ?? 'next_open')
        setBenchmark(String(d.benchmark ?? '000300.SH'))
      } catch (error) {
        message.error(`加载回测配置失败：${(error as Error).message}`)
      }
    })()
  }, [])

  const handleRun = async () => {
    setLoading(true)
    try {
      const universe = universeText
        .split(/[,\s]+/)
        .map((s) => s.trim())
        .filter(Boolean)
      const data = await api.runBacktest({
        strategy,
        params,
        start: range[0].format('YYYY-MM-DD'),
        end: range[1].format('YYYY-MM-DD'),
        universe: universe.length ? universe : undefined,
        max_universe: maxUniverse || undefined,
        initial_cash: cash,
        commission,
        stamp_tax: stampTax,
        slippage,
        max_positions: maxPositions,
        position_sizing: sizing,
        fixed_amount: fixedAmount,
        exec_price: execPrice,
        benchmark,
      })
      setResult(data)
      data.warnings?.forEach((w) => message.warning(w))
      message.success(`回测完成，共 ${data.trades.length} 笔成交，耗时 ${data.elapsed.toFixed(2)}s`)
    } catch (error) {
      message.error(`回测失败：${(error as Error).message}`)
    } finally {
      setLoading(false)
    }
  }

  /** 净值 + 回撤组合图 */
  const equityOption = useMemo<echarts.EChartsOption>(() => {
    if (!result) return {}
    const dates = result.equity_curve.map((p) => p.date)
    const netValues = result.equity_curve.map((p) => p.net_value ?? 1)
    const drawdowns = result.drawdown_curve.map((p) => (p.drawdown_pct ?? 0))
    const bench = result.benchmark_curve.map((p) => p.benchmark ?? null)

    return {
      tooltip: { trigger: 'axis' },
      legend: { data: ['策略净值', '基准净值', '回撤'], top: 0 },
      grid: [
        { left: 60, right: 30, top: 40, height: '52%' },
        { left: 60, right: 30, top: '72%', height: '20%' },
      ],
      xAxis: [
        { type: 'category', data: dates, gridIndex: 0, boundaryGap: false, axisLabel: { show: false } },
        { type: 'category', data: dates, gridIndex: 1, boundaryGap: false },
      ],
      yAxis: [
        { type: 'value', name: '净值', gridIndex: 0, scale: true },
        { type: 'value', name: '回撤%', gridIndex: 1, max: 0 },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1] },
        { type: 'slider', xAxisIndex: [0, 1], bottom: 0, height: 18 },
      ],
      series: [
        {
          name: '策略净值',
          type: 'line',
          data: netValues,
          showSymbol: false,
          lineStyle: { width: 2, color: '#cf1322' },
          xAxisIndex: 0,
          yAxisIndex: 0,
        },
        ...(bench.length
          ? [
              {
                name: '基准净值',
                type: 'line' as const,
                data: bench,
                showSymbol: false,
                lineStyle: { width: 1.5, color: '#1677ff', type: 'dashed' as const },
                xAxisIndex: 0,
                yAxisIndex: 0,
              },
            ]
          : []),
        {
          name: '回撤',
          type: 'line',
          data: drawdowns,
          showSymbol: false,
          areaStyle: { color: 'rgba(250,173,20,0.25)' },
          lineStyle: { color: '#faad14' },
          xAxisIndex: 1,
          yAxisIndex: 1,
        },
      ],
    }
  }, [result])

  /** 月度收益热力图（用柱状图简化展示） */
  const monthlyOption = useMemo<echarts.EChartsOption>(() => {
    const rows = result?.monthly_returns ?? []
    if (!rows.length) return {}
    const labels: string[] = []
    const values: number[] = []
    rows.forEach((row) => {
      Object.entries(row).forEach(([key, value]) => {
        if (key === '年份' || key === 'year') return
        if (value === null || value === undefined) return
        labels.push(`${row['年份'] ?? row['year']}-${key}`)
        values.push(Number(value))
      })
    })
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 50, right: 20, top: 20, bottom: 60 },
      xAxis: { type: 'category', data: labels, axisLabel: { rotate: 60, fontSize: 10 } },
      yAxis: { type: 'value', name: '%' },
      series: [
        {
          type: 'bar',
          data: values.map((v) => ({
            value: v,
            itemStyle: { color: v >= 0 ? '#cf1322' : '#3f8600' },
          })),
        },
      ],
    }
  }, [result])

  const tradeColumns: ColumnsType<TradeRow> = [
    { title: '日期', dataIndex: 'date', width: 110, sorter: (a, b) => a.date.localeCompare(b.date) },
    { title: '代码', dataIndex: 'code', width: 110 },
    { title: '名称', dataIndex: 'name', width: 110 },
    {
      title: '方向',
      dataIndex: 'direction_text',
      width: 80,
      render: (v: string) => <Tag color={v === '买入' ? 'red' : 'green'}>{v}</Tag>,
    },
    { title: '成交价', dataIndex: 'price', width: 90, render: (v: number) => v.toFixed(3) },
    { title: '数量', dataIndex: 'shares', width: 90 },
    { title: '金额', dataIndex: 'amount', width: 120, render: (v: number) => v.toFixed(2) },
    { title: '手续费', dataIndex: 'commission', width: 90, render: (v: number) => v.toFixed(2) },
    { title: '印花税', dataIndex: 'stamp_tax', width: 90, render: (v: number) => v.toFixed(2) },
    {
      title: '盈亏',
      dataIndex: 'pnl',
      width: 110,
      render: (v: number, r) =>
        r.direction === 'buy' ? (
          '-'
        ) : (
          <span style={{ color: v > 0 ? '#cf1322' : v < 0 ? '#3f8600' : undefined }}>{v.toFixed(2)}</span>
        ),
    },
    {
      title: '盈亏%',
      dataIndex: 'pnl_pct',
      width: 90,
      render: (v: number, r) =>
        r.direction === 'buy' ? '-' : `${v.toFixed(2)}%`,
    },
    { title: '持仓天数', dataIndex: 'hold_days', width: 90 },
    { title: '原因', dataIndex: 'reason', ellipsis: true },
  ]

  const positionColumns: ColumnsType<PositionRow> = [
    { title: '日期', dataIndex: 'date', width: 110 },
    { title: '代码', dataIndex: 'code', width: 110 },
    { title: '名称', dataIndex: 'name', width: 120 },
    { title: '持股', dataIndex: 'shares', width: 100 },
    { title: '成本', dataIndex: 'avg_cost', width: 90, render: (v: number) => v.toFixed(3) },
    { title: '现价', dataIndex: 'price', width: 90, render: (v: number) => v.toFixed(3) },
    { title: '市值', dataIndex: 'market_value', width: 120, render: (v: number) => v.toFixed(2) },
    {
      title: '浮盈',
      dataIndex: 'unrealized_pnl',
      width: 110,
      render: (v: number) => (
        <span style={{ color: v > 0 ? '#cf1322' : v < 0 ? '#3f8600' : undefined }}>{v.toFixed(2)}</span>
      ),
    },
    {
      title: '浮盈%',
      dataIndex: 'unrealized_pct',
      width: 90,
      render: (v: number) => `${v.toFixed(2)}%`,
    },
    {
      title: '权重',
      dataIndex: 'weight',
      width: 90,
      render: (v: number) => `${(v * 100).toFixed(2)}%`,
    },
  ]

  const rejectedColumns: ColumnsType<BacktestResult['rejected'][number]> = [
    { title: '日期', dataIndex: 'date', width: 120 },
    { title: '代码', dataIndex: 'code', width: 130 },
    { title: '方向', dataIndex: 'direction', width: 90 },
    { title: '未成交原因', dataIndex: 'reason' },
  ]

  const metrics = result?.metrics ?? {}
  const labels = result?.metric_labels ?? {}

  const labelOf = (key: string) => labels[key] ?? key

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card title={<Title level={4} style={{ margin: 0 }}>回测</Title>}>
        <Row gutter={16}>
          <Col xs={24} lg={8}>
            <Card size="small" type="inner" title="策略与仓位规则">
              <StrategyParamsForm
                strategies={strategies as any}
                value={strategy}
                params={params}
                onChange={(name, next) => {
                  setStrategy(name)
                  setParams(next)
                }}
              />
              <Divider style={{ margin: '12px 0' }} />
              <Space direction="vertical" style={{ width: '100%' }}>
                <div>
                  <Text type="secondary">初始资金</Text>
                  <InputNumber
                    style={{ width: '100%' }}
                    min={1000}
                    step={100000}
                    value={cash}
                    onChange={(v) => setCash(Number(v ?? 1_000_000))}
                  />
                </div>
                <div>
                  <Text type="secondary">最大持仓数量</Text>
                  <InputNumber
                    style={{ width: '100%' }}
                    min={1}
                    max={200}
                    value={maxPositions}
                    onChange={(v) => setMaxPositions(Number(v ?? 10))}
                  />
                </div>
                <div>
                  <Text type="secondary">仓位规则</Text>
                  <Select
                    style={{ width: '100%' }}
                    value={sizing}
                    onChange={setSizing}
                    options={[
                      { value: 'equal', label: '等权（总权益 / 最大持仓数）' },
                      { value: 'fixed', label: '固定资金' },
                    ]}
                  />
                </div>
                {sizing === 'fixed' ? (
                  <div>
                    <Text type="secondary">每笔固定金额</Text>
                    <InputNumber
                      style={{ width: '100%' }}
                      min={1000}
                      step={10000}
                      value={fixedAmount}
                      onChange={(v) => setFixedAmount(Number(v ?? 100_000))}
                    />
                  </div>
                ) : null}
              </Space>
            </Card>
          </Col>

          <Col xs={24} lg={8}>
            <Card size="small" type="inner" title="区间与股票池">
              <Space direction="vertical" style={{ width: '100%' }}>
                <div>
                  <Text type="secondary">回测区间</Text>
                  <DatePicker.RangePicker
                    style={{ width: '100%' }}
                    value={range}
                    onChange={(v) => v && v[0] && v[1] && setRange([v[0], v[1]])}
                    allowClear={false}
                  />
                </div>
                <div>
                  <Text type="secondary">股票池（留空=本地全部 A 股）</Text>
                  <Select
                    mode="tags"
                    style={{ width: '100%' }}
                    placeholder="600000.SH,000001.SZ"
                    value={universeText ? universeText.split(/[,\s]+/).filter(Boolean) : []}
                    onChange={(v) => setUniverseText(v.join(','))}
                    tokenSeparators={[',', ' ']}
                  />
                </div>
                <div>
                  <Text type="secondary">股票池数量上限</Text>
                  <InputNumber
                    style={{ width: '100%' }}
                    min={1}
                    value={maxUniverse ?? undefined}
                    onChange={(v) => setMaxUniverse((v as number) ?? null)}
                    placeholder="留空=全部（数量大时耗时较长）"
                  />
                </div>
              </Space>
            </Card>
          </Col>

          <Col xs={24} lg={8}>
            <Card size="small" type="inner" title="费用与基准">
              <Space direction="vertical" style={{ width: '100%' }}>
                <div>
                  <Text type="secondary">佣金费率</Text>
                  <InputNumber
                    style={{ width: '100%' }}
                    min={0}
                    step={0.0001}
                    value={commission}
                    onChange={(v) => setCommission(Number(v ?? 0))}
                  />
                </div>
                <div>
                  <Text type="secondary">印花税（仅卖出）</Text>
                  <InputNumber
                    style={{ width: '100%' }}
                    min={0}
                    step={0.0001}
                    value={stampTax}
                    onChange={(v) => setStampTax(Number(v ?? 0))}
                  />
                </div>
                <div>
                  <Text type="secondary">滑点率</Text>
                  <InputNumber
                    style={{ width: '100%' }}
                    min={0}
                    step={0.0001}
                    value={slippage}
                    onChange={(v) => setSlippage(Number(v ?? 0))}
                  />
                </div>
                <div>
                  <Text type="secondary">成交时点</Text>
                  <Select
                    style={{ width: '100%' }}
                    value={execPrice}
                    onChange={setExecPrice}
                    options={[
                      { value: 'next_open', label: 'T+1 开盘（推荐）' },
                      { value: 'next_close', label: 'T+1 收盘' },
                      { value: 'close', label: 'T 日收盘' },
                    ]}
                  />
                </div>
                <div>
                  <Text type="secondary">基准指数</Text>
                  <Select
                    style={{ width: '100%' }}
                    value={benchmark || undefined}
                    onChange={(v) => setBenchmark(v ?? '')}
                    allowClear
                    options={benchmarks.map((b) => ({ value: b, label: b }))}
                  />
                </div>
              </Space>
            </Card>
          </Col>
        </Row>

        <Space style={{ marginTop: 16 }}>
          <Button type="primary" icon={<PlayCircleOutlined />} loading={loading} onClick={handleRun}>
            运行回测
          </Button>
          <Button
            icon={<StarOutlined />}
            disabled={!result}
            onClick={async () => {
              try {
                const stats = await api.backtestToWatchlist({ group: '回测持仓' })
                message.success(`导入关注池 ${stats.added} 只`)
              } catch (error) {
                message.error((error as Error).message)
              }
            }}
          >
            期末持仓导入关注池
          </Button>
        </Space>

        {result ? (
          <Row gutter={[12, 12]} style={{ marginTop: 16 }}>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title={labelOf('total_return')} value={metrics.total_return} percent colorBySign />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title={labelOf('annual_return')} value={metrics.annual_return} percent colorBySign />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title={labelOf('max_drawdown')} value={metrics.max_drawdown} percent colorBySign invert />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title={labelOf('sharpe')} value={metrics.sharpe} />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title={labelOf('sortino')} value={metrics.sortino} />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title={labelOf('calmar')} value={metrics.calmar} />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title={labelOf('win_rate')} value={metrics.win_rate} percent />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title={labelOf('profit_loss_ratio')} value={metrics.profit_loss_ratio} />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title={labelOf('trade_count')} value={metrics.trade_count} precision={0} suffix="笔" />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title={labelOf('turnover')} value={metrics.turnover} percent />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title={labelOf('benchmark_return')} value={metrics.benchmark_return} percent colorBySign />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title={labelOf('excess_return')} value={metrics.excess_return} percent colorBySign />
            </Col>
          </Row>
        ) : null}
      </Card>

      {result ? (
        <>
          <Card title="净值与回撤曲线">
            <EChart option={equityOption} height={460} loading={loading} />
          </Card>

          <Row gutter={[16, 16]}>
            <Col xs={24} lg={14}>
              <Card title="月度收益（%）">
                {result.monthly_returns?.length ? (
                  <EChart option={monthlyOption} height={300} />
                ) : (
                  <Empty description="回测区间过短，无月度数据" />
                )}
              </Card>
            </Col>
            <Col xs={24} lg={10}>
              <Card title="交易统计">
                <Table
                  size="small"
                  rowKey="key"
                  pagination={false}
                  columns={[
                    { title: '项目', dataIndex: 'key', width: 160 },
                    { title: '数值', dataIndex: 'value' },
                  ]}
                  dataSource={[
                    { key: '平仓次数', value: result.trade_stats.closed_trade_count ?? 0 },
                    { key: '盈利次数', value: result.trade_stats.win_count ?? 0 },
                    { key: '亏损次数', value: result.trade_stats.loss_count ?? 0 },
                    { key: '平均盈利', value: Number(result.trade_stats.avg_win ?? 0).toFixed(2) },
                    { key: '平均亏损', value: Number(result.trade_stats.avg_loss ?? 0).toFixed(2) },
                    { key: '最大单笔盈利', value: Number(result.trade_stats.max_win ?? 0).toFixed(2) },
                    { key: '最大单笔亏损', value: Number(result.trade_stats.max_loss ?? 0).toFixed(2) },
                    { key: '平均持仓天数', value: Number(result.trade_stats.avg_hold_days ?? 0).toFixed(1) },
                    { key: '累计手续费', value: Number(result.trade_stats.total_commission ?? 0).toFixed(2) },
                    { key: '累计印花税', value: Number(result.trade_stats.total_stamp_tax ?? 0).toFixed(2) },
                    { key: '累计滑点成本', value: Number(result.trade_stats.total_slippage ?? 0).toFixed(2) },
                    { key: '未成交笔数', value: metrics.rejected_count ?? 0 },
                  ]}
                />
              </Card>
            </Col>
          </Row>

          <Card
            title="明细"
            extra={
              <Space>
                <Button
                  size="small"
                  icon={<DownloadOutlined />}
                  onClick={async () => {
                    try {
                      const text = await api.downloadText(`/api/backtest/export?fmt=csv&kind=trades`)
                      downloadText('backtest_trades.csv', '\uFEFF' + text, 'text/csv')
                    } catch (e) {
                      message.error((e as Error).message)
                    }
                  }}
                >
                  交易 CSV
                </Button>
                <Button
                  size="small"
                  icon={<DownloadOutlined />}
                  onClick={async () => {
                    try {
                      const text = await api.downloadText(`/api/backtest/export?fmt=csv&kind=positions`)
                      downloadText('backtest_positions.csv', '\uFEFF' + text, 'text/csv')
                    } catch (e) {
                      message.error((e as Error).message)
                    }
                  }}
                >
                  持仓 CSV
                </Button>
                <Button
                  size="small"
                  icon={<DownloadOutlined />}
                  onClick={async () => {
                    try {
                      const text = await api.downloadText(`/api/backtest/export?fmt=html`)
                      downloadText('backtest_report.html', text, 'text/html')
                    } catch (e) {
                      message.error((e as Error).message)
                    }
                  }}
                >
                  HTML 报告
                </Button>
                <Button
                  size="small"
                  icon={<DownloadOutlined />}
                  onClick={() => downloadText('backtest_report.md', result.markdown, 'text/markdown')}
                >
                  Markdown 报告
                </Button>
              </Space>
            }
          >
            <Tabs
              items={[
                {
                  key: 'trades',
                  label: `交易明细（${result.trades.length}）`,
                  children: (
                    <Table
                      size="small"
                      rowKey={(r, i) => `${r.date}-${r.code}-${r.direction}-${i}`}
                      columns={tradeColumns}
                      dataSource={result.trades}
                      pagination={{ pageSize: 12, showSizeChanger: true }}
                      scroll={{ x: 1500 }}
                      locale={{ emptyText: <Empty description="回测期间没有产生成交" /> }}
                    />
                  ),
                },
                {
                  key: 'positions',
                  label: `每日持仓（${result.positions.length}）`,
                  children: (
                    <Table
                      size="small"
                      rowKey={(r, i) => `${r.date}-${r.code}-${i}`}
                      columns={positionColumns}
                      dataSource={result.positions}
                      pagination={{ pageSize: 12, showSizeChanger: true }}
                      scroll={{ x: 1200 }}
                      locale={{ emptyText: <Empty description="无持仓记录" /> }}
                    />
                  ),
                },
                {
                  key: 'rejected',
                  label: `未成交（${result.rejected.length}）`,
                  children: (
                    <Table
                      size="small"
                      rowKey={(r, i) => `${r.date}-${r.code}-${r.direction}-${i}`}
                      columns={rejectedColumns}
                      dataSource={result.rejected}
                      pagination={{ pageSize: 12 }}
                      locale={{ emptyText: <Empty description="无未成交记录" /> }}
                    />
                  ),
                },
                {
                  key: 'metrics',
                  label: '绩效明细',
                  children: (
                    <Table
                      size="small"
                      rowKey="key"
                      pagination={false}
                      columns={[
                        { title: '指标', dataIndex: 'key', width: 220 },
                        { title: '数值', dataIndex: 'value' },
                      ]}
                      dataSource={Object.entries(metrics).map(([k, v]) => ({
                        key: labelOf(k),
                        value:
                          v === null || v === undefined
                            ? '-'
                            : ['total_return', 'annual_return', 'max_drawdown', 'annual_volatility',
                               'win_rate', 'turnover', 'benchmark_return', 'benchmark_annual_return',
                               'excess_return', 'alpha'].includes(k)
                              ? `${(Number(v) * 100).toFixed(2)}%`
                              : Number(v).toFixed(4),
                      }))}
                    />
                  ),
                },
              ]}
            />
          </Card>

          <MarkdownView
            content={result.markdown}
            title="回测报告"
            filename="backtest_report"
            subtitle={`${result.strategy_name}　${result.start} ~ ${result.end}　股票池 ${result.universe_size} 只`}
          />
        </>
      ) : (
        <Card>
          <Empty description="尚未运行回测。请配置左侧参数后点击「运行回测」。" />
        </Card>
      )}
    </Space>
  )
}
