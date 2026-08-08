"use client";

import { Activity, FileImage, LoaderCircle, MessageSquareText, Mic, Presentation, Search, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuthGuard } from "@/lib/use-auth-guard";

import { ChatPanel } from "./components/chat-panel";
import { PptPanel } from "./components/ppt-panel";
import { PsdPanel } from "./components/psd-panel";
import { RealtimePanel } from "./components/realtime-panel";
import { SearchPanel } from "./components/search-panel";
import { SkillPanel } from "./components/skill-panel";

const tabs = [
  { value: "realtime", title: "实时语音", group: "conversation", description: "WebRTC voice core", icon: Mic },
  { value: "chat", title: "对话", group: "conversation", description: "流式 ARC 对话", icon: MessageSquareText },
  { value: "skills", title: "搜索Skills", group: "utilities", description: "发现可用技能", icon: Sparkles },
  { value: "search", title: "搜索", group: "utilities", description: "检索连接器", icon: Search },
  { value: "ppt", title: "PPT生成", group: "utilities", description: "演示文稿任务", icon: Presentation },
  { value: "psd", title: "PSD生成", group: "utilities", description: "图像文件任务", icon: FileImage },
] as const;

type DebugTab = (typeof tabs)[number]["value"];

const DEFAULT_TAB: DebugTab = "realtime";

function isDebugTab(value: string | null): value is DebugTab {
  return tabs.some((tab) => tab.value === value);
}

function readTabFromLocation(): { value: DebugTab; invalid: boolean } {
  if (typeof window === "undefined") return { value: DEFAULT_TAB, invalid: false };
  try {
    const raw = new URL(window.location.href).searchParams.get("tab");
    return raw === null
      ? { value: DEFAULT_TAB, invalid: false }
      : isDebugTab(raw)
        ? { value: raw, invalid: false }
        : { value: DEFAULT_TAB, invalid: true };
  } catch {
    return { value: DEFAULT_TAB, invalid: true };
  }
}

function syncTabUrl(value: DebugTab, mode: "push" | "replace") {
  if (typeof window === "undefined") return;
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("tab", value);
    window.history[`${mode}State`](window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  } catch {
    // URL/history can be unavailable in embedded clients; the in-memory tab still works.
  }
}

export default function DebugPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  const [activeTab, setActiveTab] = useState<DebugTab>(DEFAULT_TAB);
  const tabHydratedRef = useRef(false);

  useEffect(() => {
    if (!tabHydratedRef.current) {
      tabHydratedRef.current = true;
      const initial = readTabFromLocation();
      setActiveTab(initial.value);
      if (initial.invalid) syncTabUrl(DEFAULT_TAB, "replace");
    }

    const handlePopState = () => {
      const next = readTabFromLocation();
      setActiveTab(next.value);
      if (next.invalid) syncTabUrl(DEFAULT_TAB, "replace");
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const handleTabChange = (value: string) => {
    if (!isDebugTab(value)) return;
    setActiveTab(value);
    syncTabUrl(value, "push");
  };

  const activeMeta = tabs.find((tab) => tab.value === activeTab) || tabs[0];
  const ActiveIcon = activeMeta.icon;

  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="debug-arc-console debug-arc-console--loading flex min-h-[calc(100vh-49px)] items-center justify-center">
        <div className="debug-arc-console__loading-card" role="status" aria-live="polite">
          <LoaderCircle className="size-5 animate-spin" aria-hidden="true" />
          <span>正在验证 ARC 控制台权限…</span>
        </div>
      </div>
    );
  }

  return (
    <main className="debug-arc-console mx-auto flex min-h-[calc(100vh-49px)] w-full max-w-[1600px] min-w-0 flex-col px-4 pt-3 pb-6 md:px-8">
      <a href="#debug-console-content" className="debug-arc-console__skip-link">跳转到控制台内容</a>
      <header className="debug-arc-console__header">
        <div className="debug-arc-console__header-main">
          <div className="debug-arc-console__brand-mark" aria-hidden="true">
            <Activity className="size-5" />
          </div>
          <div className="min-w-0">
            <p className="debug-arc-console__eyebrow">ARC INTELLIGENCE CONSOLE</p>
            <h1 className="debug-arc-console__title">调试控制台</h1>
            <p className="debug-arc-console__subtitle">Conversation-first workspace · 管理员调试空间</p>
          </div>
        </div>
        <div className="debug-arc-console__mode" aria-live="polite">
          <span className="debug-arc-console__mode-label">CURRENT MODE</span>
          <span className="debug-arc-console__mode-value"><ActiveIcon className="size-4" aria-hidden="true" />{activeMeta.title}</span>
          <span className="debug-arc-console__mode-detail">{activeMeta.description}</span>
        </div>
      </header>

      <Tabs value={activeTab} onValueChange={handleTabChange} className="debug-arc-console__tabs min-h-0 flex-1">
        <div className="debug-arc-console__nav-shell">
          <p className="debug-arc-console__nav-kicker">MODULE ROUTING</p>
          <TabsList variant="line" aria-label="ARC 调试模块" className="debug-arc-console__tab-list w-full">
            <span className="debug-arc-console__tab-group" aria-hidden="true">Conversation</span>
            {tabs.filter((tab) => tab.group === "conversation").map(({ value, title, icon: Icon }) => (
              <TabsTrigger key={value} value={value} className="debug-arc-console__tab debug-arc-console__tab--primary">
                <Icon className="size-4" aria-hidden="true" />
                <span>{title}</span>
              </TabsTrigger>
            ))}
            <span className="debug-arc-console__tab-group" aria-hidden="true">Utilities</span>
            {tabs.filter((tab) => tab.group === "utilities").map(({ value, title, icon: Icon }) => (
              <TabsTrigger key={value} value={value} className="debug-arc-console__tab debug-arc-console__tab--utility">
                <Icon className="size-4" aria-hidden="true" />
                <span>{title}</span>
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
        <div id="debug-console-content" tabIndex={-1} className="debug-arc-console__content min-h-0">
          <TabsContent value="realtime" className="min-h-0">
            <RealtimePanel />
          </TabsContent>
          <TabsContent value="skills">
            <SkillPanel />
          </TabsContent>
          <TabsContent value="search" className="min-h-0">
            <SearchPanel />
          </TabsContent>
          <TabsContent value="ppt" className="min-h-0">
            <PptPanel />
          </TabsContent>
          <TabsContent value="psd" className="min-h-0">
            <PsdPanel />
          </TabsContent>
          <TabsContent value="chat" className="min-h-0">
            <ChatPanel />
          </TabsContent>
        </div>
      </Tabs>
    </main>
  );
}
