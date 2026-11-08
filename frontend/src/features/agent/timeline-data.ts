export type AgentEvent = {
    run_id: string;
    seq: number;
    time: string;
    type: string;
    data: Record<string, any>;
};

export type TimelineRow = { event: AgentEvent; duration?: number };
export type TimelineSection =
    | { kind: 'event'; row: TimelineRow }
    | { kind: 'turn'; turn: number; rows: TimelineRow[] };
export type TimelineData = {
    sections: TimelineSection[];
    count: number;
    last?: AgentEvent;
    requests: Map<number, AgentEvent>;
    tools: Map<string, AgentEvent>;
    usage: Record<string, number>;
    usageCount: Record<string, number>;
    requestCount: number;
    started?: AgentEvent;
    duration?: number;
};

export const tokenFields = [
    'prompt_tokens',
    'completion_tokens',
    'total_tokens',
] as const;
export const cachedTokens = (usage: any): number | undefined => {
    const value = usage?.prompt_tokens_details?.cached_tokens;
    return typeof value === 'number' && Number.isFinite(value) && value >= 0
        ? value
        : undefined;
};
export const cacheHitRate = (
    cached: unknown,
    prompt: unknown
): number | undefined =>
    typeof cached === 'number' &&
    Number.isFinite(cached) &&
    cached >= 0 &&
    typeof prompt === 'number' &&
    Number.isFinite(prompt) &&
    prompt > 0
        ? (cached / prompt) * 100
        : undefined;
export const runCacheHitRate = (timeline: TimelineData, finished: boolean) =>
    finished &&
    timeline.requestCount > 0 &&
    timeline.usageCount.cached_tokens === timeline.requestCount &&
    timeline.usageCount.prompt_tokens === timeline.requestCount
        ? cacheHitRate(timeline.usage.cached_tokens, timeline.usage.prompt_tokens)
        : undefined;
export const terminal = (event?: AgentEvent) =>
    !!event && ['run.completed', 'run.finished'].includes(event.type);
const toolKey = (event: AgentEvent) =>
    JSON.stringify([event.data.turn_index, event.data.tool_call_id]);
const elapsed = (start: AgentEvent | undefined, end: AgentEvent) => {
    if (!start) return undefined;
    const value = Date.parse(end.time) - Date.parse(start.time);
    return Number.isFinite(value) && value >= 0 ? value : undefined;
};

export function createTimeline(): TimelineData {
    return {
        sections: [],
        count: 0,
        requests: new Map(),
        tools: new Map(),
        usage: {},
        usageCount: {},
        requestCount: 0,
    };
}

export function appendTimeline(timeline: TimelineData, events: AgentEvent[]) {
    for (let index = timeline.count; index < events.length; index++) {
        const event = events[index];
        let duration: number | undefined;
        if (event.type === 'run.started') timeline.started = event;
        if (event.type === 'model.request') {
            timeline.requests.set(event.data.turn_index, event);
            timeline.requestCount++;
        }
        if (event.type === 'model.response') {
            duration = elapsed(
                timeline.requests.get(event.data.turn_index),
                event
            );
            for (const field of tokenFields) {
                const value = event.data.usage?.[field];
                if (typeof value === 'number' && Number.isFinite(value)) {
                    timeline.usage[field] =
                        (timeline.usage[field] ?? 0) + value;
                    timeline.usageCount[field] =
                        (timeline.usageCount[field] ?? 0) + 1;
                }
            }
            const cached = cachedTokens(event.data.usage);
            if (cached !== undefined) {
                timeline.usage.cached_tokens =
                    (timeline.usage.cached_tokens ?? 0) + cached;
                timeline.usageCount.cached_tokens =
                    (timeline.usageCount.cached_tokens ?? 0) + 1;
            }
        }
        if (event.type === 'tool.started')
            timeline.tools.set(toolKey(event), event);
        if (event.type === 'tool.completed')
            duration = elapsed(timeline.tools.get(toolKey(event)), event);
        if (terminal(event))
            timeline.duration = elapsed(timeline.started, event);
        if (event.type !== 'model.delta') {
            const row = { event, duration };
            const last = timeline.sections.at(-1);
            const turn = event.data.turn_index;
            if (typeof turn === 'number') {
                if (last?.kind === 'turn' && last.turn === turn)
                    timeline.sections[timeline.sections.length - 1] = {
                        ...last,
                        rows: [...last.rows, row],
                    };
                else
                    timeline.sections.push({ kind: 'turn', turn, rows: [row] });
            } else if (
                event.type.startsWith('steer.') &&
                last?.kind === 'turn'
            ) {
                timeline.sections[timeline.sections.length - 1] = {
                    ...last,
                    rows: [...last.rows, row],
                };
            } else timeline.sections.push({ kind: 'event', row });
        }
        timeline.count++;
        timeline.last = event;
    }
}

export function mergeEvents(
    current: AgentEvent[],
    incoming: AgentEvent[]
): AgentEvent[] {
    if (!incoming.length) return current;
    if (
        current.length &&
        incoming.length === 1 &&
        incoming[0].seq > current[current.length - 1].seq
    )
        return [...current, incoming[0]];
    const bySeq = new Map(current.map((event) => [event.seq, event]));
    for (const event of incoming)
        if (!bySeq.has(event.seq)) bySeq.set(event.seq, event);
    if (bySeq.size === current.length) return current;
    return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}
