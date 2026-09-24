/**
 * 选股页面。
 *
 * 流程：选择策略 → 配置参数 → 选择日期与股票池 → 执行选股 → 表格展示 → 加入关注池 / 导出。
 */
import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  DatePicker,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  Radio,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { DownloadOutlined, PlayCircleOutlined, StarOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import dayjs, { Dayjs } from 'dayjs'
import { api, downloadText } from '@/api'
import type { ScreenItem, ScreenResult, StrategyMeta } from '@/api'
import StrategyParamsForm from '@/components/StrategyParamsForm'
import MarkdownView from '@/components/MarkdownView'

const { Title, Text } = Typography

export default function Screener() {
  const [strategies, setStrategies] = useState<StrategyMeta[]>([])
  const [strategy, setStrategy] = useState<string>('')
  const [params, setParams] = useState<Record<string, unknown>>({})
  const [result, setResult] = useState<ScreenResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [stockOptions, setStockOptions] = useState<Array<{ value: string; label: string }>>([])
  const [universe, setUniverse] = useState<string[] | undefined>(undefined)

  const [mode, setMode] = useState<'single' | 'range'>('single')
  const [singleDate, setSingleDate] = useState<Dayjs>(dayjs('2024-01-02'))
  const [range, setRange] = useState<[Dayjs, Dayjs]>([dayjs('2024-01-01'), dayjs('2024-03-31')])
  const [minBars, setMinBars] = useState(60)
  const [maxResults, setMaxResults] = useState(200)
  const [onlyBuy, setOnlyBuy] = useState(true)
  const [limitUniverse, setLimitUniverse] = useState<number | null>(null)

  useEffect(() => {
    ;(async () => {
      try {
        const meta = await api.screenMeta()
        setStrategies(meta.strategies)
        setStrategy(meta.default_strategy)
        const target = meta.strategies.find((s) => s.name === meta.default_strategy)
        setParams({ ...(target?.effective_params ?? target?.default_params ?? {}) })
        setMinBars(meta.defaults.min_bars)
        setMaxResults(meta.defaults.max_results)
      } catch (error) {
        message.error(`加载策略失败：${(error as Error).message}`)
      }
      try {
        const stocks = await api.listStocks({ include_index: false, limit: 3000 })
        setStockOptions(
          stocks.stocks.map((s) => ({ value: s.code, label: `${s.code} ${s.name ?? ''}`.trim() })),
        )
      } catch {
        // 未配置通达信目录时忽略
      }
    })()
  }, [])

  const handleRun = async () => {
    setLoading(true)
    try {
      const payload: Record<string, unknown> = {
        strategy,
        params,
        min_bars: minBars,
        max_results: maxResults,
        only_buy: onlyBuy,
        universe: universe && universe.length ? universe : undefined,
        limit_universe: limitUniverse || undefined,
      }
      if (mode === 'single') {
        payload.date = singleDate.format('YYYY-MM-DD')
      } else {
        payload.start = range[0].format('YYYY-MM-DD')
        payload.end = range[1].format('YYYY-MM-DD')
        payload.scan_all = true
      }
      const data = await api.runScreen(payload)
      setResult(data)
      message.success(`扫描 ${data.total_scanned} 只，命中 ${data.matched} 只，耗时 ${data.elapsed.toFixed(2)}s`)
      data.warnings?.forEach((w) => message.warning(w))
    } catch (error) {
      message.error(`选股失败：${(error as Error).message}`)
    } finally {
      setLoading(false)
    }
  }

  const handleAddToWatchlist = async () => {
    if (!result?.results?.length) {
      message.warning('没有可加入的结果')
      return
    }
    try {
      const stats = await api.screenToWatchlist({ group: '选股结果', strategy })
      message.success(`已加入关注池 ${stats.added} 只`)
    } catch (error) {
      message.error((error as Error).message)
    }
  }

  const handleExportCsv = () => {
    if (!result) return
    const headers = Object.keys(result.rows[0] ?? {})
    const lines = [
      headers.join(','),
      ...result.rows.map((row) =>
        headers
          .map((h) => {
            const value = row[h]
            if (value === null || value === undefined) return ''
            const text = String(value)
            return text.includes(',') || text.includes('"') ? `"${text.replace(/"/g, '""')}"` : text
          })
          .join(','),
      ),
    ]
    downloadText('screening_result.csv', '\uFEFF' + lines.join('\n'), 'text/csv')
  }

  const columns: ColumnsType<ScreenItem> = useMemo(() => {
    const factorKeys = Array.from(
      new Set((result?.results ?? []).flatMap((r) => Object.keys(r.factors ?? {}))),
    ).slice(0, 8)

    const base: ColumnsType<ScreenItem> = [
      { title: '代码', dataIndex: 'code', width: 116, fixed: 'left' },
      { title: '名称', dataIndex: 'name', width: 130, fixed: 'left' },
      { title: '日期', dataIndex: 'data_date', width: 110 },
      {
        title: '信号',
        dataIndex: 'signal',
        width: 90,
        render: (v: number) => (v > 0 ? <Tag color="red">买入</Tag> : <Tag color="green">卖出</Tag>),
      },
      {
        title: '新信号',
        dataIndex: 'is_new',
        width: 80,
        render: (v: boolean) => (v ? <Tag color="gold">是</Tag> : <Tag>否</Tag>),
      },
      {
        title: '收盘价',
        dataIndex: 'price',
        width: 100,
        render: (v: number | null) => (v === null ? '-' : v.toFixed(2)),
      },
      {
        title: '涨跌幅',
        dataIndex: 'pct_change',
        width: 100,
        sorter: (a, b) => (a.pct_change ?? 0) - (b.pct_change ?? 0),
        render: (v: number | null) =>
          v === null ? (
            '-'
          ) : (
            <span style={{ color: v > 0 ? '#cf1322' : v < 0 ? '#3f8600' : undefined }}>
              {(v * 100).toFixed(2)}%
            </span>
          ),
      },
    ]

    const factors: ColumnsType<ScreenItem> = factorKeys.map((key) => ({
      title: `因子·${key}`,
      dataIndex: ['factors', key],
      width: 130,
      render: (v: number | null) => (v === null || v === undefined ? '-' : Number(v).toFixed(4)),
    }))

    base.push(...factors)
    base.push({
      title: '触发原因',
      dataIndex: 'reason_text',
      width: 320,
      ellipsis: true,
    })
    return base
  }, [result])

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        title={<Title level={4} style={{ margin: 0 }}>选股</Title>}
        extra={<Text type="secondary">选股逻辑与回测、追踪完全共用同一套策略实现</Text>}
      >
        <Row gutter={16}>
          <Col xs={24} lg={8}>
            <Card size="small" type="inner" title="策略与参数">
              <StrategyParamsForm
                strategies={strategies}
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
                  <Text type="secondary">最小 K 线根数</Text>
                  <InputNumber
                    min={0}
                    value={minBars}
                    onChange={(v) => setMinBars(Number(v ?? 60))}
                    style={{ width: '100%' }}
                  />
                </div>
                <div>
                  <Text type="secondary">最多返回条数</Text>
                  <InputNumber
                    min={1}
                    value={maxResults}
                    onChange={(v) => setMaxResults(Number(v ?? 200))}
                    style={{ width: '100%' }}
                  />
                </div>
                <Space>
                  <Checkbox checked={onlyBuy} onChange={(e) => setOnlyBuy(e.target.checked)}>
                    只输出买入信号
                  </Checkbox>
                </Space>
                <div>
                  <Text type="secondary">股票池数量上限（调试）</Text>
                  <InputNumber
                    min={1}
                    value={limitUniverse ?? undefined}
                    onChange={(v) => setLimitUniverse((v as number) ?? null)}
                    style={{ width: '100%' }}
                    placeholder="留空=全部"
                  />
                </div>
              </Space>
            </Card>
          </Col>

          <Col xs={24} lg={16}>
            <Card size="small" type="inner" title="日期与股票池">
              <Radio.Group
                value={mode}
                onChange={(e) => setMode(e.target.value)}
                style={{ marginBottom: 12 }}
                optionType="button"
                buttonStyle="solid"
                options={[
                  { value: 'single', label: '指定日期' },
                  { value: 'range', label: '日期区间（逐日扫描）' },
                ]}
              />
              {mode === 'single' ? (
                <Space>
                  <Text>选股日期</Text>
                  <DatePicker
                    value={singleDate}
                    onChange={(d) => d && setSingleDate(d)}
                    allowClear={false}
                  />
                </Space>
              ) : (
                <Space>
                  <Text>区间</Text>
                  <DatePicker.RangePicker
                    value={range}
                    onChange={(v) => v && v[0] && v[1] && setRange([v[0], v[1]])}
                    allowClear={false}
                  />
                </Space>
              )}

              <div style={{ marginTop: 12 }}>
                <Text type="secondary">
                  股票池（留空表示扫描通达信本地目录中的全部 A 股）
                </Text>
                <Select
                  mode="multiple"
                  allowClear
                  showSearch
                  maxTagCount={8}
                  style={{ width: '100%', marginTop: 6 }}
                  placeholder="例如 600000.SH, 000001.SZ"
                  value={universe}
                  onChange={setUniverse}
                  options={stockOptions}
                  optionFilterProp="label"
                />
              </div>

              <Space style={{ marginTop: 16 }}>
                <Button
                  type="primary"
                  icon={<PlayCircleOutlined />}
                  loading={loading}
                  onClick={handleRun}
                >
                  执行选股
                </Button>
              </Space>

              {result ? (
                <Row gutter={12} style={{ marginTop: 16 }}>
                  <Col span={6}><Statistic title="扫描" value={result.total_scanned} /></Col>
                  <Col span={6}><Statistic title="命中" value={result.matched} valueStyle={{ color: '#cf1322' }} /></Col>
                  <Col span={6}><Statistic title="耗时(秒)" value={result.elapsed} precision={2} /></Col>
                  <Col span={6}><Statistic title="策略" value={result.strategy_name} /></Col>
                </Row>
              ) : null}
            </Card>
          </Col>
        </Row>
      </Card>

      {result?.conditions?.length ? (
        <Alert
          type="info"
          showIcon
          message="本次选股条件"
          description={
            <ul style={{ margin: 0, paddingLeft: 20 }}>
              {result.conditions.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          }
        />
      ) : null}

      <Card
        title={`选股结果（${result?.results?.length ?? 0}）`}
        extra={
          <Space>
            <Button icon={<StarOutlined />} onClick={handleAddToWatchlist} disabled={!result?.results?.length}>
              一键加入关注池
            </Button>
            <Button icon={<DownloadOutlined />} onClick={handleExportCsv} disabled={!result?.results?.length}>
              导出 CSV
            </Button>
          </Space>
        }
      >
        <Table
          size="small"
          rowKey={(r) => `${r.code}-${r.data_date}`}
          loading={loading}
          columns={columns}
          dataSource={result?.results ?? []}
          pagination={{ pageSize: 15, showSizeChanger: true, showTotal: (t) => `共 ${t} 条` }}
          locale={{ emptyText: <Empty description="尚无选股结果，请先在左侧配置并执行选股" /> }}
          scroll={{ x: 1400 }}
        />
      </Card>

      {result?.markdown ? (
        <MarkdownView
          content={result.markdown}
          title="选股报告"
          filename="screening_report"
          subtitle={`${result.strategy_name}　${result.end ?? result.start ?? ''}`}
        />
      ) : null}
    </Space>
  )
}
