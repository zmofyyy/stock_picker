/**
 * 追踪看板页面。
 *
 * 关注池管理 + 状态总览 + 状态时间线 + 手动更新 + 历史回放 + 提醒查看。
 * 所有数据来自后端 API，页面只负责渲染。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Divider,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Timeline,
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  BellOutlined,
  DeleteOutlined,
  EditOutlined,
  HistoryOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  RollbackOutlined,
  StarOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import dayjs, { Dayjs } from 'dayjs'
import { api } from '@/api'
import type { AlertRow, HistoryRow, TrackingState, WatchItem } from '@/api'
import StateTag from '@/components/StateTag'

const { Title, Text, Paragraph } = Typography

/** 内置状态列表（与 config.yaml 一致，可扩展） */
const DEFAULT_STATES = [
  '候选',
  '观察',
  '触发',
  '买入',
  '持仓',
  '减仓',
  '清仓',
  '止盈',
  '止损',
  '失效',
  '暂停',
]

/** 风险等级配色 */
const RISK_COLOR: Record<string, string> = {
  高: 'red',
  中: 'orange',
  低: 'blue',
  无: 'default',
}

/** 信号配色 */
function signalColor(signal: number): string {
  if (signal > 0) return 'red'
  if (signal < 0) return 'green'
  return 'default'
}

