'use client';

import { memo, useRef } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Button } from '@/components/ui/button';
import { terminal } from '@/features/agent/timeline-data';
import type { AgentEvent } from '@/features/agent/timeline-data';

const statuses: Record<string, string> = {
  stopped: '已停止',
  limited: '达到轮次上限',
  interrupted: '执行中断',
  failed: '执行失败',
};
const key = (event: AgentEvent) =>
  JSON.stringify([event.data.turn_index, event.data.tool_call_id]);

type Props = {
  items: AgentEvent[];
  pending: boolean;
  session: string;
  post: (path: string, body?: object) => Promise<any>;
};

export const ChatRun = memo(function ChatRun({
  items,
  pending,
  session,
  post,
}: Props) {
  const cache = useRef({
    count: 0,
    last: undefined as AgentEvent | undefined,
    visible: [] as AgentEvent[],
    text: new Map<number, string>(),
    results: new Map<string, AgentEvent>(),
    resolved: new Set<string>(),
  });
  if (
    items.length < cache.current.count ||
    (cache.current.count > 0 &&
      items[cache.current.count - 1] !== cache.current.last)
  ) {
    cache.current = {
      count: 0,
      last: undefined,
      visible: [],
      text: new Map(),
      results: new Map(),
      resolved: new Set(),
    };
  }
  const data = cache.current;
  for (let index = data.count; index < items.length; index++) {
    const event = items[index];
    if (event.type === 'model.delta')
      data.text.set(
        event.data.turn_index,
        (data.text.get(event.data.turn_index) ?? '') +
        (event.data.fragments.text || '')
      );
    else if (event.type === 'tool.completed')
      data.results.set(key(event), event);
    else if (event.type === 'tool.confirmation_resolved')
      data.resolved.add(key(event));
    else if (
      [
        'run.started',
        'steer.queued',
        'model.request',
        'tool.started',
        'tool.confirmation_requested',
        'run.finished',
      ].includes(event.type)
    )
      data.visible.push(event);
    data.count++;
    data.last = event;
  }
  const run = items[0].run_id;
  return (
    <article className='space-y-4'>
      {data.visible.map((event) => {
        const item = event.data;
        if (
          event.type === 'run.started' ||
          event.type === 'steer.queued'
        )
          return (
            <div
              key={event.seq}
              className='ml-auto w-fit max-w-[85%] whitespace-pre-wrap rounded-3xl bg-secondary px-4 py-2.5'
            >
              {event.type === 'run.started'
                ? item.input
                : item.content}
            </div>
          );
        if (event.type === 'model.request') {
          const text = data.text.get(item.turn_index);
          return text ? (
            <div
              key={event.seq}
              className='chat-markdown px-1 leading-7'
            >
              <Markdown remarkPlugins={[remarkGfm]} skipHtml>
                {text}
              </Markdown>
            </div>
          ) : null;
        }
        if (event.type === 'tool.started') {
          const result = data.results.get(key(event));
          return (
            <details
              key={event.seq}
              className='rounded-xl border bg-card px-4 py-3 text-sm'
            >
              <summary className='cursor-pointer font-medium'>
                {item.name}
              </summary>
              <pre className='mt-2 overflow-x-auto whitespace-pre-wrap break-all'>
                {item.arguments}
              </pre>
              {result && (
                <pre className='mt-2 overflow-x-auto whitespace-pre-wrap break-all'>
                  {result.data.result}
                </pre>
              )}
            </details>
          );
        }
        if (
          event.type === 'tool.confirmation_requested' &&
          !terminal(data.last) &&
          !data.resolved.has(key(event))
        )
          return (
            <div
              key={event.seq}
              className='rounded-xl border p-4 space-y-3'
            >
              <p>
                删除待办 {item.task.id}：{item.task.title}
              </p>
              <div className='flex gap-2'>
                {(['approved', 'rejected'] as const).map(
                  (decision) => (
                    <Button
                      key={decision}
                      disabled={pending}
                      variant={
                        decision === 'approved'
                          ? 'default'
                          : 'outline'
                      }
                      onClick={() =>
                        void post(
                          `/api/sessions/${session}/runs/${run}/confirm`,
                          {
                            turn_index:
                              item.turn_index,
                            tool_call_id:
                              item.tool_call_id,
                            decision,
                          }
                        )
                      }
                    >
                      {decision === 'approved'
                        ? '确认删除'
                        : '拒绝'}
                    </Button>
                  )
                )}
              </div>
            </div>
          );
        if (event.type === 'run.finished')
          return (
            <p
              key={event.seq}
              className='text-sm text-muted-foreground'
            >
              {statuses[item.status] || item.status}
              {item.error ? `：${item.error}` : ''}
            </p>
          );
        return null;
      })}
    </article>
  );
});
