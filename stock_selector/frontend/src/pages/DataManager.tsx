/**
 * 数据管理页面。
 *
 * 功能：设置通达信目录、扫描股票池、读取日线/分钟线、查看缓存状态与更新时间、数据质量检查。
 */
import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  ClearOutlined,
  CloudDownloadOutlined,
  DatabaseOutlined,
  FolderOpenOutlined,
  ReloadOutlined,
  ScanOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { api } from '@/api'
import type { BasicsItem, CacheInfo, DataStatus, LoadResult, QualityData, StockRow } from '@/api'

const { Title, Text } = Typography

const FREQ_OPTIONS = [
  { value: 'daily', label: '日线（.day）' },
  { value: '1min', label: '1 分钟（.lc1）' },
  { value: '5min', label: '5 分钟（.lc5）' },
  { value: '15min', label: '15 分钟（由 5 分钟聚合）' },
  { value: '30min', label: '30 分钟（由 5 分钟聚合）' },
  { value: '60min', label: '60 分钟（由 5 分钟聚合）' },
]

export default function DataManager() {
  const [status, setStatus] = useState<DataStatus | null>(null)
  const [stocks, setStocks] = useState<StockRow[]>([])
  const [cache, setCache] = useState<CacheInfo | null>(null)
  const [quality, setQuality] = useState<QualityData | null>(null)
  const [loadResult, setLoadResult] = useState<LoadResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState<string>('')
  // 股票基础信息（流通股本）——供「小盘放量」等需要流通盘的策略使用
  const [basicsItems, setBasicsItems] = useState<BasicsItem[]>([])
  const [basicsInput, setBasicsInput] = useState('')
  const [basicsKeyword, setBasicsKeyword] = useState('')
  const [basicsLoading, setBasicsLoading] = useState(false)

  const [tdxDir, setTdxDir] = useState('')
  const [keyword, setKeyword] = useState('')
  const [loadForm] = Form.useForm()

  const refreshStatus = async () => {
    try {
      const data = await api.dataStatus()
      setStatus(data)
      setTdxDir(data.tdx_dir)
    } catch (error) {
      message.error(`获取数据状态失败：${(error as Error).message}`)
    }
  }

  const refreshCache = async () => {
    try {
      setCache(await api.cacheInfo(200))
    } catch (error) {
      message.error(`获取缓存信息失败：${(error as Error).message}`)
    }
  }

  useEffect(() => {
    refreshStatus()
    refreshCache()
    refreshBasics()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleValidateDir = async () => {
    if (!tdxDir) {
      message.warning('请输入通达信目录')
      return
    }
    setBusy('validate')
    try {
      const result = await api.checkTdxDir(tdxDir)
      if (result.valid) {
        message.success(`目录有效，vipdoc：${result.vipdoc}`)
      } else {
        message.error('目录无效：未找到 vipdoc 子目录（需包含 vipdoc/sh/lday 等）')
      }
    } catch (error) {
      message.error((error as Error).message)
    } finally {
      setBusy('')
    }
  }

  const handleSaveDir = async () => {
    setBusy('save')
    try {
      const data = await api.setTdxDir(tdxDir, true)
      setStatus(data)
      message.success('通达信目录已保存')
    } catch (error) {
      message.error(`保存失败：${(error as Error).message}`)
    } finally {
      setBusy('')
    }
  }

  const handleScan = async () => {
    setBusy('scan')
    setLoading(true)
    try {
      const data = await api.listStocks({
        include_index: true,
        limit: 2000,
        keyword: keyword || undefined,
      })
      setStocks(data.stocks)
      message.success(`扫描到 ${data.count} 个标的`)
    } catch (error) {
      message.error(`扫描失败：${(error as Error).message}`)
    } finally {
      setBusy('')
      setLoading(false)
    }
  }

  const handleLoad = async () => {
    const values = await loadForm.validateFields()
    setBusy('load')
    setLoading(true)
    try {
      const codes = values.codes
        ? String(values.codes)
            .split(/[,\s]+/)
            .filter(Boolean)
        : undefined
      const result = await api.loadData({
        codes,
        freq: values.freq,
        start: values.start || undefined,
        end: values.end || undefined,
        limit: values.limit || undefined,
        force: Boolean(values.force),
        tdx_dir: values.tdx_dir || undefined,
      })
      setLoadResult(result)
      message.success(result.message)
      await refreshCache()
    } catch (error) {
      message.error(`读取失败：${(error as Error).message}`)
    } finally {
      setBusy('')
      setLoading(false)
    }
  }

  const handleQuality = async () => {
    setBusy('quality')
    try {
      setQuality(await api.qualityCheck({ max_codes: 200 }))
      message.success('数据质量检查完成')
    } catch (error) {
      message.error(`检查失败：${(error as Error).message}`)
    } finally {
      setBusy('')
    }
  }

  const handleClearCache = async () => {
    setBusy('clear')
    try {
      const result = await api.clearCache()
      message.success(`已删除 ${result.removed} 个缓存文件`)
      await refreshCache()
    } catch (error) {
      message.error(`清理失败：${(error as Error).message}`)
    } finally {
      setBusy('')
    }
  }

  const handleNamesTemplate = async () => {
    try {
      const result = await api.createNamesTemplate()
      message.success(`名称映射模板已生成：${result.path}`)
    } catch (error) {
      message.error((error as Error).message)
    }
  }

  // ---------------- 股票基础信息（流通股本） ----------------
  const refreshBasics = async (keyword = basicsKeyword) => {
    setBasicsLoading(true)
    try {
      const data = await api.listBasics({ keyword: keyword || undefined, limit: 200 })
      setBasicsItems(data.items)
    } catch (error) {
      message.error(`获取股票基础信息失败：${(error as Error).message}`)
    } finally {
      setBasicsLoading(false)
    }
  }

  const handleBasicsTemplate = async () => {
    try {
      const result = await api.createBasicsTemplate()
      message.success(`基础信息模板已生成：${result.path}（填写 float_shares 列后可用「导入」或直接放到缓存目录）`)
      await refreshStatus()
      await refreshBasics()
    } catch (error) {
      message.error((error as Error).message)
    }
  }

  /** 解析 "600000.SH=65.7亿" 或 "600000.SH" 起头的多行输入 */
  const handleBasicsSave = async () => {
    const text = basicsInput.trim()
    if (!text) {
      message.warning('请输入形如 600000.SH=65.7亿 的流通股本')
      return
    }
    const basics: Record<string, unknown> = {}
    const bad: string[] = []
    text.split(/[\n;]+/).forEach((raw) => {
      const line = raw.trim()
      if (!line) return
      const [codePart, ...rest] = line.split(/[=,:，]+/)
      const code = codePart.trim()
      const value = rest.join('').trim()
      if (!code || !value) {
        bad.push(line)
        return
      }
      basics[code] = value
    })
    if (!Object.keys(basics).length) {
      message.error(`未能解析出有效记录：${bad.join('；')}`)
      return
    }
    setBusy('basics')
    try {
      const result = await api.updateBasics(basics)
      message.success(`已写入 ${Object.keys(basics).length} 条（当前共 ${result.count} 条）`)
      setBasicsInput('')
      await refreshStatus()
      await refreshBasics()
    } catch (error) {
      message.error(`写入失败：${(error as Error).message}`)
    } finally {
      setBusy('')
    }
  }

  const basicsColumns: ColumnsType<BasicsItem> = [
    { title: '代码', dataIndex: 'code', width: 130 },
    { title: '名称', dataIndex: 'name', width: 140 },
    {
      title: '流通股本（亿股）',
      dataIndex: 'float_shares_yi',
      width: 150,
      render: (v: number | null) => (v == null ? <Tag color="red">未填写</Tag> : v.toFixed(2)),
    },
    { title: '行业', dataIndex: 'industry', width: 120 },
    {
      title: '总股本（亿股）',
      dataIndex: 'total_shares',
      width: 130,
      render: (v: number | null) => (v == null ? '-' : (v / 1e8).toFixed(2)),
    },
  ]

  const stockColumns: ColumnsType<StockRow> = [
    { title: '代码', dataIndex: 'code', width: 120, sorter: (a, b) => a.code.localeCompare(b.code) },
    { title: '名称', dataIndex: 'name', width: 140 },
    { title: '市场', dataIndex: 'market_name', width: 90 },
    {
      title: '类型',
      dataIndex: 'is_index',
      width: 80,
      render: (v: boolean) => (v ? <Tag color="blue">指数</Tag> : <Tag>股票</Tag>),
    },
    { title: '文件', dataIndex: 'file', width: 160 },
    {
      title: '大小',
      dataIndex: 'size',
      width: 110,
      sorter: (a, b) => a.size - b.size,
      render: (v: number) => `${(v / 1024).toFixed(1)} KB`,
    },
    { title: '文件更新时间', dataIndex: 'mtime', width: 180 },
  ]

  const cacheColumns: ColumnsType<Record<string, unknown>> = [
    { title: '代码', dataIndex: 'code', width: 120 },
    { title: '周期', dataIndex: 'freq', width: 90 },
    { title: '行数', dataIndex: 'rows', width: 90 },
    { title: '起始', dataIndex: 'start', width: 180 },
    { title: '结束', dataIndex: 'end', width: 180 },
    { title: '复权', dataIndex: 'adjust', width: 80 },
    { title: '缓存时间', dataIndex: 'saved_at', width: 170 },
  ]

  const qualityColumns: ColumnsType<QualityData['issues'][number]> = [
    { title: '代码', dataIndex: 'code', width: 130 },
    {
      title: '级别',
      dataIndex: 'level',
      width: 100,
      render: (v: string) =>
        v === 'error' ? <Tag color="red">错误</Tag> : v === 'warning' ? <Tag color="orange">警告</Tag> : <Tag>提示</Tag>,
    },
    { title: '问题描述', dataIndex: 'message' },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        title={
          <Space>
            <DatabaseOutlined />
            <Title level={4} style={{ margin: 0 }}>数据管理</Title>
          </Space>
        }
        extra={
          <Button icon={<ReloadOutlined />} onClick={() => { refreshStatus(); refreshCache() }}>
            刷新
          </Button>
        }
      >
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={12}>
            <Card size="small" title="通达信数据目录" type="inner">
              <Space direction="vertical" style={{ width: '100%' }}>
                <Input
                  value={tdxDir}
                  onChange={(e) => setTdxDir(e.target.value)}
                  placeholder="例如 C:/new_tdx（需包含 vipdoc 子目录）"
                  prefix={<FolderOpenOutlined />}
                  allowClear
                />
                <Space>
                  <Button
                    onClick={handleValidateDir}
                    loading={busy === 'validate'}
                  >
                    检测目录
                  </Button>
                  <Button type="primary" onClick={handleSaveDir} loading={busy === 'save'}>
                    保存并生效
                  </Button>
                </Space>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  目录下应存在 vipdoc/sh/lday、vipdoc/sz/lday、vipdoc/bj/lday。
                  日线从 .day 读取，分钟线从 minline/.lc1 或 fzline/.lc5 读取。
                </Text>
              </Space>
            </Card>
          </Col>
          <Col xs={24} lg={12}>
            <Card size="small" title="数据源状态" type="inner">
              <Descriptions column={2} size="small">
                <Descriptions.Item label="就绪">
                  {status?.ready ? <Tag color="green">是</Tag> : <Tag color="red">否</Tag>}
                </Descriptions.Item>
                <Descriptions.Item label="文件总数">{status?.total_files ?? 0}</Descriptions.Item>
                <Descriptions.Item label="沪市">{status?.markets?.sh ?? 0}</Descriptions.Item>
                <Descriptions.Item label="深市">{status?.markets?.sz ?? 0}</Descriptions.Item>
                <Descriptions.Item label="北交所">{status?.markets?.bj ?? 0}</Descriptions.Item>
                <Descriptions.Item label="最新数据日">
                  {status?.last_data_date || '-'}
                </Descriptions.Item>
                <Descriptions.Item label="pytdx">
                  {status?.pytdx?.available ? (
                    <Tag color="green">可用</Tag>
                  ) : (
                    <Tag color="orange">不可用</Tag>
                  )}
                </Descriptions.Item>
                <Descriptions.Item label="复权模式">
                  <Tag color={status?.adjust === 'none' ? 'default' : 'orange'}>
                    {status?.adjust}
                  </Tag>
                </Descriptions.Item>
                <Descriptions.Item label="缓存格式">{status?.cache?.format}</Descriptions.Item>
                <Descriptions.Item label="缓存目录" span={2}>
                  <Text code style={{ fontSize: 12 }}>{status?.cache?.dir}</Text>
                </Descriptions.Item>
                <Descriptions.Item label="名称映射" span={2}>
                  {status?.names?.available ? (
                    <Text>{status.names.count} 条（{status.names.file}）</Text>
                  ) : (
                    <Space>
                      <Text type="secondary">未配置</Text>
                      <Button size="small" onClick={handleNamesTemplate}>
                        生成模板
                      </Button>
                    </Space>
                  )}
                </Descriptions.Item>
              </Descriptions>
              {status?.pytdx && !status.pytdx.available ? (
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginTop: 8 }}
                  message="pytdx 不可用"
                  description={`当前使用内置备用解析器读取 .day 文件。建议安装 pytdx：pip install pytdx。错误信息：${status.pytdx.error ?? '无'}`}
                />
              ) : null}
            </Card>
          </Col>
        </Row>

        {status?.adjust_warning ? (
          <Alert
            type="info"
            showIcon
            style={{ marginTop: 16 }}
            message="复权提示"
            description={status.adjust_warning}
          />
        ) : (
          <Alert
            type="success"
            showIcon
            style={{ marginTop: 16 }}
            message="复权说明：本地通达信日线为不复权数据"
            description="系统默认返回不复权价格。如需前复权/后复权，请在 config.yaml 的 data.adjust 中选择模式，并通过 data.adjust_file 提供复权因子 CSV；若未提供，系统会明确告警并返回原始数据，绝不静默使用错误的复权结果。"
          />
        )}
      </Card>

      <Card
        title={
          <Space>
            <CloudDownloadOutlined />
            <span>读取行情数据</span>
          </Space>
        }
      >
        <Form form={loadForm} layout="inline" initialValues={{ freq: 'daily', force: false }}>
          <Form.Item name="codes" label="股票代码">
            <Input style={{ width: 260 }} placeholder="留空=扫描全部，如 600000.SH,000001.SZ" />
          </Form.Item>
          <Form.Item name="freq" label="周期">
            <Select style={{ width: 220 }} options={FREQ_OPTIONS} />
          </Form.Item>
          <Form.Item name="start" label="起始">
            <Input style={{ width: 130 }} placeholder="2022-01-01" />
          </Form.Item>
          <Form.Item name="end" label="结束">
            <Input style={{ width: 130 }} placeholder="2024-12-31" />
          </Form.Item>
          <Form.Item name="limit" label="数量上限">
            <InputNumber min={1} style={{ width: 110 }} placeholder="全部" />
          </Form.Item>
          <Form.Item name="force" label="强制重读" valuePropName="checked">
            <Select
              style={{ width: 90 }}
              options={[
                { value: false, label: '否' },
                { value: true, label: '是' },
              ]}
            />
          </Form.Item>
          <Form.Item>
            <Button type="primary" onClick={handleLoad} loading={busy === 'load'}>
              开始读取
            </Button>
          </Form.Item>
        </Form>

        {loadResult ? (
          <Row gutter={12} style={{ marginTop: 16 }}>
            <Col span={6}><Statistic title="待处理" value={loadResult.total} /></Col>
            <Col span={6}><Statistic title="成功" value={loadResult.loaded} valueStyle={{ color: '#3f8600' }} /></Col>
            <Col span={6}><Statistic title="失败" value={loadResult.failed} valueStyle={{ color: '#cf1322' }} /></Col>
            <Col span={6}><Statistic title="总记录数" value={loadResult.rows} /></Col>
          </Row>
        ) : null}
      </Card>

      <Card
        title={
          <Space>
            <ScanOutlined />
            <span>股票池</span>
          </Space>
        }
        extra={
          <Space>
            <Input
              placeholder="代码或名称"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              onPressEnter={handleScan}
              style={{ width: 180 }}
              allowClear
            />
            <Button type="primary" onClick={handleScan} loading={busy === 'scan'}>
              扫描本地股票池
            </Button>
          </Space>
        }
      >
        <Table
          size="small"
          rowKey="code"
          loading={loading}
          columns={stockColumns}
          dataSource={stocks}
          pagination={{ pageSize: 10, showSizeChanger: true, showTotal: (t) => `共 ${t} 条` }}
          locale={{ emptyText: <Empty description="点击「扫描本地股票池」获取标的列表" /> }}
          scroll={{ x: 900 }}
        />
      </Card>

      <Card
        title={
          <Space>
            <DatabaseOutlined />
            <span>股票基础信息（流通股本）</span>
          </Space>
        }
        extra={
          <Space>
            <Input
              placeholder="代码或名称"
              value={basicsKeyword}
              onChange={(e) => setBasicsKeyword(e.target.value)}
              onPressEnter={() => refreshBasics()}
              style={{ width: 170 }}
              allowClear
            />
            <Button onClick={handleBasicsTemplate}>生成模板</Button>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => refreshBasics()}
              loading={basicsLoading}
            >
              刷新
            </Button>
          </Space>
        }
      >
        {status?.basics?.warning ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="流通盘类策略缺少股本数据"
            description={status.basics.warning}
          />
        ) : null}
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={10}>
            <Descriptions column={1} size="small">
              <Descriptions.Item label="状态">
                {status?.basics?.available ? (
                  <Tag color="green">已配置</Tag>
                ) : (
                  <Tag color="orange">未配置</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="记录数">{status?.basics?.count ?? 0}</Descriptions.Item>
              <Descriptions.Item label="文件">
                <Text code style={{ fontSize: 12 }}>{status?.basics?.file || '-'}</Text>
              </Descriptions.Item>
              <Descriptions.Item label="依赖策略">
                {(status?.basics?.strategies_requiring_basics ?? []).join('、') || '-'}
              </Descriptions.Item>
            </Descriptions>
            <Space direction="vertical" style={{ width: '100%', marginTop: 8 }}>
              <Input.TextArea
                rows={3}
                value={basicsInput}
                onChange={(e) => setBasicsInput(e.target.value)}
                placeholder={'每行一条，单位默认「股」，支持中文单位：\n600000.SH=65.7亿\n830000.BJ=3500万'}
              />
              <Button type="primary" onClick={handleBasicsSave} loading={busy === 'basics'}>
                保存流通股本
              </Button>
              <Text type="secondary" style={{ fontSize: 12 }}>
                通达信 .day 文件不含股本信息，因此「小盘放量（volume_surge）」等流通盘策略
                需要在此维护流通股本；流通市值由「流通股本 × 当日收盘价」实时计算。
              </Text>
            </Space>
          </Col>
          <Col xs={24} lg={14}>
            <Table
              size="small"
              rowKey="code"
              loading={basicsLoading}
              columns={basicsColumns}
              dataSource={basicsItems}
              pagination={{ pageSize: 8, size: 'small' }}
              locale={{ emptyText: <Empty description="尚未配置流通股本" /> }}
              scroll={{ x: 640 }}
            />
          </Col>
        </Row>
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          <Card
            title={
              <Space>
                <DatabaseOutlined />
                <span>缓存状态（{cache?.count ?? 0} 项，{cache?.total_size_mb ?? 0} MB）</span>
              </Space>
            }
            extra={
              <Button danger icon={<ClearOutlined />} onClick={handleClearCache} loading={busy === 'clear'}>
                清空缓存
              </Button>
            }
          >
            <Table
              size="small"
              rowKey={(r) => `${r.code}-${r.freq}`}
              columns={cacheColumns}
              dataSource={cache?.entries ?? []}
              pagination={{ pageSize: 8, size: 'small' }}
              locale={{ emptyText: <Empty description="缓存为空" /> }}
              scroll={{ x: 800 }}
            />
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card
            title={
              <Space>
                <WarningOutlined />
                <span>数据质量检查</span>
              </Space>
            }
            extra={
              <Button onClick={handleQuality} loading={busy === 'quality'}>
                开始检查
              </Button>
            }
          >
            {quality ? (
              <>
                <Row gutter={12} style={{ marginBottom: 12 }}>
                  <Col span={8}><Statistic title="检查" value={quality.total} /></Col>
                  <Col span={8}><Statistic title="正常" value={quality.ok} valueStyle={{ color: '#3f8600' }} /></Col>
                  <Col span={8}>
                    <Statistic
                      title="有问题"
                      value={quality.problem_count}
                      valueStyle={{ color: quality.problem_count ? '#cf1322' : undefined }}
                    />
                  </Col>
                </Row>
                <Table
                  size="small"
                  rowKey={(r, i) => `${r.code}-${i}`}
                  columns={qualityColumns}
                  dataSource={quality.issues}
                  pagination={{ pageSize: 6, size: 'small' }}
                  locale={{ emptyText: <Empty description="未发现问题" /> }}
                />
              </>
            ) : (
              <Empty description="点击「开始检查」验证本地数据完整性" />
            )}
          </Card>
        </Col>
      </Row>
    </Space>
  )
}
