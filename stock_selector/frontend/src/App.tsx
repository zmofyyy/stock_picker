/**
 * 应用根组件。
 *
 * 使用侧边导航 + 路由切换 7 个功能页面：
 * 仪表盘 / 数据管理 / 选股 / 回测 / 追踪看板 / 报告中心 / 设置。
 */
import { useEffect, useState } from 'react'
import { ConfigProvider, Layout, Menu, Space, Tag, Typography, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import {
  BarChartOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  LineChartOutlined,
  SearchOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import { api } from '@/api'
import Dashboard from '@/pages/Dashboard'
import DataManager from '@/pages/DataManager'
import Screener from '@/pages/Screener'
import Backtest from '@/pages/Backtest'
import Tracker from '@/pages/Tracker'
import Reports from '@/pages/Reports'
import Settings from '@/pages/Settings'

dayjs.locale('zh-cn')

const { Header, Sider, Content } = Layout
const { Text } = Typography

type PageKey = 'dashboard' | 'data' | 'screen' | 'backtest' | 'tracker' | 'reports' | 'settings'

const MENU = [
  { key: 'dashboard', icon: <DashboardOutlined />, label: '仪表盘' },
  { key: 'data', icon: <DatabaseOutlined />, label: '数据管理' },
  { key: 'screen', icon: <SearchOutlined />, label: '选股' },
  { key: 'backtest', icon: <LineChartOutlined />, label: '回测' },
  { key: 'tracker', icon: <BarChartOutlined />, label: '追踪看板' },
  { key: 'reports', icon: <FileTextOutlined />, label: '报告中心' },
  { key: 'settings', icon: <SettingOutlined />, label: '设置' },
]

/** 用 hash 做轻量路由，避免额外依赖 */
function useHashRoute(): [PageKey, (key: PageKey) => void] {
  const getKey = (): PageKey => {
    const raw = window.location.hash.replace(/^#\/?/, '')
    const found = MENU.find((m) => m.key === raw)
    return (found?.key as PageKey) ?? 'dashboard'
  }
  const [key, setKey] = useState<PageKey>(getKey)

  useEffect(() => {
    const onHash = () => setKey(getKey())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const navigate = (next: PageKey) => {
    window.location.hash = `#/${next}`
    setKey(next)
  }

  return [key, navigate]
}

export default function App() {
  const [page, navigate] = useHashRoute()
  const [health, setHealth] = useState<{ version: string } | null>(null)
  const [backendOk, setBackendOk] = useState(true)

  useEffect(() => {
    api
      .health()
      .then((h) => {
        setHealth(h)
        setBackendOk(true)
      })
      .catch(() => setBackendOk(false))
  }, [])

  const renderPage = () => {
    switch (page) {
      case 'data':
        return <DataManager />
      case 'screen':
        return <Screener />
      case 'backtest':
        return <Backtest />
      case 'tracker':
        return <Tracker />
      case 'reports':
        return <Reports />
      case 'settings':
        return <Settings />
      default:
        return <Dashboard />
    }
  }

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: { colorPrimary: '#1677ff', borderRadius: 6 },
      }}
    >
      <Layout style={{ minHeight: '100vh' }}>
        <Sider
          theme="light"
          breakpoint="lg"
          collapsedWidth={64}
          style={{ borderRight: '1px solid #f0f0f0' }}
        >
          <div style={{ padding: '16px 12px', textAlign: 'center' }}>
            <Text strong style={{ fontSize: 16 }}>
              A 股选股回测
            </Text>
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                stock_selector
              </Text>
            </div>
          </div>
          <Menu
            mode="inline"
            selectedKeys={[page]}
            items={MENU}
            onClick={({ key }) => navigate(key as PageKey)}
          />
        </Sider>

        <Layout>
          <Header
            style={{
              background: '#fff',
              borderBottom: '1px solid #f0f0f0',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '0 20px',
            }}
          >
            <Text strong style={{ fontSize: 15 }}>
              {MENU.find((m) => m.key === page)?.label}
            </Text>
            <Space>
              <Tag color={backendOk ? 'green' : 'red'}>
                {backendOk ? '后端在线' : '后端离线'}
              </Tag>
              {health ? <Tag color="blue">v{health.version}</Tag> : null}
              <Text type="secondary" style={{ fontSize: 12 }}>
                {dayjs().format('YYYY-MM-DD HH:mm')}
              </Text>
            </Space>
          </Header>

          <Content style={{ padding: 16, background: '#f5f6f8' }}>{renderPage()}</Content>
        </Layout>
      </Layout>
    </ConfigProvider>
  )
}
