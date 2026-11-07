"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import type { KeyboardEvent } from "react"
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar"
import { Textarea } from "@/components/ui/textarea"
import { ModelConfig } from "@/features/agent/model-config"
import { SessionSidebar } from "@/features/agent/session-sidebar"

type AgentEvent = { run_id: string; seq: number; time: string; type: string; data: Record<string, any> }
type Session = { id: string; title: string }
type RecordItem = { type: string; event?: AgentEvent }
const terminal = (event?: AgentEvent) => event && ["run.completed", "run.finished"].includes(event.type)
const statuses: Record<string, string> = { stopped: "已停止", limited: "达到轮次上限", interrupted: "执行中断", failed: "执行失败" }

export function Chat() {
  const [sessions, setSessions] = useState<Session[]>([])
  const [session, setSession] = useState("")
  const [order, setOrder] = useState<string[]>([])
  const [events, setEvents] = useState<Record<string, AgentEvent[]>>({})
  const [content, setContent] = useState("")
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const [configOpen, setConfigOpen] = useState(false)
  const [model, setModel] = useState("")
  const [generation, setGeneration] = useState(0)
  const initialized = useRef(false)
  const currentSession = useRef("")
  const composing = useRef(false)
  const composedAt = useRef(0)
  const scrollRef = useRef<HTMLDivElement>(null)
  const running = Object.values(events).find((items) => items.length && !terminal(items.at(-1)))?.[0]?.run_id

  const list = useCallback(async () => {
    const response = await fetch("/api/sessions", { cache: "no-store" })
    if (!response.ok) throw new Error("读取会话失败")
    setSessions(await response.json())
  }, [])
  const select = useCallback((id: string) => {
    currentSession.current = id
    sessionStorage.setItem("agent-session", id)
    setEvents({})
    setOrder([])
    setSession(id)
    setError("")
  }, [])
  const create = useCallback(async () => {
    const response = await fetch("/api/sessions", { method: "POST" })
    if (!response.ok) throw new Error("创建会话失败")
    const result = await response.json()
    select(result.id)
    await list()
  }, [list, select])

  useEffect(() => {
    if (initialized.current) return
    initialized.current = true
    const navigation = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined
    if (window.opener && navigation?.type === "navigate") sessionStorage.removeItem("agent-session")
    const saved = sessionStorage.getItem("agent-session")
    if (saved) select(saved)
    else void create()
    void list()
    void fetch("/api/config").then((response) => {
      if (!response.ok) throw new Error("读取配置失败")
      return response.json()
    }).then((config) => setModel(config.model))
  }, [create, list, select])

  useEffect(() => {
    if (!session) return
    let cancelled = false
    const streams: EventSource[] = []
    async function load() {
      const response = await fetch(`/api/sessions/${session}`, { cache: "no-store" })
      if (!response.ok) throw new Error("读取会话失败")
      const result = await response.json()
      const ids = [...new Set<string>((result.records as RecordItem[]).filter((record) => record.type === "event").map((record) => record.event!.run_id))]
      if (!cancelled) setOrder(ids)
      await Promise.all(ids.map(async (id) => {
        const path = `/api/sessions/${session}/runs/${id}`
        const snapshot = await fetch(`${path}/events`, { cache: "no-store" })
        if (!snapshot.ok) throw new Error("读取执行记录失败")
        const items: AgentEvent[] = await snapshot.json()
        if (cancelled) return
        setEvents((previous) => ({ ...previous, [id]: items }))
        if (terminal(items.at(-1))) return
        const stream = new EventSource(`${path}/stream?after=${items.at(-1)?.seq ?? 0}`)
        streams.push(stream)
        stream.onmessage = (message) => {
          if (cancelled) return
          setError("")
          const event: AgentEvent = JSON.parse(message.data)
          setEvents((previous) => {
            const current = previous[id] ?? []
            if (current.some((item) => item.seq === event.seq)) return previous
            return { ...previous, [id]: [...current, event].sort((a, b) => a.seq - b.seq) }
          })
          if (terminal(event)) stream.close()
        }
        stream.onerror = () => { if (!cancelled) setError("执行记录连接失败") }
      }))
    }
    void load()
    return () => { cancelled = true; streams.forEach((stream) => stream.close()) }
  }, [session, generation])

  useEffect(() => {
    const viewport = scrollRef.current?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')
    if (viewport) viewport.scrollTop = viewport.scrollHeight
  }, [events])

  async function post(path: string, body?: object) {
    setPending(true)
    setError("")
    try {
      const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined })
      if (!response.ok) {
        const result = await response.json()
        throw new Error(typeof result.detail === "string" ? result.detail : "请求失败")
      }
      return true
    } finally { setPending(false) }
  }
  async function send() {
    if (!session || !content.trim() || pending) return
    const sentSession = session
    if (await post(`/api/sessions/${sentSession}/messages`, { content }) && currentSession.current === sentSession) {
      setContent("")
      setGeneration((value) => value + 1)
    }
  }
  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) return
    if (composing.current || event.nativeEvent.isComposing || event.keyCode === 229) return
    event.preventDefault()
    if (Date.now() - composedAt.current >= 100) void send()
  }

  return (
    <SidebarProvider className="h-dvh min-h-0 overflow-hidden">
      <SessionSidebar model={model} sessions={sessions} current={session} onSelect={select} onNewSession={() => void create()} onOpenConfig={() => setConfigOpen(true)} />
      <SidebarInset className="h-dvh min-h-0 overflow-hidden">
        <header className="flex h-12 shrink-0 items-center gap-2 border-b px-3">
          <SidebarTrigger className="-ml-1" /><Separator orientation="vertical" className="h-4!" />
          <span className="truncate text-sm font-medium">{sessions.find((item) => item.id === session)?.title || "会话"}</span>
        </header>
        <ScrollArea ref={scrollRef} className="min-h-0 flex-1">
          <section aria-label="对话" aria-live="polite" className="mx-auto w-full max-w-3xl space-y-8 px-4 py-8 sm:px-6">
            {order.filter((id) => events[id]?.length).map((id) => {
              const items = events[id]
              const run = items[0].run_id
              const last = items.at(-1)
              return <article key={run} className="space-y-4">
                {items.map((event) => {
                  const data = event.data
                  if (event.type === "run.started" || event.type === "steer.queued") return <div key={event.seq} className="ml-auto w-fit max-w-[85%] whitespace-pre-wrap rounded-3xl bg-secondary px-4 py-2.5">{event.type === "run.started" ? data.input : data.content}</div>
                  if (event.type === "model.request") {
                    const text = items.filter((item) => item.type === "model.delta" && item.data.turn_index === data.turn_index).map((item) => item.data.fragments.text || "").join("")
                    return text ? <div key={event.seq} className="chat-markdown px-1 leading-7"><Markdown remarkPlugins={[remarkGfm]} skipHtml>{text}</Markdown></div> : null
                  }
                  if (event.type === "tool.started") {
                    const result = items.find((item) => item.type === "tool.completed" && item.data.turn_index === data.turn_index && item.data.tool_call_id === data.tool_call_id)
                    return <details key={event.seq} className="rounded-xl border bg-card px-4 py-3 text-sm"><summary className="cursor-pointer font-medium">{data.name}</summary><pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all">{data.arguments}</pre>{result && <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all">{result.data.result}</pre>}</details>
                  }
                  if (event.type === "tool.confirmation_requested" && !terminal(last) && !items.some((item) => item.type === "tool.confirmation_resolved" && item.data.turn_index === data.turn_index && item.data.tool_call_id === data.tool_call_id)) return <div key={event.seq} className="rounded-xl border p-4 space-y-3"><p>删除待办 {data.task.id}：{data.task.title}</p><div className="flex gap-2">{(["approved", "rejected"] as const).map((decision) => <Button key={decision} disabled={pending} variant={decision === "approved" ? "default" : "outline"} onClick={() => void post(`/api/sessions/${session}/runs/${run}/confirm`, { turn_index: data.turn_index, tool_call_id: data.tool_call_id, decision })}>{decision === "approved" ? "确认删除" : "拒绝"}</Button>)}</div></div>
                  if (event.type === "run.finished") return <p key={event.seq} className="text-sm text-muted-foreground">{statuses[data.status] || data.status}{data.error ? `：${data.error}` : ""}</p>
                  return null
                })}
              </article>
            })}
          </section>
        </ScrollArea>
        <form className="shrink-0 px-4 pb-4 pt-2 sm:px-6" onSubmit={(event) => { event.preventDefault(); void send() }}>
          <div className="mx-auto w-full max-w-3xl space-y-2">
            {error && <p role="alert" className="px-3 text-sm text-destructive">{error}</p>}
            <div className="flex items-end gap-1.5 rounded-2xl border bg-card p-2">
              <label htmlFor="message" className="sr-only">消息</label>
              <Textarea id="message" value={content} onChange={(event) => setContent(event.target.value)} onKeyDown={keyDown} onCompositionStart={() => { composing.current = true }} onCompositionEnd={() => { composing.current = false; composedAt.current = Date.now() }} disabled={pending || !session} rows={1} className="max-h-40 min-h-6 flex-1 resize-none border-0 bg-transparent px-2.5 py-1 shadow-none focus-visible:ring-0" />
              {running && <Button type="button" variant="ghost" size="sm" disabled={pending} onClick={() => void post(`/api/sessions/${session}/runs/${running}/stop`)}>停止</Button>}
              <Button type="submit" size="sm" disabled={pending || !session || !content.trim()}>发送</Button>
            </div>
          </div>
        </form>
      </SidebarInset>
      <aside aria-label="面板" className="hidden w-80 shrink-0 flex-col border-l bg-muted/20 xl:flex">
        <div className="flex h-12 shrink-0 items-center border-b px-4">
          <span className="text-sm font-medium text-muted-foreground">面板</span>
        </div>
      </aside>
      <ModelConfig open={configOpen} running={!!running} onOpenChange={setConfigOpen} onSaved={setModel} />
    </SidebarProvider>
  )
}
