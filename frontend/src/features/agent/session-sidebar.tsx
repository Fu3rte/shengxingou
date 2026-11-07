'use client';

import { PlusIcon, Settings2Icon, Trash2Icon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
    Sidebar,
    SidebarContent,
    SidebarFooter,
    SidebarGroup,
    SidebarGroupContent,
    SidebarGroupLabel,
    SidebarHeader,
    SidebarMenu,
    SidebarMenuAction,
    SidebarMenuButton,
    SidebarMenuItem,
    useSidebar,
} from '@/components/ui/sidebar';
import { ResizeHandle } from '@/features/agent/resize-handle';

type Session = { id: string; title: string };

type Props = {
    model: string;
    width: number;
    reserved: number;
    onWidthChange: (width: number) => void;
    sessions: Session[];
    current: string;
    onSelect: (id: string) => void;
    onDelete: (id: string) => void;
    onNewSession: () => void;
    onOpenConfig: () => void;
};

export function SessionSidebar({
    model,
    width,
    reserved,
    onWidthChange,
    sessions,
    current,
    onSelect,
    onDelete,
    onNewSession,
    onOpenConfig,
}: Props) {
    const { open, isMobile, openMobile } = useSidebar();
    return (
        <Sidebar collapsible='offcanvas' mobileWidth={width}>
            <SidebarHeader className='p-3'>
                <Button
                    variant='outline'
                    className='w-full justify-start rounded-xl'
                    onClick={onNewSession}
                >
                    <PlusIcon data-icon='inline-start' />
                    新建会话
                </Button>
            </SidebarHeader>
            <SidebarContent>
                <SidebarGroup>
                    <SidebarGroupLabel>最近会话</SidebarGroupLabel>
                    <SidebarGroupContent>
                        <SidebarMenu>
                            {sessions.map((session) => (
                                <SidebarMenuItem key={session.id}>
                                    <SidebarMenuButton
                                        isActive={session.id === current}
                                        onClick={() => onSelect(session.id)}
                                    >
                                        <span className='truncate'>
                                            {session.title || session.id}
                                        </span>
                                    </SidebarMenuButton>
                                    <SidebarMenuAction
                                        aria-label={`删除会话 ${
                                            session.title || session.id
                                        }`}
                                        onClick={() => onDelete(session.id)}
                                    >
                                        <Trash2Icon />
                                    </SidebarMenuAction>
                                </SidebarMenuItem>
                            ))}
                        </SidebarMenu>
                    </SidebarGroupContent>
                </SidebarGroup>
            </SidebarContent>
            <SidebarFooter className='border-t p-3'>
                <SidebarMenu>
                    <SidebarMenuItem>
                        <SidebarMenuButton
                            size='lg'
                            tooltip='模型配置'
                            onClick={onOpenConfig}
                            className='h-auto'
                        >
                            <Settings2Icon />
                            <span className='truncate'>
                                {model || '未配置'}
                            </span>
                        </SidebarMenuButton>
                    </SidebarMenuItem>
                </SidebarMenu>
            </SidebarFooter>
            {(isMobile ? openMobile : open) && (
                <ResizeHandle
                    side='left'
                    width={width}
                    min={180}
                    max={480}
                    reserved={isMobile ? 48 : reserved}
                    onChange={onWidthChange}
                />
            )}
        </Sidebar>
    );
}
