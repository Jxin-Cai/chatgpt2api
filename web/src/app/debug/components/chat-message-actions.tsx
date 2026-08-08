"use client";

import { memo, useCallback, useEffect, useRef, useState } from "react";
import { Check, Copy, RefreshCw } from "lucide-react";

type CopyState = "idle" | "copied" | "error";

type ChatMessageActionsProps = {
  text: string;
  canRegenerate?: boolean;
  disabled?: boolean;
  onRegenerate?: () => void;
};

export const ChatMessageActions = memo(function ChatMessageActions({
  text,
  canRegenerate = false,
  disabled = false,
  onRegenerate,
}: ChatMessageActionsProps) {
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const resetTimerRef = useRef<number | null>(null);

  const clearResetTimer = useCallback(() => {
    if (resetTimerRef.current !== null) {
      window.clearTimeout(resetTimerRef.current);
      resetTimerRef.current = null;
    }
  }, []);

  useEffect(() => clearResetTimer, [clearResetTimer]);

  const handleCopy = useCallback(async () => {
    clearResetTimer();
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(text);
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }
    resetTimerRef.current = window.setTimeout(() => {
      resetTimerRef.current = null;
      setCopyState("idle");
    }, 1800);
  }, [clearResetTimer, text]);

  const copyLabel = copyState === "copied" ? "已复制回复" : copyState === "error" ? "复制失败" : "复制回复";

  return (
    <div className="chat-message-actions">
      <button
        type="button"
        onClick={() => void handleCopy()}
        className="chat-message-action-button"
        aria-label={copyLabel}
      >
        {copyState === "copied" ? <Check className="size-4" /> : <Copy className="size-4" />}
        <span>{copyLabel}</span>
      </button>
      {canRegenerate ? (
        <button
          type="button"
          onClick={onRegenerate}
          disabled={disabled}
          className="chat-message-action-button"
          aria-label="重新生成回复"
        >
          <RefreshCw className="size-4" />
          <span>重新生成</span>
        </button>
      ) : null}
      {copyState !== "idle" ? (
        <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">{copyLabel}</span>
      ) : null}
    </div>
  );
});
