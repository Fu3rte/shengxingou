'use client';

import { memo, useLayoutEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
    appendTimeline,
    cachedTokens,
    cacheHitRate,
    createTimeline,
    runCacheHitRate,
    terminal,
    tokenFields,
} from '@/features/agent/timeline-data';
import type {
    AgentEvent,
    TimelineData,
    TimelineRow,
    TimelineSection,
} from '@/features/agent/timeline-data';

const names: Record<string, string> = {
    'run.started': '运行开始 · 用户消息',
    'model.request': '模型请求',
    'model.response': '模型响应',
    'tool.started': '工具调用',
    'tool.completed': '工具结果',
    'tool.confirmation_requested': '确认请求',
    'tool.confirmation_resolved': '确认结果',
    'steer.queued': 'Steering 排队',
    'steer.applied': 'Steering 已应用',
    'run.completed': '运行完成',
    'run.finished': '运行结束',
};
const tokenNames: Record<string, string> = {
    prompt_tokens: '输入 Token',
    completion_tokens: '输出 Token',
    total_tokens: '总 Token',
};
const format = (value: unknown) =>
    typeof value === 'string' ? value : JSON.stringify(value, null, 2);
const durationText = (value: number) => `${(value / 1000).toFixed(2)} 秒`;
const rateText = (value: number | undefined) =>
    value === undefined ? '未知' : `${value.toFixed(2)}%`;
const timeFormat = new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
});

export type TimelineView = {
    expanded: Map<string, boolean>;
    scroll: Map<string, number>;
};
type Props = {
    session: string;
    order: string[];
    events: Record<string, AgentEvent[]>;
    view: TimelineView;
};

function Disclosure({
    id,
    title,
    children,
    view,
    className = '',
}: {
    id: string;
    title: ReactNode;
    children: ReactNode;
    view: TimelineView;
    className?: string;
}) {
    const [open, setOpen] = useState(() => view.expanded.get(id) ?? false);
    return (
        <details
            open={open}
            onToggle={(event) => {
                const next = event.currentTarget.open;
                view.expanded.set(id, next);
                setOpen(next);
            }}
            className={className}
        >
            <summary className='cursor-pointer font-medium'>{title}</summary>
            {children}
        </details>
    );
}

function Details({
    id,
    title,
    value,
    view,
}: {
    id: string;
    title: string;
    value: unknown;
    view: TimelineView;
}) {
    return (
        <Disclosure id={id} title={title} view={view} className='mt-2'>
            <pre className='mt-1 whitespace-pre-wrap break-all'>
                {format(value)}
            </pre>
        </Disclosure>
    );
}

const Row = memo(function Row({
    row,
    view,
}: {
    row: TimelineRow;
    view: TimelineView;
}) {
    const { event, duration } = row;
    const data = event.data;
    const id = `${event.run_id}:${event.seq}`;
    const cached = cachedTokens(data.usage);
    const fields = Object.entries(data).filter(
        ([key]) =>
            !['turn_index', 'messages', 'tools', 'message', 'usage'].includes(
                key
            )
    );
    return (
        <div className='border-t py-3 text-sm'>
            <div className='font-medium'>{names[event.type] ?? event.type}</div>
            <div className='mt-1 break-all text-xs text-muted-foreground'>
                #{event.seq} ·{' '}
                <time dateTime={event.time}>
                    {timeFormat.format(new Date(event.time))}
                </time>
                {data.turn_index != null && ` · 轮次 ${data.turn_index}`}
            </div>
            {duration !== undefined && (
                <div className='mt-1'>
                    耗时：{durationText(duration)}
                    {event.type === 'tool.completed' && data.name === 'todo'
                        ? '（可能包含确认等待）'
                        : ''}
                </div>
            )}
            {fields.map(([key, value]) => (
                <div key={key} className='mt-1 break-all'>
                    <span className='text-muted-foreground'>{key}：</span>
                    <span className='whitespace-pre-wrap'>{format(value)}</span>
                </div>
            ))}
            {data.usage &&
                tokenFields.map(
                    (field) =>
                        data.usage[field] != null && (
                            <div key={field}>
                                {tokenNames[field]}：{data.usage[field]}
                            </div>
                        )
                )}
            {event.type === 'model.response' && (
                <>
                    <div>缓存命中输入 Token：{cached ?? '未知'}</div>
                    <div>
                        输入缓存命中率：
                        {rateText(cacheHitRate(cached, data.usage?.prompt_tokens))}
                    </div>
                </>
            )}
            {data.messages != null && (
                <Details
                    id={`${id}:messages`}
                    title='请求消息'
                    value={data.messages}
                    view={view}
                />
            )}
            {data.tools != null && (
                <Details
                    id={`${id}:tools`}
                    title='工具声明'
                    value={data.tools}
                    view={view}
                />
            )}
            {data.message != null && (
                <Details
                    id={`${id}:message`}
                    title='完整响应'
                    value={data.message}
                    view={view}
                />
            )}
            <Details
                id={`${id}:raw`}
                title='原始事件'
                value={event}
                view={view}
            />
        </div>
    );
});

