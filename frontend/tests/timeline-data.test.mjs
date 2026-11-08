import assert from 'node:assert/strict';
import test from 'node:test';
import {
    appendTimeline,
    cachedTokens,
    cacheHitRate,
    createTimeline,
    runCacheHitRate,
} from '../src/features/agent/timeline-data.ts';

const event = (seq, type, data = {}) => ({
    run_id: 'run', seq, time: '2024-01-01T00:00:00Z', type, data,
});
const usage = (prompt, cached) => ({
    prompt_tokens: prompt,
    prompt_tokens_details: { cached_tokens: cached },
});

test('per-call cache hits and unavailable values', () => {
    for (const [cached, expected] of [[0, 0], [25, 25], [100, 100]]) {
        const value = usage(100, cached);
        assert.equal(cachedTokens(value), cached);
        assert.equal(cacheHitRate(cachedTokens(value), value.prompt_tokens), expected);
    }
    for (const value of [undefined, {}, { prompt_tokens_details: {} }]) {
        assert.equal(cachedTokens(value), undefined);
        assert.equal(cacheHitRate(cachedTokens(value), 100), undefined);
    }
    assert.equal(cacheHitRate(0, 0), undefined);
    assert.equal(cacheHitRate(5, undefined), undefined);
    assert.equal(cachedTokens(usage(100, NaN)), undefined);
});

test('run rates include zero and full cache hits', () => {
    for (const cached of [0, 100]) {
        const timeline = createTimeline();
        appendTimeline(timeline, [
            event(1, 'model.request', { turn_index: 1 }),
            event(2, 'model.response', { turn_index: 1, usage: usage(100, cached) }),
        ]);
        assert.equal(runCacheHitRate(timeline, true), cached);
    }
});

test('run rate is token-weighted and incremental append does not recount', () => {
    const timeline = createTimeline();
    const events = [
        event(1, 'model.request', { turn_index: 1 }),
        event(2, 'model.response', { turn_index: 1, usage: usage(100, 100) }),
        event(3, 'model.request', { turn_index: 2 }),
        event(4, 'model.response', { turn_index: 2, usage: usage(300, 0) }),
        event(5, 'run.completed'),
    ];
    appendTimeline(timeline, events.slice(0, 2));
    assert.equal(runCacheHitRate(timeline, false), undefined);
    appendTimeline(timeline, events);
    appendTimeline(timeline, events);
    assert.equal(timeline.requestCount, 2);
    assert.equal(timeline.usage.cached_tokens, 100);
    assert.equal(timeline.usage.prompt_tokens, 400);
    assert.equal(timeline.usageCount.cached_tokens, 2);
    assert.equal(runCacheHitRate(timeline, true), 25);
});

test('run rate is unknown for missing usage, missing fields, pending response or zero input', () => {
    assert.equal(runCacheHitRate(createTimeline(), true), undefined);
    for (const missing of [undefined, {}, { prompt_tokens: 100 }, { prompt_tokens_details: { cached_tokens: 0 } }]) {
        const timeline = createTimeline();
        appendTimeline(timeline, [
            event(1, 'model.request', { turn_index: 1 }),
            event(2, 'model.response', { turn_index: 1, usage: usage(100, 50) }),
            event(3, 'model.request', { turn_index: 2 }),
            event(4, 'model.response', { turn_index: 2, usage: missing }),
            event(5, 'run.completed'),
        ]);
        assert.equal(runCacheHitRate(timeline, true), undefined);
    }
    const pending = createTimeline();
    appendTimeline(pending, [event(1, 'model.request', { turn_index: 1 })]);
    assert.equal(runCacheHitRate(pending, true), undefined);
    const zero = createTimeline();
    appendTimeline(zero, [
        event(1, 'model.request', { turn_index: 1 }),
        event(2, 'model.response', { turn_index: 1, usage: usage(0, 0) }),
    ]);
    assert.equal(runCacheHitRate(zero, true), undefined);
});
