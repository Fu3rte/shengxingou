"use client"

import { PlusIcon, Settings2Icon } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarGroupLabel, SidebarHeader, SidebarMenu, SidebarMenuButton, SidebarMenuItem } from "@/components/ui/sidebar"

type Session = { id: string; title: string }

type Props = {
  model: string
  sessions: Session[]
  current: string
  onSelect: (id: string) => void
  onNewSession: () => void
  onOpenConfig: () => void
}

export function SessionSidebar({ model, sessions, current, onSelect, onNewSession, onOpenConfig }: Props) {
  return (
    <Sidebar collapsible="offcanvas">
      <SidebarHeader className="p-3">
        <Button variant="outline" className="w-full justify-start rounded-xl" onClick={onNewSession}>
          <PlusIcon data-icon="inline-start" />新建会话
        </Button>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>最近会话</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>{sessions.map((session) => (
              <SidebarMenuItem key={session.id}>
                <SidebarMenuButton isActive={session.id === current} onClick={() => onSelect(session.id)}>
                  <span className="truncate">{session.title || session.id}</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            ))}</SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter className="border-t p-3">
        <SidebarMenu><SidebarMenuItem>
          <SidebarMenuButton size="lg" tooltip="模型配置" onClick={onOpenConfig} className="h-auto">
            <Settings2Icon /><span className="truncate">{model || "未配置"}</span>
          </SidebarMenuButton>
        </SidebarMenuItem></SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  )
}
