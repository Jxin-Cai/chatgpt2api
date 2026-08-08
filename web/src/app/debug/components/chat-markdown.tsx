"use client";

import { Children, isValidElement, memo, useCallback, useEffect, useRef, useState } from "react";
import type { ComponentPropsWithoutRef, ReactElement, ReactNode } from "react";
import { Check, Copy } from "lucide-react";
import ReactMarkdown from "react-markdown";
import type { Components, ExtraProps, UrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { defaultUrlTransform } from "react-markdown";

type CodeBlockProps = {
  code: string;
  language: string;
};

type CopyState = "idle" | "copied" | "error";

const DATA_IMAGE_URL = /^data:image\/(?:png|jpeg|jpg|webp|gif);base64,[A-Za-z0-9+/]+={0,2}$/i;

function reactNodeText(value: ReactNode): string {
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(reactNodeText).join("");
  if (isValidElement<{ children?: ReactNode }>(value)) return reactNodeText(value.props.children);
  return "";
}

function CodeBlock({ code, language }: CodeBlockProps) {
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
      await navigator.clipboard.writeText(code);
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }
    resetTimerRef.current = window.setTimeout(() => {
      resetTimerRef.current = null;
      setCopyState("idle");
    }, 1800);
  }, [clearResetTimer, code]);

  const copyLabel = copyState === "copied" ? "已复制代码" : copyState === "error" ? "复制失败" : "复制代码";

  return (
    <div className="chat-markdown-code-block">
      <div className="chat-markdown-code-header">
        <span className="chat-markdown-code-language">{language || "CODE"}</span>
        <button
          type="button"
          onClick={() => void handleCopy()}
          className="chat-markdown-copy-button"
          aria-label={copyLabel}
        >
          {copyState === "copied" ? <Check className="size-4" /> : <Copy className="size-4" />}
          <span className="chat-markdown-copy-label">{copyLabel}</span>
        </button>
      </div>
      <pre className="chat-markdown-code-scroll"><code>{code}</code></pre>
      {copyState !== "idle" ? (
        <span className="sr-only" role="status" aria-live="polite">{copyLabel}</span>
      ) : null}
    </div>
  );
}

function MarkdownCode({ children, className, ...props }: ComponentPropsWithoutRef<"code"> & ExtraProps) {
  return <code {...props} className={`chat-markdown-inline-code${className ? ` ${className}` : ""}`}>{children}</code>;
}

function MarkdownPre({ children }: { children?: ReactNode }) {
  const child = Children.toArray(children).find((item) => isValidElement(item)) as ReactElement<{
    children?: ReactNode;
    className?: string;
  }> | undefined;
  if (!child) return <pre className="chat-markdown-code-scroll">{children}</pre>;
  const language = child.props.className?.match(/language-([^\s]+)/)?.[1]?.toUpperCase() || "CODE";
  const code = reactNodeText(child.props.children).replace(/\n$/, "");
  return <CodeBlock code={code} language={language} />;
}

function MarkdownLink({ children, href, title, ...props }: ComponentPropsWithoutRef<"a"> & ExtraProps) {
  const isExternal = typeof href === "string" && /^https?:\/\//i.test(href);
  return (
    <a
      {...props}
      href={href}
      title={title}
      {...(isExternal ? { target: "_blank", rel: "noopener noreferrer" } : {})}
      className="chat-markdown-link"
    >
      {children}
    </a>
  );
}

function MarkdownImage({ src, alt, title, ...props }: ComponentPropsWithoutRef<"img"> & ExtraProps) {
  if (!src) return null;
  const fallbackAlt = alt?.trim() || "Arc 生成的图片";
  return (
    <span className="chat-markdown-image-frame">
      <img
        {...props}
        src={src}
        alt={fallbackAlt}
        title={title}
        loading="lazy"
        decoding="async"
        className="chat-markdown-image"
      />
      {alt?.trim() ? <span className="chat-markdown-image-caption">{alt}</span> : null}
    </span>
  );
}

const chatUrlTransform: UrlTransform = (url, key, node) => {
  const safeUrl = defaultUrlTransform(url);
  if (safeUrl) return safeUrl;
  if (key === "src" && node.tagName === "img" && DATA_IMAGE_URL.test(url)) return url;
  return "";
};

const markdownComponents: Components = {
  h1: ({ children, ...props }) => <h1 {...props} className="chat-markdown-heading chat-markdown-heading--1">{children}</h1>,
  h2: ({ children, ...props }) => <h2 {...props} className="chat-markdown-heading chat-markdown-heading--2">{children}</h2>,
  h3: ({ children, ...props }) => <h3 {...props} className="chat-markdown-heading chat-markdown-heading--3">{children}</h3>,
  h4: ({ children, ...props }) => <h4 {...props} className="chat-markdown-heading chat-markdown-heading--4">{children}</h4>,
  p: ({ children, ...props }) => <p {...props} className="chat-markdown-paragraph">{children}</p>,
  ul: ({ children, ...props }) => <ul {...props} className="chat-markdown-list chat-markdown-list--unordered">{children}</ul>,
  ol: ({ children, ...props }) => <ol {...props} className="chat-markdown-list chat-markdown-list--ordered">{children}</ol>,
  li: ({ children, ...props }) => <li {...props} className="chat-markdown-list-item">{children}</li>,
  blockquote: ({ children, ...props }) => <blockquote {...props} className="chat-markdown-blockquote">{children}</blockquote>,
  hr: (props) => <hr {...props} className="chat-markdown-rule" />,
  a: MarkdownLink,
  img: MarkdownImage,
  code: MarkdownCode,
  pre: MarkdownPre,
  table: ({ children, ...props }) => <div className="chat-markdown-table-scroll"><table {...props} className="chat-markdown-table">{children}</table></div>,
  thead: ({ children, ...props }) => <thead {...props}>{children}</thead>,
  tbody: ({ children, ...props }) => <tbody {...props}>{children}</tbody>,
  tr: ({ children, ...props }) => <tr {...props}>{children}</tr>,
  th: ({ children, ...props }) => <th {...props}>{children}</th>,
  td: ({ children, ...props }) => <td {...props}>{children}</td>,
  input: ({ checked, ...props }) => <input {...props} type="checkbox" checked={Boolean(checked)} disabled className="chat-markdown-task-checkbox" aria-label={checked ? "已完成" : "未完成"} />,
};

export const ChatMarkdown = memo(function ChatMarkdown({ content }: { content: string }) {
  if (!content) return null;
  return (
    <div className="chat-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={chatUrlTransform}
        components={markdownComponents}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});
