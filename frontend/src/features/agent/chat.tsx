"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import type { KeyboardEvent } from "react"
import { PanelRightIcon, SparklesIcon } from "lucide-react"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"

import { Button } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar"
import { Textarea } from "@/components/ui/textarea"
import { ModelConfig } from "@/features/agent/model-config"
import { SessionSidebar } from "@/features/agent/session-sidebar"

const COMPOSITION_GRACE = 100

type ToolCall = { id: string; function: { name: string; arguments: string } }
type Message = { role: string; content: string | null; tool_calls?: ToolCall[]; tool_call_id?: string }
type Turn = { id: string; status: string; messages: Message[]; error?: string }

const statusText: Record<string, string> = {
  running: "执行中",
  stopping: "停止中",
  stopped: "已停止",
  limited: "达到轮次上限",
  interrupted: "执行中断",
  failed: "执行失败",
}

export function Chat() {
  const [turns, setTurns] = useState<Turn[]>([])
  const [content, setContent] = useState("")
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const [configOpen, setConfigOpen] = useState(false)
  const [model, setModel] = useState("")
  const composing = useRef(false)
  const composedAt = useRef(0)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const running = turns.find((turn) => turn.status === "running" || turn.status === "stopping")

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/turns", { cache: "no-store" })
      if (!response.ok) return
      setTurns(await response.json())
    } catch {
      // 轮询失败时保留上一份数据，等下一次刷新
    }
  }, [])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), 1000)
    return () => window.clearInterval(timer)
  }, [refresh])

  useEffect(() => {
    void (async () => {
      try {
        const response = await fetch("/api/config", { cache: "no-store" })
        if (!response.ok) return
        const config = await response.json()
        setModel(config.model ?? "")
      } catch {
        // 配置读不到时保持未配置状态
      }
    })()
  }, [])

  useEffect(() => {
    const viewport = scrollRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')
    if (viewport) viewport.scrollTop = viewport.scrollHeight
  }, [turns])

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) return
    const composingNow = composing.current || event.nativeEvent.isComposing || event.keyCode === 229
    const justComposed = Date.now() - composedAt.current < COMPOSITION_GRACE
    if (composingNow || justComposed) {
      if (justComposed) event.preventDefault()
      return
    }
    event.preventDefault()
    if (!running && !pending && content.trim()) void send("/api/turns", { content })
  }

  function newSession() {
    setContent("")
    setError("")
    textareaRef.current?.focus()
  }

  async function send(path: string, body?: object) {
    setPending(true)
    setError("")
    try {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      })
      if (!response.ok) {
        const result = await response.json()
        setError(typeof result.detail === "string" ? result.detail : "请求失败")
      } else {
        if (body) setContent("")
        await refresh()
      }
    } finally {
      setPending(false)
    }
  }

  return (
    <SidebarProvider className="h-dvh min-h-0 overflow-hidden">
      <SessionSidebar model={model} onNewSession={newSession} onOpenConfig={() => setConfigOpen(true)} />
      <SidebarInset className="h-dvh min-h-0 overflow-hidden">
        <header className="flex h-12 shrink-0 items-center gap-2 border-b px-3">
          <SidebarTrigger className="-ml-1" />
          <Separator orientation="vertical" className="h-4!" />
          <span className="truncate text-sm font-medium">新会话</span>
        </header>
        <div className="flex min-h-0 flex-1 flex-col">
          {turns.length === 0 ? (
            <div className="flex flex-1 items-center justify-center px-6">
              <Empty className="max-w-md border-0">
                <EmptyHeader>
                  <EmptyMedia variant="icon">
                    <SparklesIcon />
                  </EmptyMedia>
                  <EmptyTitle>今天想了解点什么？</EmptyTitle>
                  <EmptyDescription>输入消息开始对话，或先在侧栏配置模型。</EmptyDescription>
                </EmptyHeader>
              </Empty>
            </div>
          ) : (
            <ScrollArea ref={scrollRef} className="min-h-0 flex-1">
              <section aria-label="对话" aria-live="polite" className="mx-auto w-full max-w-3xl space-y-8 px-4 py-8 sm:px-6">
                {turns.map((turn) => (
                  <article key={turn.id} className="space-y-4">
                    <div className="ml-auto w-fit max-w-[85%] whitespace-pre-wrap rounded-3xl bg-secondary px-4 py-2.5">
                      {turn.messages[0].content}
                    </div>
                    {turn.status !== "running" && turn.status !== "stopping" && turn.messages.slice(1).map((message, index) => (
                      <div key={index} className="space-y-2">
                        {message.tool_calls?.map((call) => {
                          const result = turn.messages.find((item) => item.role === "tool" && item.tool_call_id === call.id)
                          return (
                            <details key={call.id} className="rounded-xl border bg-card px-4 py-3 text-sm">
                              <summary className="cursor-pointer font-medium">{call.function.name}</summary>
                              <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all">{call.function.arguments}</pre>
                              {result && <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all">{result.content}</pre>}
                            </details>
                          )
                        })}
                        {message.role === "assistant" && message.content && (
                          <div className="chat-markdown px-1 leading-7">
                            <Markdown
                              remarkPlugins={[remarkGfm]}
                              skipHtml
                              components={{ table: ({ node, ...props }) => <div className="overflow-x-auto"><table {...props} /></div> }}
                            >
                              {message.content}
                            </Markdown>
                          </div>
                        )}
                      </div>
                    ))}
                    {statusText[turn.status] && (
                      <div className="flex items-center gap-3 text-sm text-muted-foreground">
                        <span>{statusText[turn.status]}{turn.error && `：${turn.error}`}</span>
                        {["stopped", "limited", "interrupted", "failed"].includes(turn.status) && (
                          <Button variant="outline" size="sm" disabled={pending || !!running} onClick={() => void send(`/api/turns/${turn.id}/retry`)}>
                            重试
                          </Button>
                        )}
                      </div>
                    )}
                  </article>
                ))}
              </section>
            </ScrollArea>
          )}
          <form className="shrink-0 px-4 pb-4 pt-2 sm:px-6" onSubmit={(event) => {
            event.preventDefault()
            if (content.trim()) void send("/api/turns", { content })
          }}>
            <div className="mx-auto w-full max-w-3xl space-y-2">
              {error && <p role="alert" className="px-3 text-sm text-destructive">{error}</p>}
              <div className="flex items-end gap-1.5 rounded-2xl border border-border bg-card p-2 shadow-[0_1px_2px_oklch(0_0_0/0.05)] transition-colors focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/15">
                <label htmlFor="message" className="sr-only">消息</label>
                <Textarea
                  ref={textareaRef}
                  id="message"
                  value={content}
                  onChange={(event) => setContent(event.target.value)}
                  onKeyDown={handleKeyDown}
                  onCompositionStart={() => { composing.current = true }}
                  onCompositionEnd={() => { composing.current = false; composedAt.current = Date.now() }}
                  disabled={!!running || pending}
                  placeholder="给助手发消息…"
                  rows={1}
                  className="max-h-40 min-h-6 flex-1 resize-none rounded-lg border-0 bg-transparent px-2.5 py-1 text-sm leading-6 shadow-none focus-visible:ring-0"
                />
                {running && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="rounded-lg transition-colors"
                    disabled={pending || running.status === "stopping"}
                    onClick={() => void send(`/api/turns/${running.id}/stop`)}
                  >
                    停止
                  </Button>
                )}
                <Button
                  type="submit"
                  size="sm"
                  className="rounded-lg px-3.5 transition-colors disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100"
                  disabled={pending || !!running || !content.trim()}
                >
                  发送
                </Button>
              </div>
            </div>
          </form>
        </div>
      </SidebarInset>
      <aside aria-label="面板" className="hidden w-80 shrink-0 flex-col border-l bg-muted/20 xl:flex">
        <div className="flex h-12 shrink-0 items-center border-b px-4">
          <span className="text-sm font-medium text-muted-foreground">面板</span>
        </div>
        <div className="flex min-h-0 flex-1 p-3">
          <Empty className="border border-dashed">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <PanelRightIcon />
              </EmptyMedia>
              <EmptyTitle className="text-base">空白面板</EmptyTitle>
              <EmptyDescription>详情与工具区预留</EmptyDescription>
            </EmptyHeader>
          </Empty>
        </div>
      </aside>
      <ModelConfig open={configOpen} running={!!running} onOpenChange={setConfigOpen} onSaved={setModel} />
    </SidebarProvider>
  )
}
