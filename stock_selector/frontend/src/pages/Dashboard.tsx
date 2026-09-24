/**
 * 仪表盘页面。
 *
 * 展示：数据状态、关注池概况、今日信号、今日状态变更、风险提醒、最新回测绩效摘要。
 */
import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  List,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { ReloadOutlined, SyncOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import * as echarts from 'echarts'
import dayjs from 'dayjs'
import { api } from '@/api'
import type { AlertRow, DashboardData, HistoryRow, SignalRow } from '@/api'
import EChart from '@/components/EChart'
import MetricCard, { changeColor, pct } from '@/components/MetricCard'
import { AlertLevelTag, StateTag } from '@/components/StateTag'

const { Title, Text } = Typography

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null)
  const [loading, setLoading] = useState(false)
  const [updating, setUpdating] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      setData(await api.dashboard())
    } catch (error) {
      message.error(`加载仪表盘失败：${(error as Error).message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const handleUpdate = async () => {
    setUpdating(true)
    try {
      const result = await api.updateTracking({})
      message.success(
        `更新完成：处理 ${result.processed} 只，状态变更 ${result.changed} 次，新信号 ${result.new_signals} 个`,
      )
      await load()
    } catch (error) {
      message.error(`更新失败：${(error as Error).message}`)
    } finally {
      setUpdating(false)
    }
  }

  /** 状态分布饼图 */
  const stateOption = useMemo<echarts.EChartsOption>(() => {
    const dist = data?.state_distribution ?? {}
    return {
      tooltip: { trigger: 'item' },
      legend: { bottom: 0, type: 'scroll' },
      series: [
        {
          type: 'pie',
          radius: ['42%', '68%'],
          avoidLabelOverlap: true,
          label: { formatter: '{b}: {c}' },
          data: Object.entries(dist).map(([name, value]) => ({ name, value })),
        },
      ],
    }
  }, [data])

  /** 风险分布饼图 */
  const riskOption = useMemo<echarts.EChartsOption>(() => {
    const labels: Record<string, string> = { normal: '正常', warning: '警示', danger: '危险' }
    const dist = data?.risk_distribution ?? {}
    return {
      tooltip: { trigger: 'item' },
      legend: { bottom: 0 },
      series: [
        {
          type: 'pie',
          radius: '62%',
          data: Object.entries(dist).map(([name, value]) => ({
            name: labels[name] ?? name,
            value,
            itemStyle: {
              color: name === 'danger' ? '#ff4d4f' : name === 'warning' ? '#faad14' : '#52c41a',
            },
          })),
        },
      ],
    }
  }, [data])

  const signalColumns: ColumnsType<SignalRow> = [
    { title: '代码', dataIndex: 'code', width: 110 },
    { title: '名称', dataIndex: 'name', width: 120 },
    {
      title: '信号',
      dataIndex: 'signal',
      width: 80,
      render: (v: number) => (v > 0 ? <Tag color="red">买入</Tag> : <Tag color="green">卖出</Tag>),
    },
    { title: '策略', dataIndex: 'strategy', width: 110 },
    { title: '价格', dataIndex: 'price', width: 90, render: (v: number) => v?.toFixed(2) ?? '-' },
    {
      title: '触发原因',
      dataIndex: 'reason',
      ellipsis: true,
    },
  ]

  const historyColumns: ColumnsType<HistoryRow> = [
    { title: '代码', dataIndex: 'code', width: 110 },
    { title: '名称', dataIndex: 'name', width: 120 },
    { title: '原状态', dataIndex: 'old_state', width: 90, render: (v) => <StateTag state={v} /> },
    { title: '新状态', dataIndex: 'new_state', width: 90, render: (v) => <StateTag state={v} /> },
    { title: '价格', dataIndex: 'price', width: 90, render: (v: number) => v?.toFixed(2) ?? '-' },
    { title: '原因', dataIndex: 'reason', ellipsis: true },
  ]

  const alertColumns: ColumnsType<AlertRow> = [
    { title: '代码', dataIndex: 'code', width: 110 },
    { title: '名称', dataIndex: 'name', width: 110 },
    { title: '级别', dataIndex: 'level', width: 80, render: (v) => <AlertLevelTag level={v} /> },
    { title: '类型', dataIndex: 'title', width: 130 },
    { title: '内容', dataIndex: 'message', ellipsis: true },
    {
      title: '日期',
      dataIndex: 'trade_date',
      width: 110,
    },
  ]

  const ds = data?.data_status
  const bt = data?.last_backtest

  return (
    <Spin spinning={loading}>
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Card
          title={<Title level={4} style={{ margin: 0 }}>仪表盘</Title>}
          extra={
            <Space>
              <Text type="secondary">数据日期：{data?.date ?? '-'}</Text>
              <Button icon={<ReloadOutlined />} onClick={load}>
                刷新
              </Button>
              <Button
                type="primary"
                icon={<SyncOutlined />}
                loading={updating}
                onClick={handleUpdate}
              >
                立即更新追踪
              </Button>
            </Space>
          }
        >
          {ds && !ds.ready ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message="通达信数据目录未就绪"
              description={
                <span>
                  当前配置：<Text code>{ds.tdx_dir || '未配置'}</Text>。
                  请前往「数据管理」或「设置」页面配置正确的通达信安装目录（需包含
                  <Text code>vipdoc/sh/lday</Text> 等子目录）。
                </span>
              }
            />
          ) : null}

          <Row gutter={[12, 12]}>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title="关注池" value={data?.watch_count ?? 0} suffix="只" precision={0} />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title="追踪中" value={data?.tracked_count ?? 0} suffix="只" precision={0} />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard title="持仓中" value={data?.holding_count ?? 0} suffix="只" precision={0} />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard
                title="今日新信号"
                value={data?.today_signals?.length ?? 0}
                suffix="个"
                precision={0}
              />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard
                title="今日状态变更"
                value={data?.today_state_changes?.length ?? 0}
                suffix="次"
                precision={0}
              />
            </Col>
            <Col xs={12} sm={8} md={6} lg={4}>
              <MetricCard
                title="合计浮动盈亏"
                value={data?.total_unrealized_pnl ?? 0}
                colorBySign
                tip="关注池中已录入成本价与持股数的持仓合计浮动盈亏"
              />
            </Col>
          </Row>
        </Card>

        <Row gutter={[12, 12]}>
          <Col xs={24} lg={8}>
            <Card title="数据状态" size="small">
              <Descriptions column={1} size="small">
                <Descriptions.Item label="通达信目录">
                  {ds?.tdx_dir || '-'}
                </Descriptions.Item>
                <Descriptions.Item label="vipdoc">
                  {ds?.vipdoc || '-'}
                </Descriptions.Item>
                <Descriptions.Item label="本地文件数">
                  {ds?.total_files ?? 0}
                </Descriptions.Item>
                <Descriptions.Item label="最新数据日期">
                  {ds?.last_data_date || '-'}
                </Descriptions.Item>
                <Descriptions.Item label="pytdx">
                  {ds?.pytdx?.available ? (
                    <Tag color="green">可用</Tag>
                  ) : (
                    <Tag color="orange">不可用（已回退内置解析器）</Tag>
                  )}
                </Descriptions.Item>
                <Descriptions.Item label="复权模式">
                  <Tag color={ds?.adjust === 'none' ? 'default' : 'orange'}>{ds?.adjust}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label="缓存格式">{ds?.cache?.format}</Descriptions.Item>
                <Descriptions.Item label="名称映射">
                  {ds?.names?.available ? `${ds.names.count} 条` : '未配置'}
                </Descriptions.Item>
              </Descriptions>
              {ds?.adjust_warning ? (
                <Alert type="info" showIcon message={ds.adjust_warning} style={{ marginTop: 8 }} />
              ) : null}
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Card title="状态分布" size="small">
              {Object.keys(data?.state_distribution ?? {}).length ? (
                <EChart option={stateOption} height={300} />
              ) : (
                <Empty description="关注池为空" />
              )}
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Card title="风险分布" size="small">
              {Object.keys(data?.risk_distribution ?? {}).length ? (
                <EChart option={riskOption} height={300} />
              ) : (
                <Empty description="暂无风险数据" />
              )}
            </Card>
          </Col>
        </Row>

        {bt ? (
          <Card
            title="最新回测绩效摘要"
            size="small"
            extra={<Text type="secondary">{bt.strategy_name}　{bt.start} ~ {bt.end}</Text>}
          >
            <Row gutter={[12, 12]}>
              <Col xs={12} sm={8} md={6}>
                <MetricCard
                  title="总收益率"
                  value={bt.total_return}
                  percent
                  colorBySign
                  tip="来自最近一次回测结果缓存"
                />
              </Col>
              <Col xs={12} sm={8} md={6}>
                <MetricCard title="最大回撤" value={bt.max_drawdown} percent colorBySign invert />
              </Col>
              <Col xs={12} sm={8} md={6}>
                <MetricCard title="夏普比率" value={bt.sharpe} />
              </Col>
              <Col xs={12} sm={8} md={6}>
                <Text type="secondary">如需查看完整结果，请前往「回测」页面重新运行。</Text>
              </Col>
            </Row>
          </Card>
        ) : null}

        <Row gutter={[12, 12]}>
          <Col xs={24} lg={12}>
            <Card title={`今日信号（${data?.today_signals?.length ?? 0}）`} size="small">
              <Table
                size="small"
                rowKey={(r) => `${r.code}-${r.id}`}
                columns={signalColumns}
                dataSource={data?.today_signals ?? []}
                pagination={{ pageSize: 6, size: 'small' }}
                locale={{ emptyText: <Empty description="今日暂无新信号" /> }}
              />
            </Card>
          </Col>
          <Col xs={24} lg={12}>
            <Card title={`今日状态变更（${data?.today_state_changes?.length ?? 0}）`} size="small">
              <Table
                size="small"
                rowKey={(r) => `${r.code}-${r.id}`}
                columns={historyColumns}
                dataSource={data?.today_state_changes ?? []}
                pagination={{ pageSize: 6, size: 'small' }}
                locale={{ emptyText: <Empty description="今日暂无状态变更" /> }}
              />
            </Card>
          </Col>
        </Row>

        <Card title={`风险提醒（${data?.today_alerts?.length ?? 0}）`} size="small">
          <Table
            size="small"
            rowKey="id"
            columns={alertColumns}
            dataSource={data?.today_alerts ?? []}
            pagination={{ pageSize: 8, size: 'small' }}
            locale={{ emptyText: <Empty description="今日暂无风险提醒" /> }}
          />
        </Card>

        <Card title="持仓概览" size="small">
          {(data?.holdings ?? []).length ? (
            <List
              size="small"
              dataSource={data?.holdings ?? []}
              renderItem={(item) => (
                <List.Item
                  actions={[
                    <Text key="pnl" style={{ color: changeColor(item.unrealized_pct) }}>
                      {pct(item.unrealized_pct)}
                    </Text>,
                    <StateTag key="state" state={item.state} />,
                  ]}
                >
                  <List.Item.Meta
                    title={`${item.code} ${item.name}`}
                    description={
                      <Space size={16}>
                        <span>成本 {item.cost_price?.toFixed(2) ?? '-'}</span>
                        <span>现价 {item.price?.toFixed(2) ?? '-'}</span>
                        <span>持股 {item.shares ?? '-'}</span>
                        <span>浮盈 {item.unrealized_pnl?.toFixed(2) ?? '-'}</span>
                        <span>数据日期 {item.data_date ?? '-'}</span>
                      </Space>
                    }
                  />
                </List.Item>
              )}
            />
          ) : (
            <Empty description="暂无持仓记录（可在「追踪看板」中录入成本价与持股数）" />
          )}
        </Card>
      </Space>
    </Spin>
  )
}
