import { useId, type SVGProps } from "react";

import { cn } from "@/lib/utils";

type BrandMarkProps = SVGProps<SVGSVGElement> & {
  /** Keep the mark decorative when a visible gpt2api label sits beside it. */
  decorative?: boolean;
  title?: string;
};

/**
 * The gpt2api mark combines a looped `g` with a compact `2` in a rounded
 * square. It uses currentColor so the same mark works in both stone themes.
 */
export function BrandMark({
  className,
  decorative = true,
  title = "gpt2api",
  ...props
}: BrandMarkProps) {
  const titleId = useId();

  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={cn("size-7 shrink-0", className)}
      role={decorative ? undefined : "img"}
      aria-hidden={decorative ? true : undefined}
      aria-labelledby={decorative ? undefined : titleId}
      focusable="false"
      {...props}
    >
      {decorative ? null : <title id={titleId}>{title}</title>}
      <rect x="1.75" y="1.75" width="28.5" height="28.5" rx="8.5" fill="currentColor" opacity="0.12" />
      <path
        d="M13.25 10.75a5.25 5.25 0 1 0 4.5 5.19v-5.19h-4.5m4.5 5.19h-3.5"
        stroke="currentColor"
        strokeWidth="2.25"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M19.25 11.25h2.15a3.3 3.3 0 1 1 2.27 5.67l-5.57 5.08h6.4"
        stroke="currentColor"
        strokeWidth="2.25"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
