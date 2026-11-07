'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent } from 'react';
import { PanelRightCloseIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Separator } from '@/components/ui/separator';
import {
    Sheet,
    SheetContent,
    SheetHeader,
    SheetTitle,
} from '@/components/ui/sheet';
import {
    SidebarInset,
    SidebarProvider,
    SidebarTrigger,
} from '@/components/ui/sidebar';
import { Textarea } from '@/components/ui/textarea';
import { ChatRun } from '@/features/agent/chat-run';
import { ModelConfig } from '@/features/agent/model-config';
import { ResizeHandle } from '@/features/agent/resize-handle';
import { SessionSidebar } from '@/features/agent/session-sidebar';
import { Timeline } from '@/features/agent/timeline';
import { mergeEvents, terminal } from '@/features/agent/timeline-data';
import type { AgentEvent } from '@/features/agent/timeline-data';

type Session = { id: string; title: string };
type RecordItem = { type: string; event?: AgentEvent };

export function Chat() {
    const [sessions, setSessions] = useState<Session[]>([]);
    const [session, setSession] = useState('');
    const [order, setOrder] = useState<string[]>([]);
    const [events, setEvents] = useState<Record<string, AgentEvent[]>>({});
    const [content, setContent] = useState('');
    const [pending, setPending] = useState(false);
    const [error, setError] = useState('');
    const [configOpen, setConfigOpen] = useState(false);
    const [model, setModel] = useState('');
    const [generation, setGeneration] = useState(0);
    const [timelineOpen, setTimelineOpen] = useState(false);
    const [rightOpen, setRightOpen] = useState(true);
    const [leftWidth, setLeftWidth] = useState(256);
    const [rightWidth, setRightWidth] = useState(320);
    const [wide, setWide] = useState(false);
    const timelineView = useRef({
        expanded: new Map<string, boolean>(),
        scroll: new Map<string, number>(),
    });
    const followChat = useRef(true);
    const initialized = useRef(false);
    const currentSession = useRef('');
    const composing = useRef(false);
    const composedAt = useRef(0);
    const scrollRef = useRef<HTMLDivElement>(null);
    const running = Object.values(events).find(
        (items) => items.length && !terminal(items.at(-1))
    )?.[0]?.run_id;

    const list = useCallback(async () => {
        const response = await fetch('/api/sessions', { cache: 'no-store' });
        if (!response.ok) throw new Error('读取会话失败');
        setSessions(await response.json());
    }, []);
    const select = useCallback((id: string) => {
        if (currentSession.current === id) return;
        currentSession.current = id;
        timelineView.current = {
            expanded: new Map(),
            scroll: new Map(),
        };
        followChat.current = true;
        setTimelineOpen(false);
        sessionStorage.setItem('agent-session', id);
        setEvents({});
        setOrder([]);
        setSession(id);
        setContent('');
        setError('');
    }, []);
    const create = useCallback(async () => {
        const response = await fetch('/api/sessions', { method: 'POST' });
        if (!response.ok) throw new Error('创建会话失败');
        const result = await response.json();
        select(result.id);
        await list();
    }, [list, select]);

    const remove = useCallback(
        async (id: string) => {
            const response = await fetch(`/api/sessions/${id}`, {
                method: 'DELETE',
            });
            if (!response.ok) {
                setError('删除会话失败');
                return;
            }
            setSessions((items) => items.filter((item) => item.id !== id));
            if (currentSession.current === id) {
                select('');
                await create();
            }
        },
        [create, select]
    );

    useEffect(() => {
        const media = window.matchMedia('(min-width: 1280px)');
        const update = () => {
            setWide(media.matches);
            if (media.matches) setTimelineOpen(false);
        };
        update();
        media.addEventListener('change', update);
        return () => media.removeEventListener('change', update);
    }, []);

    useEffect(() => {
        if (initialized.current) return;
        initialized.current = true;
        const navigation = performance.getEntriesByType('navigation')[0] as
            | PerformanceNavigationTiming
            | undefined;
        if (window.opener && navigation?.type === 'navigate')
            sessionStorage.removeItem('agent-session');
        const saved = sessionStorage.getItem('agent-session');
        if (saved) select(saved);
        else void create();
        void list();
        void fetch('/api/config')
            .then((response) => {
                if (!response.ok) throw new Error('读取配置失败');
                return response.json();
            })
            .then((config) => setModel(config.model));
    }, [create, list, select]);

    useEffect(() => {
        if (!session) return;
        let cancelled = false;
        const streams: EventSource[] = [];
        async function load() {
            const response = await fetch(`/api/sessions/${session}`, {
                cache: 'no-store',
            });
            if (response.status === 404) {
                if (!cancelled) {
                    select('');
                    await create();
                }
                return;
            }
            if (!response.ok) throw new Error('读取会话失败');
            const result = await response.json();
            const ids = [
                ...new Set<string>(
                    (result.records as RecordItem[])
                        .filter((record) => record.type === 'event')
                        .map((record) => record.event!.run_id)
                ),
            ];
            if (!cancelled) setOrder(ids);
            await Promise.all(
                ids.map(async (id) => {
                    const path = `/api/sessions/${session}/runs/${id}`;
                    const snapshot = await fetch(`${path}/events`, {
                        cache: 'no-store',
                    });
                    if (!snapshot.ok) throw new Error('读取执行记录失败');
                    const items: AgentEvent[] = await snapshot.json();
                    if (cancelled) return;
                    setEvents((previous) => ({
                        ...previous,
                        [id]: mergeEvents(previous[id] ?? [], items),
                    }));
                    if (terminal(items.at(-1))) return;
                    const stream = new EventSource(
                        `${path}/stream?after=${items.at(-1)?.seq ?? 0}`
                    );
                    streams.push(stream);
                    stream.onmessage = (message) => {
                        if (cancelled) return;
                        setError('');
                        const event: AgentEvent = JSON.parse(message.data);
                        setEvents((previous) => {
                            const current = previous[id] ?? [];
                            const merged = mergeEvents(current, [event]);
                            return merged === current
                                ? previous
                                : { ...previous, [id]: merged };
                        });
                        if (terminal(event)) stream.close();
                    };
                    stream.onerror = () => {
                        if (!cancelled) setError('执行记录连接失败');
                    };
                })
            );
        }
        void load();
        return () => {
            cancelled = true;
            streams.forEach((stream) => stream.close());
        };
    }, [session, generation, create, select]);

    useEffect(() => {
        const viewport = scrollRef.current?.querySelector<HTMLElement>(
            '[data-slot="scroll-area-viewport"]'
        );
        if (!viewport) return;
        const track = () => {
            followChat.current =
                viewport.scrollHeight -
                    viewport.scrollTop -
                    viewport.clientHeight <
                48;
        };
        viewport.addEventListener('scroll', track);
        return () => viewport.removeEventListener('scroll', track);
    }, []);

    useEffect(() => {
        const viewport = scrollRef.current?.querySelector<HTMLElement>(
            '[data-slot="scroll-area-viewport"]'
        );
        if (viewport && followChat.current)
            viewport.scrollTop = viewport.scrollHeight;
    }, [events]);

    const post = useCallback(async (path: string, body?: object) => {
        setPending(true);
        setError('');
        try {
            const response = await fetch(path, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: body ? JSON.stringify(body) : undefined,
            });
            if (!response.ok) {
                const result = await response.json();
                throw new Error(
                    typeof result.detail === 'string'
                        ? result.detail
                        : '请求失败'
                );
            }
            return await response.json();
        } finally {
            setPending(false);
        }
    }, []);
    async function send() {
        if (!session || !content.trim() || pending) return;
        const sentSession = session;
        await post(`/api/sessions/${sentSession}/messages`, {
            content,
        });
        if (currentSession.current === sentSession) {
            setContent('');
            setGeneration((value) => value + 1);
        }
    }
    function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
        if (event.key !== 'Enter' || event.shiftKey) return;
        if (
            composing.current ||
            event.nativeEvent.isComposing ||
            event.keyCode === 229
        )
            return;
        event.preventDefault();
        if (Date.now() - composedAt.current >= 100) void send();
    }

    return (
        <SidebarProvider
            className='h-dvh min-h-0 overflow-hidden'
            style={{ '--sidebar-width': `${leftWidth}px` } as CSSProperties}
        >
            <SessionSidebar
                model={model}
                width={leftWidth}
                reserved={320 + (wide && rightOpen ? rightWidth : 0)}
                onWidthChange={setLeftWidth}
                sessions={sessions}
                current={session}
                onSelect={select}
                onDelete={(id) => void remove(id)}
                onNewSession={() => void create()}
                onOpenConfig={() => setConfigOpen(true)}
            />
            <SidebarInset className='h-dvh min-h-0 min-w-0 overflow-hidden'>
                <header className='flex h-12 shrink-0 items-center gap-2 border-b px-3'>
                    <SidebarTrigger className='-ml-1' />
                    <Separator orientation='vertical' className='h-4!' />
                    <span className='truncate text-sm font-medium'>
                        {sessions.find((item) => item.id === session)?.title ||
                            '会话'}
                    </span>
                    <Button
                        type='button'
                        variant='ghost'
                        size='sm'
                        className='ml-auto'
                        aria-expanded={wide ? rightOpen : timelineOpen}
                        onClick={() =>
                            wide
                                ? setRightOpen((open) => !open)
                                : setTimelineOpen(true)
                        }
                    >
                        执行时间线
                    </Button>
                </header>
                <ScrollArea ref={scrollRef} className='min-h-0 flex-1'>
                    <section
                        aria-label='对话'
                        aria-live='polite'
                        className='mx-auto w-full max-w-3xl space-y-8 px-4 py-8 sm:px-6'
                    >
                        {order
                            .filter((id) => events[id]?.length)
                            .map((id) => (
                                <ChatRun
                                    key={id}
                                    items={events[id]}
                                    pending={pending}
                                    session={session}
                                    post={post}
                                />
                            ))}
                    </section>
                </ScrollArea>
                <form
                    className='shrink-0 px-4 pb-4 pt-2 sm:px-6'
                    onSubmit={(event) => {
                        event.preventDefault();
                        void send();
                    }}
                >
                    <div className='mx-auto w-full max-w-3xl space-y-2'>
                        {error && (
                            <p
                                role='alert'
                                className='px-3 text-sm text-destructive'
                            >
                                {error}
                            </p>
                        )}
                        <div className='flex items-end gap-1.5 rounded-2xl border bg-card p-2'>
                            <label htmlFor='message' className='sr-only'>
                                消息
                            </label>
                            <Textarea
                                id='message'
                                value={content}
                                onChange={(event) =>
                                    setContent(event.target.value)
                                }
                                onKeyDown={keyDown}
                                onCompositionStart={() => {
                                    composing.current = true;
                                }}
                                onCompositionEnd={() => {
                                    composing.current = false;
                                    composedAt.current = Date.now();
                                }}
                                disabled={pending || !session}
                                rows={1}
                                className='max-h-40 min-h-6 flex-1 resize-none border-0 bg-transparent px-2.5 py-1 shadow-none focus-visible:ring-0'
                            />
                            {running && (
                                <Button
                                    type='button'
                                    variant='ghost'
                                    size='sm'
                                    disabled={pending}
                                    onClick={() =>
                                        void post(
                                            `/api/sessions/${session}/runs/${running}/stop`
                                        )
                                    }
                                >
                                    停止
                                </Button>
                            )}
                            <Button
                                type='submit'
                                size='sm'
                                disabled={
                                    pending || !session || !content.trim()
                                }
                            >
                                发送
                            </Button>
                        </div>
                    </div>
                </form>
            </SidebarInset>
            <aside
                aria-label='执行时间线'
                style={{ width: rightWidth }}
                className={`relative hidden shrink-0 flex-col border-l bg-muted/20 ${
                    rightOpen ? 'xl:flex' : ''
                }`}
            >
                <div className='flex h-12 shrink-0 items-center justify-between border-b px-4'>
                    <span className='text-sm font-medium'>执行时间线</span>
                    <Button
                        type='button'
                        variant='ghost'
                        size='icon-sm'
                        aria-label='收起执行时间线'
                        onClick={() => setRightOpen(false)}
                    >
                        <PanelRightCloseIcon />
                    </Button>
                </div>
                {wide && rightOpen && (
                    <Timeline
                        key={session}
                        session={session}
                        order={order}
                        events={events}
                        view={timelineView.current}
                    />
                )}
                {wide && rightOpen && (
                    <ResizeHandle
                        side='right'
                        width={rightWidth}
                        min={280}
                        max={720}
                        reserved={leftWidth + 320}
                        onChange={setRightWidth}
                    />
                )}
            </aside>
            <Sheet open={timelineOpen} onOpenChange={setTimelineOpen}>
                <SheetContent
                    aria-describedby={undefined}
                    className='p-0 xl:hidden'
                    style={{
                        width: rightWidth,
                        maxWidth: 'calc(100vw - 48px)',
                    }}
                >
                    <SheetHeader className='h-12 shrink-0 justify-center border-b px-4 py-0'>
                        <SheetTitle>执行时间线</SheetTitle>
                    </SheetHeader>
                    {!wide && (
                        <Timeline
                            key={session}
                            session={session}
                            order={order}
                            events={events}
                            view={timelineView.current}
                        />
                    )}
                    <ResizeHandle
                        side='right'
                        width={rightWidth}
                        min={280}
                        max={720}
                        reserved={48}
                        onChange={setRightWidth}
                    />
                </SheetContent>
            </Sheet>
            <ModelConfig
                open={configOpen}
                running={!!running}
                onOpenChange={setConfigOpen}
                onSaved={setModel}
            />
        </SidebarProvider>
    );
}
