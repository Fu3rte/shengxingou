"use client"

import { useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import { Dialog, DialogClose, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

type ModelConfigProps = {
  open: boolean
  running: boolean
  onOpenChange: (open: boolean) => void
  onSaved: (model: string) => void
}

export function ModelConfig({ open, running, onOpenChange, onSaved }: ModelConfigProps) {
  const [model, setModel] = useState("")
  const [baseUrl, setBaseUrl] = useState("")
  const [apiKey, setApiKey] = useState("")
  const [apiKeySet, setApiKeySet] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    void (async () => {
      try {
        const response = await fetch("/api/config", { cache: "no-store", signal: controller.signal })
        if (!response.ok) {
          setError("读取配置失败")
          return
        }
        const config = await response.json()
        setModel(config.model ?? "")
        setBaseUrl(config.base_url ?? "")
        setApiKey("")
        setApiKeySet(config.api_key_set ?? false)
        setError("")
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "读取配置失败")
      }
    })()
    return () => controller.abort()
  }, [open])

  async function save() {
    setPending(true)
    setError("")
    try {
      const response = await fetch("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model, base_url: baseUrl, api_key: apiKey }),
      })
      const result = await response.json()
      if (!response.ok) {
        setError(typeof result.detail === "string" ? result.detail : "保存失败")
        return
      }
      onSaved(result.model)
      onOpenChange(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败")
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent aria-describedby={undefined}>
        <DialogHeader>
          <DialogTitle>模型配置</DialogTitle>
        </DialogHeader>
        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="config-model">模型</Label>
            <Input id="config-model" value={model} onChange={(event) => setModel(event.target.value)} disabled={pending} autoComplete="off" spellCheck={false} />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="config-base-url">接口地址</Label>
            <Input id="config-base-url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} disabled={pending} autoComplete="off" spellCheck={false} />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="config-api-key">API Key{apiKeySet ? "（已设置）" : ""}</Label>
            <Input id="config-api-key" type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} disabled={pending} autoComplete="off" />
          </div>
          {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="outline" disabled={pending}>取消</Button>
          </DialogClose>
          <Button type="button" disabled={pending || running || !model.trim()} onClick={() => void save()}>保存</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