export default function Tracker() {
  const [items, setItems] = useState<WatchItem[]>([])
  const [groups, setGroups] = useState<Array<{ group: string; count: number }>>([])
  const [loading, setLoading] = useState(false)
  const [groupFilter, setGroupFilter] = useState<string | undefined>()
  const [keyword, setKeyword] = useState('')

  const [states, setStates] = useState<TrackingState[]>([])
  const [signals, setSignals] = useState<Array<Record<string, any>>>([])
  const [alerts, setAlerts] = useState<AlertRow[]>([])

  const [addOpen, setAddOpen] = useState(false)
  const [editItem, setEditItem] = useState<WatchItem | null>(null)
  const [form] = Form.useForm()

  const [detail, setDetail] = useState<WatchItem | null>(null)
  const [detailState, setDetailState] = useState<TrackingState | null>(null)
  const [timeline, setTimeline] = useState<Array<Record<string, any>>>([])
  const [history, setHistory] = useState<HistoryRow[]>([])
  const [notes, setNotes] = useState<Array<Record<string, any>>>([])
  const [noteText, setNoteText] = useState('')

  const [updateDate, setUpdateDate] = useState<Dayjs | null>(null)
  const [busy, setBusy] = useState(false)
  const [wsConnected, setWsConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)

  /** 拉取关注池与状态 */
  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [wl, st, sg, al] = await Promise.all([
        api.getWatchlist({ group: groupFilter, keyword: keyword || undefined }),
        api.listStates({ group: groupFilter, keyword: keyword || undefined }),
        api.getSignals({ limit: 100 }),
        api.getAlerts({ limit: 100, acked: false }),
      ])
      setItems(wl.items)
      setGroups(wl.groups)
      setStates(st.items)
      setSignals(sg.items)
      setAlerts(al.items)
    } catch (error) {
      message.error(`加载追踪数据失败：${(error as Error).message}`)
    } finally {
      setLoading(false)
    }
  }, [groupFilter, keyword])

  useEffect(() => {
    void refresh()
  }, [refresh])

  /** WebSocket 实时推送（后端可用时自动连接，失败则静默降级为轮询） */
  useEffect(() => {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const url = `${proto}://${window.location.host}/ws/tracker`
    try {
      const ws = new WebSocket(url)
      wsRef.current = ws
      ws.onopen = () => setWsConnected(true)
      ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data)
          if (payload?.type === 'tracking_update') {
            message.info(`收到实时更新：${payload.trade_date ?? ''} 变更 ${payload.changed ?? 0} 条`)
            void refresh()
          }
        } catch {
          /* 忽略非 JSON 消息 */
        }
      }
      ws.onerror = () => setWsConnected(false)
      ws.onclose = () => setWsConnected(false)
      return () => ws.close()
    } catch {
      setWsConnected(false)
      return undefined
    }
  }, [refresh])

  /** 状态映射，便于关注池列表直接展示 */
  const stateMap = useMemo(() => {
    const map: Record<string, TrackingState> = {}
    states.forEach((s) => {
      map[s.code] = s
    })
    return map
  }, [states])

  /** 打开详情抽屉并加载时间线 */
  const openDetail = async (code: string) => {
    const item = items.find((i) => i.code === code) ?? null
    setDetail(item)
    setDetailState(stateMap[code] ?? null)
    setTimeline([])
    setHistory([])
    setNotes([])
    try {
      const [tl, hs, nt, st] = await Promise.all([
        api.getTimeline(code, 200),
        api.getHistory(code, { limit: 200 }),
        api.listNotes(code),
        api.getState(code),
      ])
      setTimeline(tl.events)
      setHistory(hs.items)
      setNotes(nt)
      if (st.state) setDetailState(st.state)
    } catch (error) {
      message.error(`加载时间线失败：${(error as Error).message}`)
    }
  }

  /** 手动更新 */
  const handleUpdate = async () => {
    setBusy(true)
    try {
      const res = await api.updateTracking({
        date: updateDate ? updateDate.format('YYYY-MM-DD') : undefined,
        group: groupFilter,
        notify: true,
      })
      if (res.errors.length) {
        message.warning(`部分股票更新失败 ${res.errors.length} 只`)
      }
      message.success(
        `追踪更新完成：交易日 ${res.trade_date ?? '-'}，处理 ${res.processed} 只，` +
          `状态变更 ${res.changed} 条，新信号 ${res.new_signals} 条，提醒 ${res.alerts} 条，耗时 ${res.elapsed.toFixed(2)}s`,
      )
      await refresh()
    } catch (error) {
      message.error(`更新失败：${(error as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  /** 历史回放 */
  const handleReplay = async (range: [Dayjs, Dayjs]) => {
    setBusy(true)
    try {
      const res = await api.replayTracking({
        start: range[0].format('YYYY-MM-DD'),
        end: range[1].format('YYYY-MM-DD'),
        group: groupFilter,
        notify: false,
      })
      message.success(
        `回放完成：${range[0].format('YYYY-MM-DD')} ~ ${range[1].format('YYYY-MM-DD')}，` +
          `处理 ${res.processed} 只，状态变更 ${res.changed} 条，耗时 ${res.elapsed.toFixed(2)}s`,
      )
      await refresh()
    } catch (error) {
      message.error(`回放失败：${(error as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  const openAdd = () => {
    form.resetFields()
    form.setFieldsValue({ strategy: 'ma_cross', group: '默认', enabled: true })
    setAddOpen(true)
  }

  const openEdit = (item: WatchItem) => {
    form.setFieldsValue({
      code: item.code,
      name: item.name,
      group: item.group,
      tags: (item.tags ?? []).join(','),
      note: item.note,
      strategy: item.strategy,
      cost_price: item.cost_price,
      shares: item.shares,
      target_price: item.target_price,
      stop_price: item.stop_price,
      enabled: item.enabled,
    })
    setEditItem(item)
    setAddOpen(true)
  }

  const submitAdd = async () => {
    try {
      const values = await form.validateFields()
      const payload = {
        ...values,
        tags: typeof values.tags === 'string' ? values.tags.split(',').map((s: string) => s.trim()).filter(Boolean) : values.tags,
      }
      if (editItem) {
        await api.updateWatch(editItem.code, payload)
        message.success(`${editItem.code} 已更新`)
      } else {
        await api.addWatch(payload)
        message.success(`${payload.code} 已加入关注池`)
      }
      setAddOpen(false)
      setEditItem(null)
      await refresh()
    } catch (error) {
      if ((error as { errorFields?: unknown }).errorFields) return
      message.error((error as Error).message)
    }
  }

  const watchColumns: ColumnsType<WatchItem> = [
    {
      title: '代码',
      dataIndex: 'code',
      width: 120,
      fixed: 'left',
      render: (v: string, r) => (
        <a onClick={() => void openDetail(v)}>
          {v}
          {r.enabled ? null : <Tag style={{ marginLeft: 6 }}>暂停</Tag>}
        </a>
      ),
    },
    { title: '名称', dataIndex: 'name', width: 110 },
    { title: '分组', dataIndex: 'group', width: 100 },
    {
      title: '策略',
      dataIndex: 'strategy',
      width: 120,
      render: (v: string) => v || '-',
    },
    {
      title: '状态',
      key: 'state',
      width: 100,
      render: (_, r) => <StateTag state={stateMap[r.code]?.state} />,
    },
    {
      title: '上次状态',
      key: 'prev_state',
      width: 100,
      render: (_, r) => <StateTag state={stateMap[r.code]?.prev_state} />
    },
    {
      title: '变更时间',
      key: 'changed_at',
      width: 170,
      render: (_, r) => {
        const t = stateMap[r.code]?.state_changed_at
        return t ? dayjs(t).format('YYYY-MM-DD HH:mm') : '-'
      },
    },
    {
      title: '信号',
      key: 'signal',
      width: 90,
      render: (_, r) => {
        const s = stateMap[r.code]
        if (!s) return '-'
        return <Tag color={signalColor(s.signal)}>{s.signal_text || '无'}</Tag>
      },
    },
    {
      title: '风险',
      key: 'risk',
      width: 110,
      render: (_, r) => {
        const s = stateMap[r.code]
        if (!s) return '-'
        return (
          <Tooltip title={(s.risk_flags ?? []).join('、') || '无风险标记'}>
            <Badge color={RISK_COLOR[s.risk_level] ?? 'default'} text={s.risk_level || '无'} />
          </Tooltip>
        )
      },
    },
    {
      title: '现价',
      key: 'price',
      width: 90,
      render: (_, r) => {
        const s = stateMap[r.code]
        return s?.price != null ? s.price.toFixed(3) : '-'
      },
    },
    {
      title: '涨跌幅',
      key: 'pct',
      width: 100,
      sorter: (a, b) => (stateMap[a.code]?.pct_change ?? 0) - (stateMap[b.code]?.pct_change ?? 0),
      render: (_, r) => {
        const p = stateMap[r.code]?.pct_change
        if (p == null) return '-'
        const color = p > 0 ? '#cf1322' : p < 0 ? '#3f8600' : undefined
        return <span style={{ color }}>{(p * 100).toFixed(2)}%</span>
      },
    },
    {
      title: '浮动盈亏',
      key: 'pnl',
      width: 140,
      render: (_, r) => {
        const s = stateMap[r.code]
        if (!s || s.unrealized_pnl == null) return '-'
        const color = s.unrealized_pnl > 0 ? '#cf1322' : s.unrealized_pnl < 0 ? '#3f8600' : undefined
        return (
          <span style={{ color }}>
            {s.unrealized_pnl.toFixed(2)}
            {s.unrealized_pct != null ? ` (${(s.unrealized_pct * 100).toFixed(2)}%)` : ''}
          </span>
        )
      },
    },
    {
      title: '标签',
      dataIndex: 'tags',
      width: 150,
      render: (tags: string[]) =>
        (tags ?? []).map((t) => (
          <Tag key={t} color="blue">
            {t}
          </Tag>
        )),
    },
    { title: '备注', dataIndex: 'note', width: 160, ellipsis: true },
    {
      title: '操作',
      key: 'action',
      width: 170,
      fixed: 'right',
      render: (_, r) => (
        <Space size={4}>
          <Tooltip title="编辑">
            <Button size="small" type="text" icon={<EditOutlined />} onClick={() => openEdit(r)} />
          </Tooltip>
          <Tooltip title={r.enabled ? '暂停追踪' : '恢复追踪'}>
            <Button
              size="small"
              type="text"
              icon={r.enabled ? <PauseCircleOutlined /> : <PlayCircleOutlined />}
              onClick={async () => {
                try {
                  await api.pauseWatch(r.code, r.enabled)
                  await refresh()
                } catch (e) {
                  message.error((e as Error).message)
                }
              }}
            />
          </Tooltip>
          <Popconfirm
            title={`确认从关注池移除 ${r.code}？`}
            onConfirm={async () => {
              try {
                await api.removeWatch(r.code)
                message.success('已移除')
                await refresh()
              } catch (e) {
                message.error((e as Error).message)
              }
            }}
          >
            <Button size="small" type="text" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const alertColumns: ColumnsType<AlertRow> = [
    { title: '时间', dataIndex: 'created_at', width: 170, render: (v: string) => dayjs(v).format('YYYY-MM-DD HH:mm') },
    { title: '级别', dataIndex: 'level', width: 80, render: (v: string) => <Tag color={RISK_COLOR[v] ?? 'default'}>{v}</Tag> },
    { title: '类别', dataIndex: 'category', width: 110 },
    { title: '代码', dataIndex: 'code', width: 110 },
    { title: '名称', dataIndex: 'name', width: 110 },
    { title: '标题', dataIndex: 'title', ellipsis: true },
    { title: '内容', dataIndex: 'message', ellipsis: true },
    {
      title: '操作',
      key: 'action',
      width: 90,
      render: (_, r) => (
        <Button
          size="small"
          onClick={async () => {
            await api.ackAlert(r.id)
            await refresh()
          }}
        >
          标记已读
        </Button>
      ),
    },
  ]

  const historyColumns: ColumnsType<HistoryRow> = [
    { title: '变更时间', dataIndex: 'changed_at', width: 170, render: (v: string) => dayjs(v).format('YYYY-MM-DD HH:mm') },
    { title: '交易日', dataIndex: 'trade_date', width: 110 },
    { title: '旧状态', dataIndex: 'old_state', width: 100, render: (v: string) => <StateTag state={v} /> },
    { title: '新状态', dataIndex: 'new_state', width: 100, render: (v: string) => <StateTag state={v} /> },
    { title: '触发原因', dataIndex: 'reason', ellipsis: true },
    { title: '策略', dataIndex: 'strategy', width: 120 },
    { title: '价格', dataIndex: 'price', width: 90, render: (v: number | null) => (v == null ? '-' : v.toFixed(3)) },
    { title: '备注', dataIndex: 'note', ellipsis: true },
  ]

  const stats = useMemo(() => {
    const dist: Record<string, number> = {}
    states.forEach((s) => {
      dist[s.state] = (dist[s.state] ?? 0) + 1
    })
    const holding = states.filter((s) => ['持仓', '买入', '减仓'].includes(s.state))
    const pnl = holding.reduce((acc, s) => acc + (s.unrealized_pnl ?? 0), 0)
    const risk = states.filter((s) => s.risk_level === '高').length
    return { dist, holding: holding.length, pnl, risk }
  }, [states])

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        title={
          <Space>
            <Title level={4} style={{ margin: 0 }}>
              追踪看板
            </Title>
            <Badge
              status={wsConnected ? 'processing' : 'default'}
              text={wsConnected ? '实时推送已连接' : '实时推送未连接（使用手动/定时更新）'}
            />
          </Space>
        }
        extra={
          <Space>
            <DatePicker
              placeholder="指定交易日（留空=最新）"
              value={updateDate}
              onChange={(v) => setUpdateDate(v)}
              allowClear
            />
            <Button type="primary" icon={<SyncOutlined />} loading={busy} onClick={handleUpdate}>
              手动更新
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => void refresh()} loading={loading}>
              刷新
            </Button>
          </Space>
        }
      >
        <Row gutter={[12, 12]}>
          <Col xs={12} md={6}>
            <Statistic title="关注池总数" value={items.length} suffix="只" />
          </Col>
          <Col xs={12} md={6}>
            <Statistic title="持仓中" value={stats.holding} suffix="只" />
          </Col>
          <Col xs={12} md={6}>
            <Statistic
              title="浮动盈亏合计"
              value={stats.pnl}
              precision={2}
              valueStyle={{ color: stats.pnl > 0 ? '#cf1322' : stats.pnl < 0 ? '#3f8600' : undefined }}
            />
          </Col>
          <Col xs={12} md={6}>
            <Statistic title="高风险标的" value={stats.risk} suffix="只" valueStyle={{ color: '#cf1322' }} />
          </Col>
        </Row>

        <Divider style={{ margin: '12px 0' }} />

        <Space wrap>
          <Text type="secondary">状态分布：</Text>
          {Object.keys(stats.dist).length ? (
            Object.entries(stats.dist).map(([k, v]) => (
              <Tag key={k} color="geekblue">
                {k} {v}
              </Tag>
            ))
          ) : (
            <Text type="secondary">暂无数据</Text>
          )}
        </Space>
      </Card>

      <Card
        title="关注池"
        extra={
          <Space>
            <Select
              allowClear
              placeholder="按分组筛选"
              style={{ width: 160 }}
              value={groupFilter}
              onChange={(v) => setGroupFilter(v)}
              options={groups.map((g) => ({ value: g.group, label: `${g.group}（${g.count}）` }))}
            />
            <Input.Search
              placeholder="代码 / 名称"
              allowClear
              style={{ width: 180 }}
              onSearch={(v) => setKeyword(v)}
            />
            <Button type="primary" icon={<PlusOutlined />} onClick={openAdd}>
              添加股票
            </Button>
          </Space>
        }
      >
        <Table
          size="small"
          rowKey="code"
          loading={loading}
          columns={watchColumns}
          dataSource={items}
          scroll={{ x: 1900 }}
          pagination={{ pageSize: 15, showSizeChanger: true }}
          locale={{ emptyText: <Empty description="关注池为空，请先在「选股」页加入或点击右上角添加" /> }}
        />
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} xl={12}>
          <Card
            title={
              <Space>
                <BellOutlined />
                未读提醒（{alerts.length}）
              </Space>
            }
          >
            <Table
              size="small"
              rowKey="id"
              columns={alertColumns}
              dataSource={alerts}
              pagination={{ pageSize: 6 }}
              locale={{ emptyText: <Empty description="暂无未读提醒" /> }}
            />
          </Card>
        </Col>
        <Col xs={24} xl={12}>
          <Card title="最新信号">
            <Table
              size="small"
              rowKey="id"
              columns={[
                { title: '交易日', dataIndex: 'trade_date', width: 110 },
                { title: '代码', dataIndex: 'code', width: 110 },
                { title: '名称', dataIndex: 'name', width: 100 },
                {
                  title: '信号',
                  dataIndex: 'signal_text',
                  width: 90,
                  render: (v: string, r: Record<string, any>) => (
                    <Tag color={signalColor(Number(r.signal))}>{v}</Tag>
                  ),
                },
                { title: '价格', dataIndex: 'price', width: 90, render: (v: number | null) => (v == null ? '-' : v.toFixed(3)) },
                { title: '原因', dataIndex: 'reason', ellipsis: true },
              ]}
              dataSource={signals}
              pagination={{ pageSize: 6 }}
              locale={{ emptyText: <Empty description="暂无信号" /> }}
            />
          </Card>
        </Col>
      </Row>

      <Card title={<Space><RollbackOutlined />历史回放</Space>}>
        <Paragraph type="secondary">
          给定历史区间，逐交易日重建当时的追踪状态，用于验证追踪逻辑（只使用当日及之前的数据，无未来函数）。
        </Paragraph>
        <DatePicker.RangePicker
          showTime={false}
          onChange={(v) => {
            if (v && v[0] && v[1]) void handleReplay([v[0], v[1]])
          }}
          disabledDate={(d) => d.isAfter(dayjs())}
        />
      </Card>

      {/* 添加 / 编辑关注项 */}
      <Modal
        open={addOpen}
        title={editItem ? `编辑 ${editItem.code}` : '添加关注'}
        onCancel={() => {
          setAddOpen(false)
          setEditItem(null)
        }}
        onOk={submitAdd}
        okText="保存"
        width={620}
      >
        <Form form={form} layout="vertical">
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="code"
                label="股票代码"
                rules={[{ required: true, message: '请输入代码，如 600000.SH' }]}
              >
                <Input placeholder="600000.SH" disabled={!!editItem} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="name" label="股票名称">
                <Input placeholder="留空自动从名称表推断" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="group" label="分组" initialValue="默认">
                <Input placeholder="默认" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="strategy" label="关联策略" initialValue="ma_cross">
                <Select
                  options={[
                    { value: 'ma_cross', label: '双均线交叉' },
                    { value: 'rsi', label: 'RSI 超买超卖' },
                    { value: 'volume_breakout', label: '放量突破' },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="cost_price" label="成本价">
                <InputNumber style={{ width: '100%' }} min={0} step={0.01} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="shares" label="持股数量">
                <InputNumber style={{ width: '100%' }} min={0} step={100} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="target_price" label="目标价">
                <InputNumber style={{ width: '100%' }} min={0} step={0.01} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="stop_price" label="止损价">
                <InputNumber style={{ width: '100%' }} min={0} step={0.01} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="tags" label="标签（逗号分隔）">
                <Input placeholder="核心资产,低估值" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="enabled" label="启用追踪" valuePropName="checked" initialValue={true}>
                <Switch />
              </Form.Item>
            </Col>
            <Col span={24}>
              <Form.Item name="note" label="备注">
                <Input.TextArea rows={2} />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      {/* 个股详情抽屉 */}
      <Drawer
        open={!!detail}
        onClose={() => setDetail(null)}
        width={860}
        title={detail ? `${detail.code} ${detail.name || ''}` : ''}
      >
        {detail ? (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions size="small" bordered column={3}>
              <Descriptions.Item label="分组">{detail.group}</Descriptions.Item>
              <Descriptions.Item label="关联策略">{detail.strategy || '-'}</Descriptions.Item>
              <Descriptions.Item label="数据日期">{detailState?.data_date ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="当前状态">
                <StateTag state={detailState?.state} />
              </Descriptions.Item>
              <Descriptions.Item label="上次状态">
                <StateTag state={detailState?.prev_state} />
              </Descriptions.Item>
              <Descriptions.Item label="变更时间">
                {detailState?.state_changed_at
                  ? dayjs(detailState.state_changed_at).format('YYYY-MM-DD HH:mm')
                  : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="现价">
                {detailState?.price != null ? detailState.price.toFixed(3) : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="涨跌幅">
                {detailState?.pct_change != null ? `${(detailState.pct_change * 100).toFixed(2)}%` : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="风险等级">
                <Badge color={RISK_COLOR[detailState?.risk_level ?? '无'] ?? 'default'} text={detailState?.risk_level ?? '无'} />
              </Descriptions.Item>
              <Descriptions.Item label="成本价">{detail.cost_price ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="持股数">{detail.shares ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="浮动盈亏">
                {detailState?.unrealized_pnl != null ? detailState.unrealized_pnl.toFixed(2) : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="持仓天数">{detailState?.hold_days ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="目标价">{detail.target_price ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="止损价">{detail.stop_price ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="状态原因" span={3}>
                {detailState?.state_reason || '-'}
              </Descriptions.Item>
              <Descriptions.Item label="风险标记" span={3}>
                {(detailState?.risk_flags ?? []).length
                  ? (detailState?.risk_flags ?? []).map((f) => <Tag color="orange" key={f}>{f}</Tag>)
                  : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="关键因子" span={3}>
                {detailState?.factors && Object.keys(detailState.factors).length
                  ? Object.entries(detailState.factors).map(([k, v]) => (
                      <Tag key={k}>
                        {k}: {v == null ? '-' : Number(v).toFixed(4)}
                      </Tag>
                    ))
                  : '-'}
              </Descriptions.Item>
            </Descriptions>
          </Space>
        ) : null}

        {detail ? (
          <Tabs
            style={{ marginTop: 16 }}
            items={[
              {
                key: 'timeline',
                label: (
                  <Space>
                    <HistoryOutlined />
                    状态时间线
                  </Space>
                ),
                children: timeline.length ? (
                  <Timeline
                    items={timeline.map((e) => ({
                      color:
                        e.level === 'error' ? 'red' : e.level === 'warning' ? 'orange' : e.level === 'success' ? 'green' : 'blue',
                      children: (
                        <div>
                          <Space>
                            <Text strong>{e.date}</Text>
                            <Tag>{e.type}</Tag>
                          </Space>
                          <div>{e.title}</div>
                          {e.detail ? <Text type="secondary">{e.detail}</Text> : null}
                        </div>
                      ),
                    }))}
                  />
                ) : (
                  <Empty description="暂无时间线数据" />
                ),
              },
              {
                key: 'history',
                label: '状态变迁',
                children: (
                  <Table
                    size="small"
                    rowKey="id"
                    columns={historyColumns}
                    dataSource={history}
                    pagination={{ pageSize: 10 }}
                  />
                ),
              },
              {
                key: 'notes',
                label: '备注',
                children: (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <Space.Compact style={{ width: '100%' }}>
                      <Input
                        value={noteText}
                        onChange={(e) => setNoteText(e.target.value)}
                        placeholder="添加备注…"
                      />
                      <Button
                        type="primary"
                        onClick={async () => {
                          if (!noteText.trim()) return
                          await api.addNote(detail.code, noteText)
                          setNoteText('')
                          setNotes(await api.listNotes(detail.code))
                        }}
                      >
                        添加
                      </Button>
                    </Space.Compact>
                    {notes.length ? (
                      <Timeline
                        items={notes.map((n) => ({
                          children: (
                            <div>
                              <Text>{String(n.content ?? '')}</Text>
                              <div>
                                <Text type="secondary">{String(n.created_at ?? '')}</Text>
                              </div>
                            </div>
                          ),
                        }))}
                      />
                    ) : (
                      <Empty description="暂无备注" />
                    )}
                  </Space>
                ),
              },
              {
                key: 'manual',
                label: '手动置状态',
                children: (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <Alert
                      type="info"
                      showIcon
                      message="手动设置状态会写入状态变迁历史，并参与后续追踪判断。"
                    />
                    <Space wrap>
                      {DEFAULT_STATES.map((s) => (
                        <Button
                          key={s}
                          icon={<StarOutlined />}
                          onClick={async () => {
                            try {
                              await api.setState(detail.code, s, '用户手动设置')
                              message.success(`已设置为「${s}」`)
                              await refresh()
                              await openDetail(detail.code)
                            } catch (e) {
                              message.error((e as Error).message)
                            }
                          }}
                        >
                          {s}
                        </Button>
                      ))}
                    </Space>
                  </Space>
                ),
              },
            ]}
          />
        ) : null}
      </Drawer>
    </Space>
  )
}
