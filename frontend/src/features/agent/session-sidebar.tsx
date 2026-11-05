"use client"

import { MessageSquareIcon, PlusIcon, Settings2Icon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"

type SessionSidebarProps = {
  model: string
  onNewSession: () => void
  onOpenConfig: () => void
}

export function SessionSidebar({ model, onNewSession, onOpenConfig }: SessionSidebarProps) {
  return (
    <Sidebar collapsible="offcanvas">
      <SidebarHeader className="p-3">
        <Button variant="outline" className="w-full justify-start rounded-xl" onClick={onNewSession}>
          <PlusIcon data-icon="inline-start" />
          新建会话
        </Button>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>最近会话</SidebarGroupLabel>
          <SidebarGroupContent>
            <Empty className="gap-3 p-4">
              <EmptyHeader className="gap-1">
                <EmptyMedia variant="icon" className="size-9">
                  <MessageSquareIcon />
                </EmptyMedia>
                <EmptyTitle className="text-sm">暂无会话</EmptyTitle>
                <EmptyDescription className="text-xs">会话记录接入后显示在这里</EmptyDescription>
              </EmptyHeader>
            </Empty>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter className="border-t p-3">
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" tooltip="模型配置" onClick={onOpenConfig} className="h-auto">
              <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-sidebar-accent text-sidebar-accent-foreground">
                <Settings2Icon />
              </div>
              <div className="grid min-w-0 flex-1 leading-tight">
                <span className="text-xs text-muted-foreground">模型</span>
                <span className="truncate text-sm font-medium">{model || "未配置"}</span>
              </div>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  )
}
