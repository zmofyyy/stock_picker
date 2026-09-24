/**
 * 设置页面。
 *
 * 覆盖通达信目录、数据库路径、策略默认参数、追踪配置、通知配置、定时任务配置。
 * 保存后写回 config.yaml，并即时作用于后端服务。
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Divider,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  ApiOutlined,
  BellOutlined,
  DatabaseOutlined,
  FolderOpenOutlined,
  SaveOutlined,
  ScheduleOutlined,
  SettingOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { api } from '@/api'
import type { SettingsData, StrategyMeta } from '@/api'

const { Title, Text, Paragraph } = Typography

export default function Settings() {
  const [data, setData] = useState<SettingsData | null>(null)
  const [strategies, setStrategies] = useState<StrategyMeta[]>([])
  const [defaultStrategy, setDefaultStrategy] = useState('ma_cross')
  const [strategyParams, setStrategyParams] = useState<Record<string, Record<string, unknown>>>({})
  const [tdxInput, setTdxInput] = useState('')
  const [tdxCheck, setTdxCheck] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(false)

  const [dataForm] = Form.useForm()
  const [btForm] = Form.useForm()
  const [trackForm] = Form.useForm()
  const [notifyForm] = Form.useForm()
  const [webForm] = Form.useForm()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const resp = await api.getSettings()
      setData(resp)
      const cfg = resp.config
      setTdxInput(String(cfg?.data?.tdx_dir ?? ''))
      dataForm.setFieldsValue({
        tdx_dir: cfg?.data?.tdx_dir,
        cache_dir: cfg?.data?.cache_dir,
        start_date: cfg?.data?.start_date,
        adjust: cfg?.data?.adjust,
      })
      btForm.setFieldsValue({
        initial_cash: cfg?.backtest?.initial_cash,
        commission: cfg?.backtest?.commission,
        stamp_tax: cfg?.backtest?.stamp_tax,
        slippage: cfg?.backtest?.slippage,
        max_positions: cfg?.backtest?.max_positions,
        position_sizing: cfg?.backtest?.position_sizing,
        benchmark: cfg?.backtest?.benchmark,
        exec_price: cfg?.backtest?.exec_price,
      })
      trackForm.setFieldsValue({
        enabled: cfg?.tracker?.enabled,
        storage: cfg?.tracker?.storage,
        update_time: cfg?.tracker?.update_time,
        auto_update: cfg?.tracker?.auto_update,
        states: cfg?.tracker?.states,
        replay_start: cfg?.tracker?.replay_start,
      })
      notifyForm.setFieldsValue({
        console: cfg?.tracker?.notify?.console,
        csv: cfg?.tracker?.notify?.csv,
        webhook: cfg?.tracker?.notify?.webhook,
        webhook_type: cfg?.tracker?.notify?.webhook_type,
        email: cfg?.tracker?.notify?.email,
        csv_dir: cfg?.tracker?.notify?.csv_dir,
      })
      webForm.setFieldsValue({
        host: cfg?.web?.host,
        port: cfg?.web?.port,
        log_level: cfg?.log?.level,
      })
      const st = await api.getStrategySettings()
      setStrategies(st.strategies)
      setDefaultStrategy(st.default)
      const map: Record<string, Record<string, unknown>> = {}
      st.strategies.forEach((s) => {
        map[s.name] = { ...(s.default_params ?? {}) }
      })
      setStrategyParams(map)
    } catch (error) {
      message.error(`加载设置失败：${(error as Error).message}`)
    } finally {
      setLoading(false)
    }
  }, [dataForm, btForm, trackForm, notifyForm, webForm])

  useEffect(() => {
    void load()
  }, [load])

  /** 统一保存：把表单值合并为 config.yaml 的 patch */
  const save = async () => {
    setLoading(true)
    try {
      const patch: Record<string, unknown> = {}
      const d = await dataForm.validateFields().catch(() => null)
      if (d) patch.data = d
      const b = await btForm.validateFields().catch(() => null)
      if (b) patch.backtest = b
      const t = await trackForm.validateFields().catch(() => null)
      if (t) {
        patch.tracker = {
          enabled: t.enabled,
          storage: t.storage,
          update_time: t.update_time,
          auto_update: t.auto_update,
          states: Array.isArray(t.states)
            ? t.states
            : String(t.states ?? '')
                .split(/[,，\s]+/)
                .filter(Boolean),
          replay_start: t.replay_start,
          notify: {
            ...(notifyForm.getFieldsValue() as Record<string, unknown>),
          },
        }
      }
      const w = await webForm.validateFields().catch(() => null)
      if (w) {
        patch.web = { host: w.host, port: w.port }
        patch.log = { level: w.log_level }
      }
      patch.strategy = { default: defaultStrategy }
      Object.entries(strategyParams).forEach(([name, params]) => {
        ;(patch.strategy as Record<string, unknown>)[name] = params
      })

      const resp = await api.updateSettings(patch, true)
      message.success(`配置已保存到 ${resp.path}`)
      await load()
    } catch (error) {
      message.error(`保存失败：${(error as Error).message}`)
    } finally {
      setLoading(false)
    }
  }

  /** 保存单个策略参数 */
  const saveStrategy = async (name: string) => {
    try {
      await api.saveStrategySettings(name, strategyParams[name] ?? {})
      message.success(`${name} 参数已保存`)
    } catch (error) {
      message.error((error as Error).message)
    }
  }

  const scheduler = (data?.scheduler ?? {}) as Record<string, any>
  const notify = (data?.notify ?? {}) as Record<string, any>
  const storage = data?.storage ?? {}

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        title={
          <Space>
            <SettingOutlined />
            <Title level={4} style={{ margin: 0 }}>
              设置
            </Title>
          </Space>
        }
        extra={
          <Button type="primary" icon={<SaveOutlined />} loading={loading} onClick={save}>
            保存全部配置
          </Button>
        }
      >
        {data ? (
          <Descriptions size="small" column={3}>
            <Descriptions.Item label="配置文件">{data.path}</Descriptions.Item>
            <Descriptions.Item label="关注池条数">{storage.watchlist ?? 0}</Descriptions.Item>
            <Descriptions.Item label="状态记录">{storage.tracking_state ?? 0}</Descriptions.Item>
            <Descriptions.Item label="状态历史">{storage.tracking_history ?? 0}</Descriptions.Item>
            <Descriptions.Item label="信号记录">{storage.signals ?? 0}</Descriptions.Item>
            <Descriptions.Item label="提醒记录">{storage.alerts ?? 0}</Descriptions.Item>
          </Descriptions>
        ) : null}
      </Card>

      <Tabs
        items={[
          {
            key: 'data',
            label: (
              <Space>
                <DatabaseOutlined />
                数据
              </Space>
            ),
            children: (
              <Card>
                <Form form={dataForm} layout="vertical">
                  <Row gutter={16}>
                    <Col xs={24} md={12}>
                      <Form.Item label="通达信目录" name="tdx_dir">
                        <Input
                          addonBefore={<FolderOpenOutlined />}
                          value={tdxInput}
                          onChange={(e) => setTdxInput(e.target.value)}
                          placeholder="C:/new_tdx"
                        />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={12}>
                      <Form.Item label="缓存目录" name="cache_dir">
                        <Input placeholder="./cache" />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item label="数据起始日期" name="start_date">
                        <Input placeholder="2015-01-01" />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item label="复权方式" name="adjust">
                        <Select
                          options={[
                            { value: 'none', label: '不复权（本地通达信原始数据）' },
                            { value: 'qfq', label: '前复权（需除权除息数据）' },
                            { value: 'hfq', label: '后复权（需除权除息数据）' },
                          ]}
                        />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Space direction="vertical">
                        <Button
                          onClick={async () => {
                            try {
                              const r = await api.checkTdxDir(tdxInput)
                              setTdxCheck(r)
                              if (r.valid) message.success(`目录有效：${r.vipdoc}`)
                              else message.warning('未找到 vipdoc 目录，请确认路径')
                            } catch (e) {
                              message.error((e as Error).message)
                            }
                          }}
                        >
                          检测目录
                        </Button>
                        <Button
                          onClick={async () => {
                            try {
                              await api.setTdxDir(tdxInput, true)
                              message.success('通达信目录已生效')
                              await load()
                            } catch (e) {
                              message.error((e as Error).message)
                            }
                          }}
                        >
                          立即应用
                        </Button>
                      </Space>
                    </Col>
                  </Row>
                  {tdxCheck ? (
                    <Alert
                      type={tdxCheck.valid ? 'success' : 'warning'}
                      showIcon
                      message={`vipdoc：${String(tdxCheck.vipdoc ?? '-')}`}
                    />
                  ) : null}
                </Form>
              </Card>
            ),
          },
          {
            key: 'strategy',
            label: (
              <Space>
                <ThunderboltOutlined />
                策略默认参数
              </Space>
            ),
            children: (
              <Card
                extra={
                  <Space>
                    <Text>默认策略</Text>
                    <Select
                      style={{ width: 180 }}
                      value={defaultStrategy}
                      onChange={setDefaultStrategy}
                      options={strategies.map((s) => ({ value: s.name, label: s.display_name || s.name }))}
                    />
                  </Space>
                }
              >
                <Tabs
                  tabPosition="left"
                  items={strategies.map((s) => ({
                    key: s.name,
                    label: s.display_name || s.name,
                    children: (
                      <Space direction="vertical" style={{ width: '100%' }}>
                        <Paragraph type="secondary">{s.description}</Paragraph>
                        <Table
                          size="small"
                          rowKey="key"
                          pagination={false}
                          columns={[
                            { title: '参数', dataIndex: 'key', width: 160 },
                            { title: '说明', dataIndex: 'label' },
                            { title: '当前值', dataIndex: 'value', width: 180 },
                          ]}
                          dataSource={Object.keys(s.default_params ?? {}).map((k) => ({
                            key: k,
                            label: s.param_schema?.[k]?.label ?? k,
                            value: (
                              <InputNumber
                                style={{ width: '100%' }}
                                value={strategyParams[s.name]?.[k] as number}
                                step={s.param_schema?.[k]?.step ?? 1}
                                onChange={(v) =>
                                  setStrategyParams((prev) => ({
                                    ...prev,
                                    [s.name]: { ...prev[s.name], [k]: v },
                                  }))
                                }
                              />
                            ),
                          }))}
                        />
                        <Space>
                          <Button type="primary" onClick={() => void saveStrategy(s.name)}>
                            保存 {s.display_name || s.name} 参数
                          </Button>
                          <Button
                            onClick={() =>
                              setStrategyParams((prev) => ({
                                ...prev,
                                [s.name]: { ...(s.default_params ?? {}) },
                              }))
                            }
                          >
                            恢复默认
                          </Button>
                        </Space>
                      </Space>
                    ),
                  }))}
                />
              </Card>
            ),
          },
          {
            key: 'backtest',
            label: (
              <Space>
                <ApiOutlined />
                回测默认
              </Space>
            ),
            children: (
              <Card>
                <Form form={btForm} layout="vertical">
                  <Row gutter={16}>
                    <Col xs={24} md={8}>
                      <Form.Item label="初始资金" name="initial_cash">
                        <InputNumber style={{ width: '100%' }} min={1000} step={100000} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item label="佣金费率" name="commission">
                        <InputNumber style={{ width: '100%' }} min={0} step={0.0001} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item label="印花税（仅卖出）" name="stamp_tax">
                        <InputNumber style={{ width: '100%' }} min={0} step={0.0001} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item label="滑点率" name="slippage">
                        <InputNumber style={{ width: '100%' }} min={0} step={0.0001} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item label="最大持仓数" name="max_positions">
                        <InputNumber style={{ width: '100%' }} min={1} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item label="仓位规则" name="position_sizing">
                        <Select
                          options={[
                            { value: 'equal', label: '等权' },
                            { value: 'fixed', label: '固定资金' },
                          ]}
                        />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item label="成交时点" name="exec_price">
                        <Select
                          options={[
                            { value: 'next_open', label: 'T+1 开盘' },
                            { value: 'next_close', label: 'T+1 收盘' },
                            { value: 'close', label: 'T 日收盘' },
                          ]}
                        />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item label="基准指数" name="benchmark">
                        <Select
                          options={[
                            { value: '000300.SH', label: '沪深300（000300.SH）' },
                            { value: '000001.SH', label: '上证指数（000001.SH）' },
                            { value: '399001.SZ', label: '深证成指（399001.SZ）' },
                          ]}
                        />
                      </Form.Item>
                    </Col>
                  </Row>
                </Form>
              </Card>
            ),
          },
          {
            key: 'tracker',
            label: (
              <Space>
                <BellOutlined />
                追踪与通知
              </Space>
            ),
            children: (
              <Card>
                <Form form={trackForm} layout="vertical">
                  <Row gutter={16}>
                    <Col xs={24} md={8}>
                      <Form.Item label="启用追踪" name="enabled" valuePropName="checked">
                        <Switch />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item label="每日自动更新时间" name="update_time">
                        <Input placeholder="15:30" />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item label="自动更新" name="auto_update" valuePropName="checked">
                        <Switch />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={12}>
                      <Form.Item label="状态数据库" name="storage">
                        <Input placeholder="sqlite:///tracking.db" />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={12}>
                      <Form.Item label="回放起始日期" name="replay_start">
                        <Input placeholder="2023-01-01" />
                      </Form.Item>
                    </Col>
                    <Col span={24}>
                      <Form.Item
                        label="状态列表（逗号分隔，可自定义扩展）"
                        name="states"
                        getValueFromEvent={(e: React.ChangeEvent<HTMLInputElement>) => e.target.value}
                        normalize={(v) => (Array.isArray(v) ? v.join(',') : v)}
                      >
                        <Input placeholder="候选,观察,触发,买入,持仓,减仓,清仓,止盈,止损,失效,暂停" />
                      </Form.Item>
                    </Col>
                  </Row>
                </Form>

                <Divider />

                <Title level={5}>通知配置</Title>
                <Form form={notifyForm} layout="vertical">
                  <Row gutter={16}>
                    <Col xs={24} md={6}>
                      <Form.Item label="控制台提醒" name="console" valuePropName="checked">
                        <Switch />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={6}>
                      <Form.Item label="CSV 导出" name="csv" valuePropName="checked">
                        <Switch />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={6}>
                      <Form.Item label="CSV 输出路径" name="csv_path">
                        <Input placeholder="./reports/alerts.csv" />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={6}>
                      <Form.Item label="邮件通知" name="email">
                        <Input placeholder="留空不启用" />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={12}>
                      <Form.Item label="Webhook 地址" name="webhook">
                        <Input placeholder="https://oapi.dingtalk.com/robot/send?access_token=..." />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={6}>
                      <Form.Item label="Webhook 类型" name="webhook_type">
                        <Select
                          allowClear
                          placeholder="自动识别"
                          options={[
                            { value: 'dingtalk', label: '钉钉' },
                            { value: 'wecom', label: '企业微信' },
                            { value: 'feishu', label: '飞书' },
                            { value: 'generic', label: '通用 JSON' },
                          ]}
                        />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={6}>
                      <Form.Item label=" ">
                        <Button
                          onClick={async () => {
                            try {
                              const r = await api.testNotify()
                              message.success(`已发送测试通知：${JSON.stringify(r)}`)
                            } catch (e) {
                              message.error((e as Error).message)
                            }
                          }}
                        >
                          发送测试通知
                        </Button>
                      </Form.Item>
                    </Col>
                  </Row>
                </Form>

                <Alert
                  type="info"
                  showIcon
                  style={{ marginTop: 8 }}
                  message="当前通知通道状态"
                  description={
                    <Space wrap>
                      {Object.entries(notify).map(([k, v]) => (
                        <Tag key={k} color={v ? 'green' : 'default'}>
                          {k}: {String(v)}
                        </Tag>
                      ))}
                    </Space>
                  }
                />

                <Divider />

                <Title level={5}>定时任务</Title>
                <Space wrap>
                  <Tag color={scheduler.running ? 'green' : 'default'}>
                    {scheduler.running ? '运行中' : '已停止'}
                  </Tag>
                  {scheduler.jobs
                    ? (scheduler.jobs as Array<Record<string, any>>).map((j, i) => (
                        <Tag key={i} color="blue">
                          {String(j.id ?? j.name ?? i)} → {String(j.next_run ?? '-')}
                        </Tag>
                      ))
                    : null}
                  <Button size="small" onClick={() => void api.schedulerStart().then(load)}>
                    启动
                  </Button>
                  <Button size="small" onClick={() => void api.schedulerStop().then(load)}>
                    停止
                  </Button>
                  <Button size="small" type="primary" onClick={() => void api.schedulerRun(true).then(load)}>
                    立即执行一次
                  </Button>
                  <Button size="small" icon={<ScheduleOutlined />} onClick={() => void load()}>
                    刷新状态
                  </Button>
                </Space>
              </Card>
            ),
          },
          {
            key: 'web',
            label: (
              <Space>
                <ApiOutlined />
                系统
              </Space>
            ),
            children: (
              <Card>
                <Form form={webForm} layout="vertical">
                  <Row gutter={16}>
                    <Col xs={24} md={8}>
                      <Form.Item label="服务监听地址" name="host">
                        <Input placeholder="0.0.0.0" />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item label="服务端口" name="port">
                        <InputNumber style={{ width: '100%' }} min={1} max={65535} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item label="日志级别" name="log_level">
                        <Select
                          options={['DEBUG', 'INFO', 'WARNING', 'ERROR'].map((v) => ({ value: v, label: v }))}
                        />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={8}>
                      <Form.Item label="挂载前端构建产物" name="serve_frontend" valuePropName="checked">
                        <Switch />
                      </Form.Item>
                    </Col>
                  </Row>
                </Form>
                <Divider />
                <Title level={5}>危险操作</Title>
                <Space wrap>
                  <Popconfirm
                    title="确认清空 SQLite 中的追踪数据？此操作不可撤销。"
                    okButtonProps={{ danger: true }}
                    onConfirm={async () => {
                      try {
                        const r = await api.resetDb()
                        message.success(`已清空：${JSON.stringify(r)}`)
                        await load()
                      } catch (e) {
                        message.error((e as Error).message)
                      }
                    }}
                  >
                    <Button danger>清空追踪数据库</Button>
                  </Popconfirm>
                  <Button
                    onClick={async () => {
                      try {
                        const r = await api.createNamesTemplate()
                        message.success(`名称模板已生成：${r.path}`)
                      } catch (e) {
                        message.error((e as Error).message)
                      }
                    }}
                  >
                    生成股票名称模板
                  </Button>
                </Space>
              </Card>
            ),
          },
        ]}
      />
    </Space>
  )
}