const Turn = memo(function Turn({
    section,
    run,
    finished,
    view,
}: {
    section: Extract<TimelineSection, { kind: 'turn' }>;
    run: string;
    finished: boolean;
    view: TimelineView;
}) {
    const request = section.rows.find(
        (row) => row.event.type === 'model.request'
    );
    const response = section.rows.find(
        (row) => row.event.type === 'model.response'
    );
    const status = response
        ? response.event.data.message
            ? '模型已响应'
            : '响应不完整'
        : finished
        ? '未完成'
        : '请求中';
    const usage = response?.event.data.usage;
    return (
        <Disclosure
            id={`${run}:turn:${section.turn}:${section.rows[0].event.seq}`}
            view={view}
            className='my-2 rounded-lg border px-3 py-2'
            title={
                <span>
                    Turn {section.turn} · {status}
                    {request?.event.data.model &&
                        ` · ${request.event.data.model}`}
                    {response?.duration !== undefined &&
                        ` · ${durationText(response.duration)}`}
                    {response &&
                        tokenFields.map(
                            (field) =>
                                ` · ${tokenNames[field]} ${
                                    usage?.[field] ?? '缺失'
                                }`
                        )}
                    {response &&
                        ` · 缓存命中输入 Token ${cachedTokens(usage) ?? '未知'} · 输入缓存命中率 ${rateText(cacheHitRate(cachedTokens(usage), usage?.prompt_tokens))}`}
                </span>
            }
        >
            {section.rows.map((row) => (
                <Row key={row.event.seq} row={row} view={view} />
            ))}
        </Disclosure>
    );
});

const Run = memo(function Run({
    session,
    run,
    events,
    view,
}: {
    session: string;
    run: string;
    events: AgentEvent[];
    view: TimelineView;
}) {
    const cache = useRef<TimelineData>(createTimeline());
    if (
        events.length < cache.current.count ||
        (cache.current.count > 0 &&
            events[cache.current.count - 1] !== cache.current.last)
    )
        cache.current = createTimeline();
    const data = cache.current;
    appendTimeline(data, events);
    const finished = terminal(data.last);
    const status =
        data.last?.type === 'run.completed'
            ? '已完成'
            : data.last?.type === 'run.finished'
            ? data.last.data.status
            : '执行中';
    return (
        <Disclosure
            id={`${run}:run`}
            view={view}
            className='rounded-xl border bg-card p-3'
            title={
                <span className='break-all'>
                    Run {run} · {status}
                </span>
            }
        >
            <div className='my-2 break-all text-sm'>
                <div>会话 ID：{session}</div>
                <div>Run ID：{run}</div>
                {data.duration !== undefined && (
                    <div>运行耗时：{durationText(data.duration)}</div>
                )}
                {data.requestCount > 0 &&
                    tokenFields.map((field) => (
                        <div key={field}>
                            {tokenNames[field]}：
                            {data.usageCount[field]
                                ? data.usage[field]
                                : '缺失'}
                            {(data.usageCount[field] ?? 0) <
                                data.requestCount || !finished
                                ? '（汇总不完整）'
                                : ''}
                        </div>
                    ))}
                {data.requestCount > 0 && (
                    <>
                        <div>
                            缓存命中输入 Token：
                            {data.usageCount.cached_tokens
                                ? data.usage.cached_tokens
                                : '未知'}
                            {(data.usageCount.cached_tokens ?? 0) <
                                data.requestCount || !finished
                                ? '（汇总不完整）'
                                : ''}
                        </div>
                        <div>
                            输入缓存命中率：{rateText(runCacheHitRate(data, finished))}
                        </div>
                    </>
                )}
            </div>
            {data.sections.map((section) =>
                section.kind === 'event' ? (
                    <Row
                        key={section.row.event.seq}
                        row={section.row}
                        view={view}
                    />
                ) : (
                    <Turn
                        key={`${section.turn}:${section.rows[0].event.seq}`}
                        section={section}
                        run={run}
                        finished={finished}
                        view={view}
                    />
                )
            )}
        </Disclosure>
    );
});

export function Timeline({
    session,
    order,
    events,
    view,
}: Props) {
    const scrollRef = useRef<HTMLDivElement>(null);
    useLayoutEffect(() => {
        const viewport = scrollRef.current?.querySelector<HTMLElement>(
            '[data-slot="scroll-area-viewport"]'
        );
        if (!viewport) return;
        viewport.scrollTop = view.scroll.get(session) ?? 0;
        const save = () => view.scroll.set(session, viewport.scrollTop);
        viewport.addEventListener('scroll', save);
        return () => {
            if (viewport.getClientRects().length) save();
            viewport.removeEventListener('scroll', save);
        };
    }, [session, view]);

    return (
        <ScrollArea ref={scrollRef} className='min-h-0 flex-1'>
            <div className='space-y-3 p-3'>
                {order
                    .filter((run) => events[run]?.length)
                    .map((run) => (
                        <Run
                            key={run}
                            session={session}
                            run={run}
                            events={events[run]}
                            view={view}
                        />
                    ))}
            </div>
        </ScrollArea>
    );
}
