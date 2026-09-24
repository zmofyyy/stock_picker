/**
 * Markdown 渲染组件。
 *
 * 使用 react-markdown + remark-gfm，支持标题、表格、列表、代码块、链接、引用。
 * 提供「复制 Markdown」「导出 .md」「导出 .html」三个操作。
 */
import { useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Button, Card, Space, Tooltip, Typography, message } from 'antd'
import { CopyOutlined, DownloadOutlined, FileTextOutlined } from '@ant-design/icons'
import { downloadText } from '@/api'

const { Text } = Typography

export interface MarkdownViewProps {
  /** Markdown 原文 */
  content: string
  /** 卡片标题 */
  title?: string
  /** 导出文件名前缀 */
  filename?: string
  /** 是否显示导出按钮 */
  actions?: boolean
  /** 生成时间等附加信息 */
  subtitle?: string
  /** 是否可折叠 */
  collapsible?: boolean
}

export default function MarkdownView({
  content,
  title = '报告',
  filename = 'report',
  actions = true,
  subtitle,
  collapsible = false,
}: MarkdownViewProps) {
  const [collapsed, setCollapsed] = useState(false)

  const html = useMemo(() => {
    // 用极简转换生成可下载的独立 HTML（与后端 to_html 风格一致）
    const body = content
      .split('\n')
      .map((line) => {
        if (line.startsWith('#')) {
          const level = Math.min(line.match(/^#+/)?.[0].length ?? 1, 6)
          return `<h${level}>${escapeHtml(line.replace(/^#+\s*/, ''))}</h${level}>`
        }
        if (line.startsWith('|')) return `<pre>${escapeHtml(line)}</pre>`
        if (line.trim() === '') return ''
        return `<p>${escapeHtml(line)}</p>`
      })
      .join('\n')
    return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>${escapeHtml(
      title,
    )}</title><style>body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;max-width:1100px;margin:32px auto;line-height:1.75;padding:0 16px;color:#1f1f1f}h1{border-bottom:2px solid #eee;padding-bottom:8px}h2{color:#1677ff;margin-top:26px}pre{background:#f7f7f7;padding:6px 10px;border-radius:6px;font-size:12px;overflow:auto}code{background:#f2f2f2;padding:1px 5px;border-radius:3px}</style></head><body>${body}</body></html>`
  }, [content, title])

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(content)
      message.success('已复制 Markdown 到剪贴板')
    } catch {
      message.warning('浏览器拒绝了剪贴板访问，请手动选择复制')
    }
  }

  return (
    <Card
      title={
        <Space>
          <FileTextOutlined />
          <span>{title}</span>
          {subtitle ? <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>{subtitle}</Text> : null}
        </Space>
      }
      extra={
        actions ? (
          <Space>
            {collapsible ? (
              <Button size="small" onClick={() => setCollapsed((v) => !v)}>
                {collapsed ? '展开' : '收起'}
              </Button>
            ) : null}
            <Tooltip title="复制 Markdown 全文">
              <Button size="small" icon={<CopyOutlined />} onClick={handleCopy}>
                复制
              </Button>
            </Tooltip>
            <Button
              size="small"
              icon={<DownloadOutlined />}
              onClick={() => downloadText(`${filename}.md`, content, 'text/markdown')}
            >
              .md
            </Button>
            <Button
              size="small"
              icon={<DownloadOutlined />}
              onClick={() => downloadText(`${filename}.html`, html, 'text/html')}
            >
              .html
            </Button>
          </Space>
        ) : null
      }
      styles={{ body: { maxHeight: collapsed ? 120 : undefined, overflow: 'auto' } }}
    >
      <div className="markdown-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{content || '_暂无内容_'}</ReactMarkdown>
      </div>
    </Card>
  )
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}
