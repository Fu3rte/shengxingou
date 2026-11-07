'use client';

import { useRef } from 'react';

type Props = {
    side: 'left' | 'right';
    width: number;
    min: number;
    max: number;
    reserved: number;
    onChange: (width: number) => void;
};

export function ResizeHandle({
    side,
    width,
    min,
    max,
    reserved,
    onChange,
}: Props) {
    const drag = useRef({ x: 0, width: 0 });
    const resize = (value: number) =>
        onChange(
            Math.max(min, Math.min(max, window.innerWidth - reserved, value))
        );
    return (
        <div
            role='separator'
            aria-label={
                side === 'left' ? '调整会话侧边栏宽度' : '调整执行时间线宽度'
            }
            aria-orientation='vertical'
            aria-valuemin={min}
            aria-valuemax={max}
            aria-valuenow={width}
            tabIndex={0}
            className={`absolute inset-y-0 z-20 w-2 cursor-col-resize touch-none outline-none hover:bg-border focus-visible:bg-ring ${
                side === 'left' ? 'right-0' : 'left-0'
            }`}
            onPointerDown={(event) => {
                drag.current = { x: event.clientX, width };
                event.currentTarget.setPointerCapture(event.pointerId);
            }}
            onPointerMove={(event) => {
                if (event.currentTarget.hasPointerCapture(event.pointerId))
                    resize(
                        drag.current.width +
                            (event.clientX - drag.current.x) *
                                (side === 'left' ? 1 : -1)
                    );
            }}
            onKeyDown={(event) => {
                if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight')
                    return;
                event.preventDefault();
                resize(
                    width +
                        (event.key === 'ArrowRight' ? 16 : -16) *
                            (side === 'left' ? 1 : -1)
                );
            }}
        />
    );
}
