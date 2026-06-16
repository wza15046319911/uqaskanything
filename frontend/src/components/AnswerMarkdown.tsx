import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface AnswerMarkdownProps {
  text: string
}

interface HastNode {
  type: string
  tagName?: string
  value?: string
  properties?: Record<string, unknown>
  children?: HastNode[]
}

const COURSE_CODE = /\b([A-Z]{4}\d{4})\b/

// 把文本节点里的课程码(CSSE1001)拆成 <code>,其余原样保留
function splitCourseCodes(value: string): HastNode[] {
  const parts = value.split(COURSE_CODE)
  if (parts.length === 1) return [{ type: 'text', value }]
  return parts
    .filter((p) => p !== '')
    .map((p) =>
      /^[A-Z]{4}\d{4}$/.test(p)
        ? {
            type: 'element',
            tagName: 'code',
            properties: {},
            children: [{ type: 'text', value: p }],
          }
        : { type: 'text', value: p },
    )
}

function visit(node: HastNode): void {
  if (!node.children) return
  if (node.type === 'element' && (node.tagName === 'code' || node.tagName === 'pre')) return
  const next: HastNode[] = []
  for (const child of node.children) {
    if (child.type === 'text' && child.value) {
      next.push(...splitCourseCodes(child.value))
    } else {
      visit(child)
      next.push(child)
    }
  }
  node.children = next
}

function rehypeCourseCodes() {
  return (tree: HastNode) => visit(tree)
}

export default function AnswerMarkdown({ text }: AnswerMarkdownProps) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeCourseCodes]}
      components={{
        a: ({ ...props }) => <a target="_blank" rel="noopener noreferrer" {...props} />,
      }}
    >
      {text}
    </ReactMarkdown>
  )
}
